"""抓作业的 agent 和 pad 之间的那根线：服务端下发一个动作，pad 上的无障碍服务（AgentService.java）
长轮询领走、执行、把截图传回来。一次只有一个动作在途（agent 是一步一步走的）。

    服务端：bus.call("tap", {"x": .., "y": ..}) → 阻塞到 pad 回结果，返回 {"ok","out","w","h","shot","widgets"}
    pad：   GET api/agent/next?wait=50  →  POST api/agent/done/<id>

pad 不在线（孩子房间断网、关机、没开辅助功能）：call 等 timeout 秒没人领就抛 PadOffline。
"""

from __future__ import annotations

import threading
import time
import uuid


class PadOffline(RuntimeError):
    pass


class Bus:
    def __init__(self):
        self._cv = threading.Condition()
        self._job: dict | None = None        # 等着 pad 领的动作
        self._results: dict[str, dict] = {}  # id → pad 回的结果
        self.last_seen = 0.0                 # pad 最后一次来领（判断在不在线）

    def online(self, within: float = 90) -> bool:
        return time.time() - self.last_seen < within

    # ---------------------------------------------------------------- 服务端（agent 那边）

    def call(self, name: str, args: dict | None = None, *, claim_timeout: float = 90, run_timeout: float = 60) -> dict:
        jid = uuid.uuid4().hex[:12]
        with self._cv:
            self._job = {"id": jid, "name": name, "args": args or {}, "at": time.time(), "taken": False}
            self._cv.notify_all()
            deadline = time.time() + claim_timeout
            while self._job is not None and self._job["id"] == jid and not self._job["taken"]:
                left = deadline - time.time()
                if left <= 0:
                    self._job = None
                    raise PadOffline("pad 没来领动作（不在线、熄屏太久、或者没开辅助功能）")
                self._cv.wait(left)
            deadline = time.time() + run_timeout
            while jid not in self._results:
                left = deadline - time.time()
                if left <= 0:
                    raise PadOffline(f"pad 领了「{name}」但 {run_timeout:.0f} 秒没回结果")
                self._cv.wait(left)
            return self._results.pop(jid)

    # ---------------------------------------------------------------- pad 那边

    def next(self, wait: float) -> dict:
        """pad 长轮询：有动作就领走，没有就等 wait 秒回空。"""
        with self._cv:
            self.last_seen = time.time()
            deadline = time.time() + wait
            while not (self._job and not self._job["taken"]):
                left = deadline - time.time()
                if left <= 0:
                    return {}
                self._cv.wait(left)
            self._job["taken"] = True
            self._cv.notify_all()
            return {k: self._job[k] for k in ("id", "name", "args")}

    def done(self, jid: str, result: dict) -> None:
        with self._cv:
            self.last_seen = time.time()
            self._results[jid] = result
            if self._job and self._job["id"] == jid:
                self._job = None
            self._cv.notify_all()


bus = Bus()
