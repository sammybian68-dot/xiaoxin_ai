# 小新 web

地址：**https://gpu.cmbian.top:8183/xiaoxin/**，手机、电脑、平板都能用。

## 登录

不用输密码，用免密链接打开一次就行。

1. 在本仓库的 `.env` 里找到 `XIAOXIN_KEY=...`，拼成下面这条链接：

   ```
   https://gpu.cmbian.top:8183/xiaoxin/?k=<XIAOXIN_KEY 的值>
   ```

   想直接拿到拼好的链接，在仓库根目录运行：

   ```bash
   echo "https://gpu.cmbian.top:8183/xiaoxin/?k=$(grep '^XIAOXIN_KEY=' .env | cut -d= -f2)"
   ```

2. 用手机或电脑的浏览器打开这条链接。浏览器会提示「不安全」或「证书无效」，点「高级 → 继续访问」。这是因为 8183 用的是自签证书。
3. 链接打开后会自动跳到 `https://gpu.cmbian.top:8183/xiaoxin/`，地址栏里不会留下 key。这个浏览器记住登录一年，以后直接打开这个不带 key 的地址就行，可以加到书签或者手机桌面。
4. 换了设备或浏览器，或者清了浏览器数据，再打开一次这条链接。
5. 手机上「按住说话」和「录我的声音」要用麦克风，第一次会弹权限提示，选「允许」。

只知道 `/xiaoxin/` 地址的人进不来，页面会提示「用免密链接打开一次」。

### 链接泄露了怎么办

```bash
# 在本仓库：生成新 key 写回 .env，然后部署
python3 - <<'EOF'
import secrets, pathlib
p = pathlib.Path(".env")
lines = [l for l in p.read_text().splitlines() if not l.startswith("XIAOXIN_KEY=")]
p.write_text("\n".join(lines + ["XIAOXIN_KEY=" + secrets.token_urlsafe(32)]) + "\n")
EOF
deploy/gpu2.sh
```

旧 key 一换，用它拼的链接和所有已登录的浏览器都会失效，各设备用新链接重新打开一次就行。

### 别把链接发到群里

拿到链接的人和你权限完全一样：可以用你的 LLM 和豆包额度，能看全部对话和上传的文件，还能下载你录的声音样本。只发给家里人，最好当面或私聊。

## 部署

```bash
deploy/gpu2.sh            # 改了 server/、src/llm、src/voice 之后跑这个
deploy/gpu2.sh --proxy    # 第一次部署，或者改了 deploy/mainbrain_xiaoxin_proxy.py（会重启 8183，训练平台断几秒）
```

服务在 gpu2 上只监听 `127.0.0.1:8184`。外面的请求先进 mainbrain 的 8183，再由 `/xiaoxin/` 路由转过来。

| 东西 | 位置（gpu2） |
|:--|:--|
| 代码 | `/Data/cmbian/codes/ai_xiaoxin` |
| 日志 | `/Data/cmbian/codes/ai_xiaoxin/logs/server.log` |
| 对话、附件、声音样本 | `/Data/cmbian/private_data/xiaoxin/` |
| 转发 | `/Data/cmbian/codes/mainbrain_27B_model/server/xiaoxin_proxy.py` |

## 本机调试

```bash
.venv/bin/pip install -r requirements.txt
XIAOXIN_DATA=tmp/xiaoxin_data .venv/bin/python -m uvicorn server.main:app --port 8199
# 浏览器打开 http://127.0.0.1:8199/?k=<XIAOXIN_KEY>
```

注意：本机是 http，浏览器在 http 下不让用麦克风，「按住说话」和「录声音」只能在 gpu2 的 https 地址上测。
