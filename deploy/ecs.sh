#!/usr/bin/env bash
# 更新 ECS（47.116.13.227）上小新视频通话用的 coturn：渲染 deploy/ecs/turnserver.conf → /etc/turnserver.conf → 重启。
#   deploy/ecs.sh
# ECS 只做两件事：frps（gpu2 的隧道，不归这里管）和 coturn。不存家庭数据、不跑业务代码。
# 前提：`ssh ecs` 能免密登（docs/ecs-login.md）；阿里云安全组放行 3478/udp、3478/tcp、49160-49200/udp。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${XIAOXIN_ECS:-ecs}"   # ~/.ssh/config 里的别名，见 docs/ecs-login.md
PUBLIC_IP="${XIAOXIN_ECS_IP:-47.116.13.227}"

SECRET="$(grep '^TURN_SECRET=' "$ROOT/.env" | cut -d= -f2-)"
[ -n "$SECRET" ] || { echo ".env 里没有 TURN_SECRET" >&2; exit 1; }

ssh "$HOST" "command -v turnserver >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q coturn >/dev/null"
PRIVATE_IP="$(ssh "$HOST" "hostname -I | awk '{print \$1}'")"

# 密钥走 stdin，不出现在命令行（ps 看不到）
sed -e "s|@PUBLIC_IP@|$PUBLIC_IP|g" -e "s|@PRIVATE_IP@|$PRIVATE_IP|g" "$ROOT/deploy/ecs/turnserver.conf" \
  | SECRET="$SECRET" python3 -c 'import os,sys; sys.stdout.write(sys.stdin.read().replace("@TURN_SECRET@", os.environ["SECRET"]))' \
  | ssh "$HOST" "install -m 600 -o turnserver -g turnserver /dev/stdin /etc/turnserver.conf \
      && mkdir -p /var/log/turnserver && chown turnserver:turnserver /var/log/turnserver \
      && sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn 2>/dev/null || true; \
      systemctl enable -q coturn && systemctl restart coturn && sleep 1 && systemctl is-active coturn \
      && ss -lnup | grep -q ':3478 ' && echo 'coturn up: 3478/udp+tcp, relay 49160-49200/udp'"
