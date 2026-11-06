#!/usr/bin/env bash
# 5090 上的 SoulX 翻唱服务：没在跑就拉起来。cron 每分钟调一次，开机也调一次。
# 只听 127.0.0.1:18024，gpu2 经 ssh 隧道（tunnel_5090.sh）进来。
# 放在 5090（cltx，10.70.55.156）的 ~/codes/SoulX-Singer/svc_up.sh，crontab：
#   @reboot sleep 30 && $HOME/codes/SoulX-Singer/svc_up.sh
#   * * * * * $HOME/codes/SoulX-Singer/svc_up.sh
set -u
cd "$HOME/codes/SoulX-Singer"
exec 9>/tmp/soulx-svc.lock
flock -n 9 || exit 0
curl -fs --max-time 3 http://127.0.0.1:18024/health >/dev/null && exit 0
pgrep -f "uvicorn serve_svc:app" >/dev/null && exit 0
mkdir -p logs "$HOME/soulx-jobs" && chmod 700 "$HOME/soulx-jobs"
echo "$(date '+%F %T') starting serve_svc" >> logs/serve_svc.log
CUDA_VISIBLE_DEVICES=0 PYTHONPATH="$PWD" HF_HOME="$HOME/hf-cache" HF_HUB_OFFLINE=1 SOULX_WORK="$HOME/soulx-jobs" \
  setsid nohup .venv/bin/python -m uvicorn serve_svc:app --host 127.0.0.1 --port 18024 \
  >> logs/serve_svc.log 2>&1 < /dev/null 9>&- &
