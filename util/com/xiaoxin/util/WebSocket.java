package com.xiaoxin.util;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.Map;

import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * 最小的 WebSocket 客户端（RFC 6455），android.jar 里没有现成的，又不想为它拉 OkHttp。
 *
 * wss 用系统默认的 SSLSocketFactory，所以 network_security_config 里配的证书照样生效；握手时校验主机名。
 * 只做客户端需要的：文本 / 二进制帧、分片重组、回 pong、关闭。收消息在自己的线程里回调 Listener。
 */
public final class WebSocket implements Closeable {
    public interface Listener {
        void onText(String text);

        void onBinary(byte[] data);

        /** 连接断了。code 是对方给的关闭码，网络错误时是 -1。 */
        void onClosed(int code, String reason);
    }

    private static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final SecureRandom RANDOM = new SecureRandom();

    private final Socket socket;
    private final OutputStream out;
    private final DataInputStream in;
    private final Listener listener;
    private volatile boolean closed;

    private WebSocket(Socket socket, DataInputStream in, Listener listener) throws IOException {
        this.socket = socket;
        this.out = socket.getOutputStream();
        this.in = in;
        this.listener = listener;
    }

    /** 连上并握手，成功后起一个线程收消息。失败抛 IOException（带服务端状态行）。 */
    public static WebSocket connect(URI uri, Map<String, String> headers, int timeoutMs, Listener listener)
            throws IOException {
        boolean tls = "wss".equals(uri.getScheme());
        String host = uri.getHost();
        int port = uri.getPort() > 0 ? uri.getPort() : (tls ? 443 : 80);
        Socket socket;
        if (tls) {
            // 先连 TCP（带超时），再在上面套 TLS：createSocket(Socket, host, ...) 会带上 SNI
            Socket raw = new Socket();
            raw.connect(new InetSocketAddress(host, port), timeoutMs);
            SSLSocket ssl = (SSLSocket) ((SSLSocketFactory) SSLSocketFactory.getDefault())
                    .createSocket(raw, host, port, true);
            // 握手时按 HTTPS 规则校验证书里的主机名 / IP（SAN）。host 是 IP 时不发 SNI：
            // gpu.cmbian.top 没备案，带这个域名的 SNI 会被运营商掐掉，所以一律用 IP 连。
            SSLParameters params = ssl.getSSLParameters();
            params.setEndpointIdentificationAlgorithm("HTTPS");
            ssl.setSSLParameters(params);
            ssl.setSoTimeout(timeoutMs);
            ssl.startHandshake();
            socket = ssl;
        } else {
            socket = new Socket();
            socket.connect(new InetSocketAddress(host, port), timeoutMs);
            socket.setSoTimeout(timeoutMs);
        }
        socket.setTcpNoDelay(true);

        byte[] nonce = new byte[16];
        RANDOM.nextBytes(nonce);
        String key = Base64.getEncoder().encodeToString(nonce);
        String path = (uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath())
                + (uri.getRawQuery() != null ? "?" + uri.getRawQuery() : "");
        StringBuilder req = new StringBuilder()
                .append("GET ").append(path).append(" HTTP/1.1\r\n")
                .append("Host: ").append(host).append(port == (tls ? 443 : 80) ? "" : ":" + port).append("\r\n")
                .append("Upgrade: websocket\r\nConnection: Upgrade\r\n")
                .append("Sec-WebSocket-Key: ").append(key).append("\r\n")
                .append("Sec-WebSocket-Version: 13\r\n");
        for (Map.Entry<String, String> h : headers.entrySet()) {
            req.append(h.getKey()).append(": ").append(h.getValue()).append("\r\n");
        }
        req.append("\r\n");
        socket.getOutputStream().write(req.toString().getBytes(StandardCharsets.UTF_8));
        socket.getOutputStream().flush();

        DataInputStream in = new DataInputStream(new BufferedInputStream(socket.getInputStream()));
        String status = readLine(in);
        String accept = null;
        for (String line = readLine(in); !line.isEmpty(); line = readLine(in)) {
            int colon = line.indexOf(':');
            if (colon > 0 && line.substring(0, colon).trim().equalsIgnoreCase("Sec-WebSocket-Accept")) {
                accept = line.substring(colon + 1).trim();
            }
        }
        if (!status.contains(" 101")) {
            socket.close();
            throw new IOException("握手失败：" + status);
        }
        if (!expectedAccept(key).equals(accept)) {
            socket.close();
            throw new IOException("握手校验不对");
        }
        socket.setSoTimeout(0);  // 之后一直阻塞读；服务端会定时 ping
        WebSocket ws = new WebSocket(socket, in, listener);
        Thread t = new Thread(ws::readLoop, "ws-read");
        t.setDaemon(true);
        t.start();
        return ws;
    }

    static String expectedAccept(String key) {
        try {
            byte[] sha = MessageDigest.getInstance("SHA-1").digest((key + GUID).getBytes(StandardCharsets.US_ASCII));
            return Base64.getEncoder().encodeToString(sha);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int prev = -1;
        while (true) {
            int c = in.read();
            if (c < 0) {
                throw new IOException("握手时连接断了");
            }
            if (prev == '\r' && c == '\n') {
                byte[] b = buf.toByteArray();
                return new String(b, 0, b.length - 1, StandardCharsets.UTF_8);
            }
            buf.write(c);
            prev = c;
            if (buf.size() > 8192) {
                throw new IOException("握手响应太长");
            }
        }
    }

    // ---------------------------------------------------------------- 发

    public void sendText(String text) throws IOException {
        send(0x1, text.getBytes(StandardCharsets.UTF_8));
    }

    public void sendBinary(byte[] data, int length) throws IOException {
        byte[] copy = new byte[length];
        System.arraycopy(data, 0, copy, 0, length);
        send(0x2, copy);
    }

    /** 客户端发的帧必须加掩码。 */
    private synchronized void send(int opcode, byte[] payload) throws IOException {
        if (closed) {
            throw new IOException("连接已关闭");
        }
        out.write(frame(opcode, payload, RANDOM));
        out.flush();
    }

    static byte[] frame(int opcode, byte[] payload, SecureRandom random) {
        int n = payload.length;
        int header = 2 + (n < 126 ? 0 : n < 65536 ? 2 : 8) + 4;
        byte[] f = new byte[header + n];
        f[0] = (byte) (0x80 | opcode);
        int i = 2;
        if (n < 126) {
            f[1] = (byte) (0x80 | n);
        } else if (n < 65536) {
            f[1] = (byte) (0x80 | 126);
            f[i++] = (byte) (n >>> 8);
            f[i++] = (byte) n;
        } else {
            f[1] = (byte) (0x80 | 127);
            for (int s = 56; s >= 0; s -= 8) {
                f[i++] = (byte) ((long) n >>> s);
            }
        }
        byte[] mask = new byte[4];
        random.nextBytes(mask);
        System.arraycopy(mask, 0, f, i, 4);
        i += 4;
        for (int k = 0; k < n; k++) {
            f[i + k] = (byte) (payload[k] ^ mask[k & 3]);
        }
        return f;
    }

    // ---------------------------------------------------------------- 收

    private void readLoop() {
        int code = -1;
        String reason = "";
        ByteArrayOutputStream partial = new ByteArrayOutputStream();
        int partialOp = 0;
        try {
            while (!closed) {
                int b0 = in.readUnsignedByte();
                int b1 = in.readUnsignedByte();
                boolean fin = (b0 & 0x80) != 0;
                int op = b0 & 0x0f;
                long len = b1 & 0x7f;
                if (len == 126) {
                    len = in.readUnsignedShort();
                } else if (len == 127) {
                    len = in.readLong();
                }
                byte[] mask = null;
                if ((b1 & 0x80) != 0) {
                    mask = new byte[4];
                    in.readFully(mask);
                }
                if (len > 16 * 1024 * 1024) {
                    throw new IOException("帧太大：" + len);
                }
                byte[] payload = new byte[(int) len];
                in.readFully(payload);
                if (mask != null) {
                    for (int k = 0; k < payload.length; k++) {
                        payload[k] ^= mask[k & 3];
                    }
                }
                if (op == 0x8) {  // close
                    if (payload.length >= 2) {
                        code = ((payload[0] & 0xff) << 8) | (payload[1] & 0xff);
                        reason = new String(payload, 2, payload.length - 2, StandardCharsets.UTF_8);
                    } else {
                        code = 1005;
                    }
                    try {
                        send(0x8, new byte[0]);
                    } catch (IOException ignored) {
                        // 对方已经走了
                    }
                    break;
                } else if (op == 0x9) {  // ping
                    send(0xA, payload);
                } else if (op == 0xA) {  // pong
                    continue;
                } else if (op == 0x0 || !fin) {  // 分片
                    if (op != 0x0) {
                        partialOp = op;
                        partial.reset();
                    }
                    partial.write(payload);
                    if (fin) {
                        deliver(partialOp, partial.toByteArray());
                        partial.reset();
                    }
                } else {
                    deliver(op, payload);
                }
            }
        } catch (IOException e) {
            if (!closed) {
                reason = e.getMessage() == null ? e.toString() : e.getMessage();
            }
        } finally {
            boolean wasOpen = !closed;
            closeQuietly();
            if (wasOpen || code != -1) {
                listener.onClosed(code, reason);
            }
        }
    }

    private void deliver(int op, byte[] payload) {
        if (op == 0x1) {
            listener.onText(new String(payload, StandardCharsets.UTF_8));
        } else if (op == 0x2) {
            listener.onBinary(payload);
        }
    }

    @Override
    public void close() {
        if (closed) {
            return;
        }
        try {
            send(0x8, new byte[] {0x03, (byte) 0xe8});  // 1000 正常关闭
        } catch (IOException ignored) {
            // 已经断了
        }
        closeQuietly();
    }

    private void closeQuietly() {
        closed = true;
        try {
            socket.close();
        } catch (IOException ignored) {
            // 关不掉也没关系
        }
    }
}
