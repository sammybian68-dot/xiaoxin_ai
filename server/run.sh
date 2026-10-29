#!/usr/bin/env bash
# 在 gpu2 上（重）启小新服务：只听 127.0.0.1:8184，外面经 mainbrain 8183 的 /xiaoxin/ 进来。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
PORT="${XIAOXIN_PORT:-8184}"

pids=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN || true)
[ -n "$pids" ] && echo "$pids" | xargs -r kill -TERM && sleep 2

PY="${XIAOXIN_PYTHON:-python3}"
nohup "$PY" -m uvicorn server.main:app --host 127.0.0.1 --port "$PORT" >> logs/server.log 2>&1 &
pid=$!
for _ in $(seq 1 20); do
  if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "xiaoxin up pid=$pid port=$PORT log=$ROOT/logs/server.log"
    exit 0
  fi
  sleep 0.5
done
echo "xiaoxin did not come up; tail logs/server.log:" >&2
tail -20 logs/server.log >&2
exit 1
