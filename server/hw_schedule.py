"""抓作业的定时器，跑在小新服务里（不需要 Mac）。

设置 →「作业」里的时刻（默认 16:50 / 17:20 / 17:45，18:00 截止）每个都会跑一轮，**不是抓到就停**：
老师下午陆续发，语文常常最后到（三周实测最晚 10/23 17:32），一次抓不全。
  - 前几轮（incremental）：只补还没有的学科，再看一眼群里有没有今天带【修改】的作业卡。没新的几步就结束。
  - 最后一轮（verify）：把今天每科正文都重读一遍，代码和库里比（homework.changed_enough），改得够多才替换。
  - 今天一科都还没有的第一轮是 full：照常全读。
存储按学科增量合并（homework.merge_day），读对的不会被重读的版本盖掉。

最后一轮跑完还一科都没有、并且家长要通知 → 小新在群里说一句，一天只说一次。
「立即抓取当天」随时可点，5 秒内接手（今天已有的话按增量跑）。
pad 不在线（断网、关机、没开辅助功能）也算这一轮没成，原因写进运行记录。
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
        # 错过 15 分钟以上就算了（服务重启）：时刻之间只隔 25–30 分钟，补晚了会和下一轮挤在一起
        if at <= hm and (today(), at) not in fired and hm < _plus(at, 15):
            return at
    return None


def _plus(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    t = h * 60 + m + minutes
    return f"{min(t // 60, 23):02d}:{t % 60 if t // 60 < 24 else 59:02d}"


def maybe_tell(c: dict, last_why: str) -> None:
    """当天最后一轮跑完、一科作业都没有 → 告诉家长（一天一次）。有几科了就不打扰（缺的那科也许老师今天就没布置）。"""
    if not c.get("notify", True) or told_today() or homework.get_day(today()):
        return
    at = c.get("at") or ["17:45"]
    used = tries_today()
    if now_hm() >= max(at) or used >= (c.get("max_tries") or len(at)):
        tell_parents(f"今天的作业我没抓到（{last_why[:80]}）。今天试了 {used} 次都没成，麻烦去 QQ 群「勤惜2024级3班」看一眼。")
        homework.log_run({"since": today(), "until": today(), "ok": False, "why": "已告诉家长", "trigger": "told"})
        log.info("hw schedule: 今天没抓到，已告诉家长")


def mode_for(c: dict, at: str) -> str:
    """这一轮用哪种模式：今天还一科都没有 → full；最后一个时刻 → verify（重读核对）；其余 → incremental。"""
    if not homework.get_day(today()):
        return "full"
    return "verify" if at == max(c.get("at") or [at]) else "incremental"


def tick(fired: set) -> str:
    """跑一次判断（每分钟）。返回做了什么（测试、日志用）。"""
    from server import hw_agent

    c = cfg()
    t = today()
    # 1) 有人点了「立即抓取」
    req = store.settings().get("agent_request") or {}
    if req.get("day") == t and not req.get("taken"):
        store.update_settings(agent_request={**req, "taken": True, "taken_at": time.time()})
        mode = "verify" if req.get("force") and homework.get_day(t) else ""
        r = hw_agent.grab(t, trigger="now", max_steps=c.get("max_steps") or 0, mode=mode)
        return "立即抓取：" + _say(r)
    # 2) 按时刻表
    if not c.get("on", True):
        return "自动抓关着"
    if time.localtime().tm_wday + 1 not in (c.get("weekdays") or [1, 2, 3, 4, 5]):
        return "今天不是抓取日"
    at = due(c, now_hm(), fired)
    if not at:
        return "还没到点"
    fired.add((t, at))
    if tries_today() >= (c.get("max_tries") or 4):
        return f"{at}：今天次数用完了"
    mode = mode_for(c, at)
    r = hw_agent.grab(t, trigger="schedule", max_steps=c.get("max_steps") or 0, mode=mode)
    if at == max(c.get("at") or [at]):
        maybe_tell(c, r["why"])
    return f"{at}（{mode}）：" + _say(r)


def _say(r: dict) -> str:
    if r.get("changed"):
        return "新增 / 更新了 " + "、".join(r["changed"])
    return "没有新的" if r.get("ok") else r.get("why", "没成")


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
