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


def tries_today() -> int:
    """今天已经试过几次（按运行记录里 trigger=schedule 的条数算）。"""
    today = time.strftime("%Y-%m-%d")
    runs = api("GET", "api/homework/runs?limit=40")["runs"]
    return sum(1 for r in runs if r.get("trigger") in ("schedule", "now")
               and time.strftime("%Y-%m-%d", time.localtime(r.get("at", 0))) == today)


def grab(day: str, steps: int, trigger: str) -> bool:
    log(f"开始抓 {day}（最多 {steps} 步）")
    r = subprocess.run([sys.executable, "-u", "-m", "tools.hw_agent", "--day", day,
                        "--max-steps", str(steps), "--trigger", trigger],
                       cwd=ROOT, capture_output=True, text=True, timeout=2400)
    for x in [x for x in (r.stdout + r.stderr).splitlines() if x.strip()][-6:]:
        log("  " + x)
    return r.returncode == 0


def once(cfg: dict, trigger: str, day: str = "", force: bool = False) -> int:
    """跑一次。返回 0 = 抓到了（或本来就有），1 = 没抓到。"""
    today = day or time.strftime("%Y-%m-%d")
    have = api("GET", f"api/homework?date={today}")["days"]
    if have and not force:
        log(f"{today} 已经抓到了（{len(have[0]['items'])} 科），不用再抓")
        return 0
    bad = pad_ok()
    if bad:
        log(bad)
        api("POST", "api/homework/runs", {"since": today, "until": today, "ok": False, "why": bad, "trigger": trigger})
    elif grab(today, cfg.get("max_steps") or 40, trigger):
        log("✓ 抓到了")
        return 0
    # 今天的机会用完了、而且家长要通知：小新在群里说一句（一天只说一次）
    at = cfg.get("at") or ["17:00"]
    used = tries_today()
    last = now_hm() >= max(at) or used >= (cfg.get("max_tries") or len(at))
    if last and cfg.get("notify", True):
        runs = api("GET", "api/homework/runs?limit=20")["runs"]
        told = any(x.get("trigger") == "told" and time.strftime("%Y-%m-%d", time.localtime(x.get("at", 0))) == today for x in runs)
        if not told:
            why = (bad or (runs[0].get("why") if runs else "") or "不知道为什么")[:80]
            tell_parents(f"今天的作业我没抓到（{why}）。今天试了 {used} 次都没成，麻烦去 QQ 群「勤惜2024级3班」看一眼。")
            api("POST", "api/homework/runs", {"since": today, "until": today, "ok": False, "why": "已告诉家长", "trigger": "told"})
            log(f"今天试了 {used} 次都没抓到，已经在群里告诉家长")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="今天抓过也再抓一次")
    ap.add_argument("--watch", action="store_true", help="常驻：每分钟问一次有没有人点了「立即抓取」")
    a = ap.parse_args()
    cfg = api("GET", "api/agent")
    if a.watch:
        log("盯着「立即抓取」（每分钟问一次）")
        while True:
            try:
                job = api("POST", "api/agent/claim").get("job")
                if job:
                    log(f"收到立即抓取：{job['day']}")
                    once(api("GET", "api/agent"), "now", job["day"], bool(job.get("force")))
            except Exception as e:  # noqa: BLE001  网断一下别退出
                log(f"问任务单出错：{e}")
            time.sleep(60)
    if not cfg.get("on", True) and not a.force:
        log("自动抓关掉了（设置 → 作业），这次不跑")
        return 0
    wd = cfg.get("weekdays") or [1, 2, 3, 4, 5]
    today_wd = time.localtime().tm_wday + 1
    if today_wd not in wd and not a.force:
        log(f"今天星期{'一二三四五六日'[today_wd - 1]}不在抓取日（{wd}），不跑")
        return 0
    used = tries_today()
    if used >= (cfg.get("max_tries") or 4) and not a.force:
        log(f"今天已经试了 {used} 次，到上限了")
        return 1
    return once(cfg, "schedule")


if __name__ == "__main__":
    raise SystemExit(main())
