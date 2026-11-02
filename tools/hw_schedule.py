"""抓作业的定时任务入口（launchd 每天 17:00 / 18:00 / 19:00 / 20:30 调一次）。

    .venv/bin/python -m tools.hw_schedule            正常跑（今天已经抓到就什么都不做）
    .venv/bin/python -m tools.hw_schedule --force    今天抓过也再抓一次

每一次：
  1. 今天已经有记录了 → 直接退出（一天成功一次就够）。
  2. pad 没连上 / adb 不通 → 记一条失败，退出。
  3. 调 hw_agent 抓今天（agent 自己处理广告、锁屏、走错页面，见 tools/hw_agent.py）。
  4. 这是今天最后一次（时刻表里的最后一个点之后），而今天还是没抓到 → 小新在孩子的群里说一声，
     让家长去 QQ 群看一眼。一天只说一次。

为什么失败不立刻告警：老师发作业的时间不固定，17:00 还没发很正常。只有最后一次也没抓到才值得打扰人。
日志：tmp/hw/schedule-<日期>.log；运行记录同时进 /api/homework/runs（小新后台能看）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hw_agent import api  # noqa: E402

PAD = "HA2KGGAH"
LOG = ROOT / "tmp" / "hw"


def log(msg: str) -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with (LOG / f"schedule-{time.strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def pad_ok() -> str:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    if f"{PAD}\tdevice" not in out:
        return f"pad（{PAD}）没连上：{out.strip().splitlines()[1:] or '一台设备都没有'}"
    return ""


def now_hm() -> str:
    return time.strftime("%H:%M")


def is_last(at: list[str]) -> bool:
    """现在是不是今天时刻表里的最后一次（过了最后一个时刻点）。"""
    return now_hm() >= max(at)


def tell_parents(text: str) -> None:
    """以小新的身份在每个孩子的群里说一句。"""
    try:
        api("POST", "api/homework/notify", {"text": text})
    except Exception as e:  # noqa: BLE001
        log(f"告诉家长没成功：{e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    today = time.strftime("%Y-%m-%d")
    cfg = api("GET", "api/agent")
    at = cfg.get("at") or ["17:00", "18:00", "19:00", "20:30"]
    have = api("GET", f"api/homework?date={today}")["days"]
    if have and not a.force:
        log(f"今天（{today}）已经抓到了，{len(have[0]['items'])} 科，不用再抓")
        return 0
    bad = pad_ok()
    if bad:
        log(bad)
        api("POST", "api/homework/runs", {"since": today, "until": today, "ok": False, "why": bad, "trigger": "schedule"})
    else:
        log(f"开始抓 {today}")
        r = subprocess.run([sys.executable, "-u", "-m", "tools.hw_agent", "--day", today, "--trigger", "schedule"],
                           cwd=ROOT, capture_output=True, text=True, timeout=1800)
        tail = [x for x in (r.stdout + r.stderr).splitlines() if x.strip()][-6:]
        for x in tail:
            log("  " + x)
        if r.returncode == 0:
            log("✓ 抓到了")
            return 0
    if is_last(at):
        runs = api("GET", "api/homework/runs?limit=20")["runs"]
        told = any(x.get("trigger") == "told" and time.strftime("%Y-%m-%d", time.localtime(x["at"])) == today for x in runs)
        if not told:
            why = (bad or (runs[0].get("why") if runs else "") or "不知道为什么")[:80]
            tell_parents(f"今天的作业我没抓到（{why}）。今天 {len(at)} 次都没成，麻烦去 QQ 群「勤惜2024级3班」看一眼。")
            api("POST", "api/homework/runs", {"since": today, "until": today, "ok": False, "why": "已告诉家长", "trigger": "told"})
            log("今天最后一次也没抓到，已经在群里告诉家长")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
