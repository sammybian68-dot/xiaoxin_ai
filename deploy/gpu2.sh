#!/usr/bin/env bash
# 从这台 Mac 把小新部署到 gpu2：同步代码 → 重启 8184 → （首次或加 --proxy）给 mainbrain 8183 装 /xiaoxin/ 转发。
#   deploy/gpu2.sh            只更新小新
#   deploy/gpu2.sh --proxy    同时更新 mainbrain 的转发并重启 8183（会断开训练平台几秒）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${XIAOXIN_HOST:-gpu2}"
DEST="/Data/cmbian/codes/ai_xiaoxin"
MAINBRAIN="/Data/cmbian/codes/mainbrain_27B_model"

# 只传服务端要用的：server/、src/llm、src/search、src/voice。APK 源码、tmp、虚拟环境都不传。
rsync -az --delete \
  --include='/server/***' --include='/src/' --include='/src/llm/***' --include='/src/search/***' --include='/src/voice/***' \
  --include='/requirements.txt' --exclude='__pycache__' --exclude='*' \
  "$ROOT/" "$HOST:$DEST/"
# .env 单独传，权限 600
scp -q "$ROOT/.env" "$HOST:$DEST/.env" && ssh "$HOST" "chmod 600 $DEST/.env"
ssh "$HOST" "bash $DEST/server/run.sh"

if [[ "${1:-}" == "--proxy" ]]; then
  scp -q "$ROOT/deploy/mainbrain_xiaoxin_proxy.py" "$HOST:$MAINBRAIN/server/xiaoxin_proxy.py"
  ssh "$HOST" "cd $MAINBRAIN && python3 - <<'PY'
from pathlib import Path
main = Path('server/main.py'); s = main.read_text()
hook = '\n\n# 小新：/xiaoxin/ 转到 127.0.0.1:8184，代码在 ai_xiaoxin 仓库\nfrom server.xiaoxin_proxy import router as xiaoxin_router  # noqa: E402\n\napp.include_router(xiaoxin_router)\n'
if 'xiaoxin_router' not in s:
    main.write_text(s.rstrip('\n') + '\n' + hook)
    print('main.py: router added')
home = Path('server/static/index.html'); h = home.read_text()
gate = '''    <a class=\"gate\" href=\"/xiaoxin/\">
      <span class=\"gate-en\">Xiaoxin</span>
      <span class=\"gate-zh\">小新</span>
    </a>
  </main>'''
if 'href=\"/xiaoxin/\"' not in h:
    home.write_text(h.replace('  </main>', gate, 1))
    print('index.html: gate added')
PY
python3 -c 'import server.main' && ./run.sh"
  for _ in $(seq 1 30); do
    ssh "$HOST" "curl -fsSk --max-time 2 https://127.0.0.1:8183/xiaoxin/api/health" && echo && break
    sleep 1
  done
fi
echo "https://gpu.cmbian.top:8183/xiaoxin/"
