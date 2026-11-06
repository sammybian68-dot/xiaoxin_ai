"""几页 A4 图 → 一个多页 PWG Raster 文档（image/pwg-raster，PWG 5102.4）。

为什么：原来一页一个打印任务、等上一页打完再发下一页，打印机每页都从头准备（DeskJet 4800 实测每页 25 秒，
3 页 76 秒）。几页放进同一个任务，打印机连着走纸。PWG Raster 是 IPP Everywhere / AirPrint 打印机都必须吃的格式
（JPEG 不是必须的），以后换打印机也通用；300 dpi、sgray_8 / srgb_8 是 IPP Everywhere 规定必须支持的组合。

格式：「RaS2」+ 每页（1796 字节页头 + 压缩的像素行）。压缩：每行前 1 字节「这一行再重复几次」，
行内 0–127 = 下一个像素重复 n+1 次，129–255 = 后面跟 257−n 个原样像素。所有数都是大端。
"""

from __future__ import annotations

import io
import struct

import numpy as np

DPI = 300
A4_PT = (595, 842)                      # PageSize 用点（1/72 英寸）
A4_PX = (2480, 3508)                    # 300 dpi 的 A4
CSPACE = {"gray": 18, "rgb": 19}        # CUPS_CSPACE_SW、CUPS_CSPACE_SRGB（PWG 的 sgray / srgb）


def _str(s: str, n: int = 64) -> bytes:
    return s.encode()[:n - 1].ljust(n, b"\0")


def header(w: int, h: int, mode: str, total: int, dpi: int = DPI, copies: int = 1) -> bytes:
    """一页的 1796 字节页头。字段位置按 PWG 5102.4 表 2（和 CUPS 的 cups_page_header2_t 一致）。"""
    ch = 1 if mode == "gray" else 3
    b = bytearray(1796)
    b[0:64] = _str("PwgRaster")
    b[64:128] = _str("")                         # MediaColor
    b[128:192] = _str("stationery")              # MediaType
    b[192:256] = _str("")                        # PrintContentOptimize

    def u(off: int, v: int) -> None:
        struct.pack_into(">I", b, off, v)

    u(276, dpi); u(280, dpi)                     # HWResolution
    u(340, max(1, copies))                       # NumCopies（我们自己重复页，这里一般是 1）
    u(352, A4_PT[0]); u(356, A4_PT[1])           # PageSize
    u(372, w); u(376, h)                         # Width、Height（像素）
    u(384, 8)                                    # BitsPerColor
    u(388, 8 * ch)                               # BitsPerPixel
    u(392, w * ch)                               # BytesPerLine
    u(396, 0)                                    # ColorOrder：chunky
    u(400, CSPACE[mode])                         # ColorSpace
    u(420, ch)                                   # NumColors
    u(452, total)                                # TotalPageCount
    u(456, 1); u(460, 1)                         # CrossFeedTransform、FeedTransform
    u(484, 0)                                    # PrintQuality：默认
    b[1732:1796] = _str("iso_a4_210x297mm")      # PageSizeName
    return bytes(b)


def _encode_line(px: np.ndarray, ch: int) -> bytes:
    """一行像素（w×ch 的 uint8）→ PWG 行内压缩。相同像素连成段用重复，零碎的攒成原样段（每段最多 128 个像素）。"""
    w = px.shape[0]
    if ch == 1:
        key = px[:, 0]
    else:
        key = px[:, 0].astype(np.uint32) << 16 | px[:, 1].astype(np.uint32) << 8 | px[:, 2]
    # 找相同像素的段：每段 [start, end)
    change = np.flatnonzero(key[1:] != key[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [w]))
    out = bytearray()
    lit_from = None                              # 正在攒的原样段从哪个像素开始

    def flush_literal(upto: int) -> None:
        nonlocal lit_from
        i = lit_from
        while i < upto:
            n = min(128, upto - i)
            out.append(0 if n == 1 else 257 - n)   # 1 个像素按「重复 1 次」写（257−1=256 放不进 1 字节）
            out.extend(px[i:i + n].tobytes())
            i += n
        lit_from = None

    for s, e in zip(starts.tolist(), ends.tolist()):
        n = e - s
        if n == 1:                               # 单个像素：并进原样段
            if lit_from is None:
                lit_from = s
            continue
        if lit_from is not None:
            flush_literal(s)
        while n > 0:
            k = min(128, n)
            out.append(k - 1)
            out.extend(px[s].tobytes())
            n -= k
    if lit_from is not None:
        flush_literal(w)
    return bytes(out)


def encode_page(im, mode: str) -> tuple[int, int, bytes]:
    """PIL 图 → (w, h, 压缩后的像素数据)。图先缩放到 300 dpi 的 A4。相同的行合在一起（白边、空白行一行就带过）。"""
    from PIL import Image

    im = im.convert("L" if mode == "gray" else "RGB")
    if im.size != A4_PX:
        im = im.resize(A4_PX, Image.LANCZOS)
    a = np.asarray(im)
    if a.ndim == 2:
        a = a[:, :, None]
    h, w, ch = a.shape
    out = bytearray()
    y = 0
    while y < h:
        rep = 0
        while rep < 255 and y + rep + 1 < h and np.array_equal(a[y + rep + 1], a[y]):
            rep += 1
        out.append(rep)
        out += _encode_line(a[y], ch)
        y += rep + 1
    return w, h, bytes(out)


def document(pages: list, color: bool) -> bytes:
    """几页（PIL 图或 JPEG 字节）→ 一个多页 PWG Raster 文档。"""
    from PIL import Image

    mode = "rgb" if color else "gray"
    buf = io.BytesIO()
    buf.write(b"RaS2")
    for p in pages:
        im = Image.open(io.BytesIO(p)) if isinstance(p, (bytes, bytearray)) else p
        w, h, data = encode_page(im, mode)
        buf.write(header(w, h, mode, len(pages)))
        buf.write(data)
    return buf.getvalue()


def decode(doc: bytes) -> list[np.ndarray]:
    """测试用：把 PWG Raster 解回每页的像素（h×w×ch）。和 encode 互为反操作。"""
    assert doc[:4] == b"RaS2"
    i, pages = 4, []
    while i < len(doc):
        hd = doc[i:i + 1796]
        i += 1796
        w, h = struct.unpack_from(">II", hd, 372)
        ch = struct.unpack_from(">I", hd, 420)[0]
        img = np.zeros((h, w, ch), np.uint8)
        y = 0
        while y < h:
            rep = doc[i] + 1
            i += 1
            x, line = 0, np.zeros((w, ch), np.uint8)
            while x < w:
                c = doc[i]
                i += 1
                if c < 128:
                    n = c + 1
                    line[x:x + n] = np.frombuffer(doc[i:i + ch], np.uint8)
                    i += ch
                else:
                    n = 257 - c
                    line[x:x + n] = np.frombuffer(doc[i:i + n * ch], np.uint8).reshape(n, ch)
                    i += n * ch
                x += n
            img[y:y + rep] = line
            y += rep
        pages.append(img)
    return pages
