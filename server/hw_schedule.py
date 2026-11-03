"""抓作业的定时器，跑在小新服务里（不需要 Mac）。

每分钟看一眼：
  - 设置 →「作业」里的时刻到了、今天是抓取日、今天还没抓到、没超过当天次数 → 抓一次（server/hw_agent.grab）。
  - 有人点了「立即抓取当天」→ 马上抓一次（今天抓过就不抓，见 /api/agent/run）。
  - 当天的机会用完了还没抓到，并且家长要通知 → 小新在群里说一句，一天只说一次。

为什么失败不立刻告警：老师发作业的时间不固定，17:00 还没发很正常。只有当天最后一次也没抓到才值得打扰人。
pad 不在线（断网、关机、没开辅助功能）也算一次没抓到，原因写进运行记录。
"""

from __future__ import annotations

import logging
import threading
import time

from server import homework, store

log = logging.getLogger(__name__)
_started = False
_lock = threading.Lock()


def cfg() -> dict:
    from server.main import agent_cfg

    return agent_cfg()


def today() -> str:
    return time.strftime("%Y-%m-%d")


def now_hm() -> str:
    return time.strftime("%H:%M")


def tries_today() -> int:
    t = today()
    return sum(1 for r in homework.runs(60) if r.get("trigger") in ("schedule", "now")
               and time.strftime("%Y-%m-%d", time.localtime(r.get("at", 0))) == t and r.get("since") == t)


def told_today() -> bool:
    t = today()
    return any(r.get("trigger") == "told" and time.strftime("%Y-%m-%d", time.localtime(r.get("at", 0))) == t
               for r in homework.runs(60))


def tell_parents(text: str) -> None:
    from server.family import members

    for k in members.kids():
        store.append(k["thread"], {"role": "assistant", "text": text, "model": "", "via": "homework"})


def due(c: dict, hm: str, fired: set) -> str | None:
    """现在该不该按时刻表抓：到了某个时刻、这个时刻今天还没触发过。返回那个时刻。"""
    for at in sorted(c.get("at") or []):
        if at <= hm and (today(), at) not in fired and hm < _plus(at, 30):  # 错过 30 分钟以上就算了（服务重启）
            return at
    return None


def _plus(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    t = h * 60 + m + minutes
    return f"{min(t // 60, 23):02d}:{t % 60 if t // 60 < 24 else 59:02d}"


def maybe_tell(c: dict, last_why: str) -> None:
    """当天机会用完还没抓到 → 告诉家长（一天一次）。"""
    if not c.get("notify", True) or told_today() or homework.get_day(today()):
        return
    at = c.get("at") or ["17:00"]
    used = tries_today()
    if now_hm() >= max(at) or used >= (c.get("max_tries") or len(at)):
        tell_parents(f"今天的作业我没抓到（{last_why[:80]}）。今天试了 {used} 次都没成，麻烦去 QQ 群「勤惜2024级3班」看一眼。")
        homework.log_run({"since": today(), "until": today(), "ok": False, "why": "已告诉家长", "trigger": "told"})
        log.info("hw schedule: 今天没抓到，已告诉家长")


def tick(fired: set) -> str:
    """跑一次判断（每分钟）。返回做了什么（测试、日志用）。"""
    from server import hw_agent

    c = cfg()
    t = today()
    # 1) 有人点了「立即抓取」
    req = store.settings().get("agent_request") or {}
    if req.get("day") == t and not req.get("taken"):
        store.update_settings(agent_request={**req, "taken": True, "taken_at": time.time()})
        if homework.get_day(t) and not req.get("force"):
            return "立即抓取：今天已经有了"
        r = hw_agent.grab(t, trigger="now", max_steps=c.get("max_steps") or 0)
        if not r["ok"]:
            maybe_tell(c, r["why"])
        return "立即抓取：" + ("抓到了" if r["ok"] else r["why"])
    # 2) 按时刻表
    if not c.get("on", True):
        return "自动抓关着"
    if time.localtime().tm_wday + 1 not in (c.get("weekdays") or [1, 2, 3, 4, 5]):
        return "今天不是抓取日"
    at = due(c, now_hm(), fired)
    if not at:
        return "还没到点"
    fired.add((t, at))
    if homework.get_day(t):
        return f"{at}：今天已经有了"
    if tries_today() >= (c.get("max_tries") or 4):
        return f"{at}：今天次数用完了"
    r = hw_agent.grab(t, trigger="schedule", max_steps=c.get("max_steps") or 0)
    if not r["ok"]:
        maybe_tell(c, r["why"])
    return f"{at}：" + ("抓到了" if r["ok"] else r["why"])


def _loop() -> None:
    fired: set = set()
    while True:
        try:
            what = tick(fired)
            if what not in ("还没到点", "自动抓关着", "今天不是抓取日"):
                log.info("hw schedule: %s", what)
        except Exception:  # noqa: BLE001  定时器不能死
            log.exception("hw schedule")
        # 「立即抓取」要快：有单就 5 秒内响应，没单一分钟看一次
        for _ in range(12):
            time.sleep(5)
            req = store.settings().get("agent_request") or {}
            if req.get("day") == today() and not req.get("taken"):
                break


def start() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="hw-schedule", daemon=True).start()
