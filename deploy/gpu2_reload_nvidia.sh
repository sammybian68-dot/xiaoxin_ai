#!/usr/bin/env bash
# 修 gpu2 的 "NVML: Driver/library version mismatch"：内核模块是旧的 595.71，用户态库已经升到 595.91。
# 做法：停掉所有占卡的进程 → 卸载并重新加载 nvidia 内核模块 → 起 CosyVoice3。不重启整机。
#
# 在 Mac 上跑：  ssh -t gpu2 'bash -s' < deploy/gpu2_reload_nvidia.sh
#
# 会停掉的东西（都在 /Data/cmbian/tmp/gpu-reload-20261028/ 留了启动命令备份）：
#   7 个 a100-vllm-* 容器（restart=unless-stopped，停了不会自己起来，要用 docker start 手动起）
#   carHer2 voice worker :8091（它的 python 已经被删了，停了起不来，要先重建 .venv-voice）
#   carHer2 ASR worker :28182（SenseVoice）
set -euo pipefail

CONTAINERS=(a100-vllm-car-nlu-merged a100-vllm-car-nlu-lora a100-vllm-minicpm5-2b a100-vllm-qwen3-1.7b
            a100-vllm-qwen3.5-2b a100-vllm-macbert-nlu a100-vllm-qwen3-4b-nlu)
WORKER_PATTERNS=("app.carher.voice.worker:app" "app.asr.worker:app")

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

say "现在的状态"
cat /proc/driver/nvidia/version | head -1
nvidia-smi -L 2>&1 | head -1 || true

say "1/5 停 vLLM 容器"
running=$(sudo docker ps --format '{{.Names}}')
for c in "${CONTAINERS[@]}"; do
  if grep -qx "$c" <<<"$running"; then sudo docker stop -t 30 "$c" >/dev/null && echo "stopped $c"; fi
done

say "2/5 停 carHer2 的两个 worker"
for pat in "${WORKER_PATTERNS[@]}"; do
  pids=$(pgrep -f "$pat" || true)
  [ -n "$pids" ] && { echo "TERM $pat: $pids"; kill -TERM $pids || true; }
done
sleep 5
for pat in "${WORKER_PATTERNS[@]}"; do
  pids=$(pgrep -f "$pat" || true)
  [ -n "$pids" ] && { echo "KILL $pat: $pids"; kill -KILL $pids || true; }
done

say "3/5 确认没人占卡"
holders=$(sudo lsof -t /dev/nvidia* 2>/dev/null | sort -u || true)
if [ -n "$holders" ]; then
  echo "还有进程占着卡，先处理掉再重跑本脚本：" >&2
  for p in $holders; do echo "  $p $(ps -o user=,cmd= -p "$p" | cut -c1-120)" >&2; done
  exit 1
fi
echo "没有进程占卡"

say "4/5 重新加载 nvidia 内核模块"
sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia
sudo modprobe nvidia
sudo modprobe nvidia_uvm
sudo modprobe nvidia_modeset || true
sudo modprobe nvidia_drm || true
cat /proc/driver/nvidia/version | head -1
nvidia-smi

say "5/5 起 CosyVoice3（Triton :18000 / :18001，TRT-LLM :18080）"
/Data/cmbian/codes/carHer2/server/scripts/start_cosyvoice_trt.sh

say "好了"
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv
echo
echo "要起回某个小模型：sudo docker start <容器名>，例如 sudo docker start a100-vllm-minicpm5-2b"
