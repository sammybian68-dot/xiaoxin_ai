#!/usr/bin/env bash
# gpu2 的 127.0.0.1:18024 转到 5090（10.70.55.156）本机的 SoulX 翻唱服务。
# 小新和 mainbrain 的 /sing 照旧连 127.0.0.1:18024，不用改配置。cron 每分钟调一次，已在跑就直接退出。
# 放在 gpu2 的 ~/codes/SoulX-Singer/tunnel_5090.sh，crontab：
#   @reboot sleep 60 && $HOME/codes/SoulX-Singer/tunnel_5090.sh
#   * * * * * $HOME/codes/SoulX-Singer/tunnel_5090.sh
# 退回 A100：删掉这两行 cron、结束本脚本和它的 ssh，再在 gpu2 上按原样起 serve_svc。
set -u
exec 9>/tmp/soulx-tunnel.lock
flock -n 9 || exit 0
LOG="$HOME/codes/SoulX-Singer/logs/tunnel_5090.log"
while true; do
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -L 127.0.0.1:18024:127.0.0.1:18024 5090 >> "$LOG" 2>&1
  echo "$(date '+%F %T') tunnel exited rc=$?, retry in 5s" >> "$LOG"
  sleep 5
done
