"""常驻 KataGo analysis 引擎：一个进程，JSON 行协议（stdin 一行一个请求，stdout 一行一个结果，按 id 对应）。

位置在 gpu2 /Data/cmbian/katago（docs/board-games.md 第 3 节）：katago 二进制 + cudalibs/ 里的 cuBLAS、cuDNN
（系统没装，放 KataGo 自己目录，不动系统）+ main.bin.gz（b28，最强）+ human.bin.gz（b18 人类模型）。
服务启动时在后台拉起（模型加载 ~20 秒）；挂了下次请求时重启。显存约 2 GB，空闲不占算力。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("xiaoxin.katago")

HOME = Path(os.environ.get("KATAGO_HOME", "/Data/cmbian/katago"))
CFG = """logDir = {home}/logs
logAllRequests = false
logSearchInfo = false
reportAnalysisWinratesAs = SIDETOMOVE
numAnalysisThreads = 4
numSearchThreadsPerAnalysisThread = 8
nnMaxBatchSize = 64
nnCacheSizePowerOfTwo = 21
numNNServerThreadsPerModel = 2
maxVisits = 500
"""
START_TIMEOUT = 120   # 模型加载（两个网络）实测 ~20 秒，冷盘慢一些
QUERY_TIMEOUT = 60    # 世界冠军档 3000 次 ~4.5 秒；排队时可能等一会儿


class EngineError(RuntimeError):
    """引擎没起来 / 挂了 / 超时：消息给家长和孩子看。"""


class Engine:
    def __init__(self, home: Path = HOME):
        self.home = home
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()          # 启动、写 stdin
        self.waiting: dict[str, dict] = {}    # id → {"event", "result"}
        self.ids = itertools.count(1)
        self.started_at = 0.0
        self.last_error = ""

    # ------------------------------------------------------------ 进程
    def available(self) -> bool:
        return (self.home / "katago").is_file() and (self.home / "main.bin.gz").is_file()

    def _env(self) -> dict:
        env = dict(os.environ)
        libs = sorted({str(p.parent) for p in (self.home / "cudalibs").rglob("*.so*")})
        env["LD_LIBRARY_PATH"] = ":".join(libs + [env.get("LD_LIBRARY_PATH", "")]).strip(":")
        return env

    def _start(self) -> None:
        if not self.available():
            raise EngineError("这台服务器上没装 KataGo")
        cfg = self.home / "xiaoxin_analysis.cfg"
        cfg.write_text(CFG.format(home=self.home))
        cmd = [str(self.home / "katago"), "analysis", "-model", str(self.home / "main.bin.gz"), "-config", str(cfg)]
        if (self.home / "human.bin.gz").is_file():
            cmd += ["-human-model", str(self.home / "human.bin.gz")]
        log.info("starting katago: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, cwd=self.home, env=self._env(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.started_at = time.time()
        ready = threading.Event()
        threading.Thread(target=self._read_err, args=(self.proc, ready), daemon=True, name="katago-err").start()
        threading.Thread(target=self._read_out, args=(self.proc,), daemon=True, name="katago-out").start()
        if not ready.wait(START_TIMEOUT) or self.proc.poll() is not None:
            self.last_error = self.last_error or "KataGo 启动超时"
            self._kill()
            raise EngineError(f"小新的棋盘大脑没起来：{self.last_error}")
        log.info("katago ready in %.1fs", time.time() - self.started_at)

    def _read_err(self, proc, ready: threading.Event) -> None:
        for line in proc.stderr:
            line = line.strip()
            if "ready to begin handling requests" in line.lower() or "Started, ready" in line:
                ready.set()
            elif "error" in line.lower() or "exception" in line.lower():
                self.last_error = line[-300:]
                log.warning("katago: %s", line[-300:])
        ready.set()  # 进程退出了：让 _start 别傻等

    def _read_out(self, proc) -> None:
        for line in proc.stdout:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("isDuringSearch"):
                continue
            w = self.waiting.get(str(r.get("id")))
            if w is not None:
                w["result"] = r
                w["event"].set()
            elif "error" in r or "warning" in r:
                log.warning("katago: %s", line[:300])
        # 进程没了：叫醒所有等着的
        for w in list(self.waiting.values()):
            w["event"].set()

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def ensure(self) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    log.warning("katago exited (%s), restarting", self.proc.returncode)
                self._start()

    def warm(self) -> None:
        """服务启动时在后台调：先把进程拉起来，孩子第一步不用等 20 秒。"""
        try:
            self.ensure()
        except EngineError as e:
            log.warning("%s", e)

    def status(self) -> dict:
        up = bool(self.proc and self.proc.poll() is None)
        return {"installed": self.available(), "running": up, "since": self.started_at if up else 0,
                "human_model": (self.home / "human.bin.gz").is_file(), "error": "" if up else self.last_error}

    # ------------------------------------------------------------ 请求
    def query(self, body: dict, timeout: float = QUERY_TIMEOUT) -> dict:
        """发一个 analysis 请求，等结果。引擎挂了重启一次再发。"""
        for attempt in range(2):
            self.ensure()
            qid = f"q{next(self.ids)}"
            w = {"event": threading.Event(), "result": None}
            self.waiting[qid] = w
            try:
                with self.lock:
                    try:
                        self.proc.stdin.write(json.dumps({**body, "id": qid}) + "\n")
                        self.proc.stdin.flush()
                    except (BrokenPipeError, OSError, AttributeError):
                        self._kill()
                        continue
                if not w["event"].wait(timeout):
                    raise EngineError("小新想太久了，没想出来")
                r = w["result"]
                if r is None:  # 进程中途挂了
                    self._kill()
                    continue
                if "error" in r:
                    raise EngineError(f"KataGo 报错：{r['error']}")
                return r
            finally:
                self.waiting.pop(qid, None)
        raise EngineError(f"小新的棋盘大脑挂了：{self.last_error or '重启后还是不行'}")


engine = Engine()
