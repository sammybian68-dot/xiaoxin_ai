"""QQ 音乐：小新这边的封装。真正干活的是 qq_cli.py，跑在单独的 .venv-qqmusic 里（为什么分开见那边的说明），
这里用子进程调它、解析它那一行 JSON。

凭证（DATA/qqmusic/credential.json，600）只在 gpu2 上，靠扫码拿：设置 → 唱歌 → 扫码登录，用微信或手机 QQ 扫
（帐号登的设备满了会报「登录设备超限」，要在 QQ 音乐 App 的登录设备管理里删掉旧的）。musickey 有效期 3 天，后台每 6 小时看一次、拿到 20 小时以后刷新，所以只要服务一直开着就不用再扫；
刷不动了（太久没开、在别处退出了）status 里 expired=true，点歌时告诉家长重新扫。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from server import store

log = logging.getLogger("xiaoxin.qqmusic")

ROOT = Path(__file__).resolve().parents[2]
CLI = Path(__file__).with_name("qq_cli.py")
# 不用 mobile（QQ 音乐 App 扫）：手机上显示「绑定成功」，gpu2 这边的 MQTT 连接却收不到结果（2026-11-06 实测）
KINDS = ("wx", "qq")
REFRESH_EVERY_S = 6 * 3600
STATUS_TTL_S = 60

_lock = threading.Lock()
_status: dict = {"at": 0.0, "value": None}
_login: dict = {"sid": "", "proc": None}


class QQError(RuntimeError):
    pass


def python() -> Path:
    return Path(os.environ.get("QQMUSIC_PYTHON") or ROOT / ".venv-qqmusic" / "bin" / "python")


def cred_path() -> Path:
    return store.DATA / "qqmusic" / "credential.json"


def _login_root() -> Path:
    return store.DATA / "qqmusic" / "login"


def _env() -> dict:
    cred_path().parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cred_path().parent, 0o700)
    return {**os.environ, "QQMUSIC_CRED": str(cred_path())}


def _run(*args: str, timeout: float = 30) -> dict:
    """跑一次 qq_cli.py，返回它输出的 JSON。组件没装、超时、输出不是 JSON 都抛 QQError。"""
    py = python()
    if not py.exists():
        raise QQError("QQ 音乐组件没装（部署 deploy/gpu2.sh 时会装）")
    try:
        r = subprocess.run([str(py), str(CLI), *args], capture_output=True, text=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired as e:
        raise QQError("QQ 音乐没响应") from e
    lines = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise QQError(f"QQ 音乐组件出错：{(r.stderr or r.stdout)[-200:]}")
    data = json.loads(lines[-1])
    if data.get("error"):
        raise QQError(f"QQ 音乐出错：{data['error']}")
    return data


def search(word: str, n: int = 5) -> list[dict]:
    return _run("search", word, str(n))["songs"]


def lyric(mid: str) -> str:
    return _run("lyric", mid).get("lrc") or ""


def fetch(mid: str, dst: Path) -> dict:
    """下载整首。{"ok": True, "quality": "320k"} 或 {"ok": False, "why": "no_login" | "expired" | "no_url"}。"""
    data = _run("fetch", mid, str(dst), timeout=180)
    if data.get("why") in ("no_login", "expired"):
        _status["at"] = 0.0
    return data


def status(fresh: bool = False) -> dict:
    """{logged_in, expired, account（打了码）, expires_at, saved_at} 外加 installed。缓存一分钟。"""
    with _lock:
        if not fresh and _status["value"] is not None and time.time() - _status["at"] < STATUS_TTL_S:
            return _status["value"]
    if not python().exists():
        value = {"installed": False, "logged_in": False}
    elif not cred_path().is_file():
        value = {"installed": True, "logged_in": False}
    else:
        try:
            value = {"installed": True, **{k: v for k, v in _run("status").items() if k != "ok"}}
        except QQError as e:
            value = {"installed": True, "logged_in": True, "expired": None, "error": str(e)}
    with _lock:
        _status.update(at=time.time(), value=value)
    return value


def usable() -> bool:
    s = status()
    return bool(s.get("logged_in")) and s.get("expired") is not True


def refresh() -> dict:
    data = _run("refresh", timeout=60)
    _status["at"] = 0.0
    return data


def logout() -> None:
    login_cancel()
    cred_path().unlink(missing_ok=True)
    _status["at"] = 0.0


# ---------------------------------------------------------------- 扫码登录

def login_start(kind: str) -> str:
    """起一个扫码会话（同时只留一个，新的顶掉旧的），返回会话 id；二维码和状态用 login_state 拿。"""
    if kind not in KINDS:
        raise QQError("不认识的扫码方式")
    py = python()
    if not py.exists():
        raise QQError("QQ 音乐组件没装（部署 deploy/gpu2.sh 时会装）")
    login_cancel()
    shutil.rmtree(_login_root(), ignore_errors=True)
    sid = uuid.uuid4().hex[:12]
    d = _login_root() / sid
    d.mkdir(parents=True)
    os.chmod(_login_root(), 0o700)
    proc = subprocess.Popen([str(py), str(CLI), "login", kind, str(d)], env=_env(), stdout=subprocess.DEVNULL,
                            stderr=(d / "stderr.log").open("w"), start_new_session=True)
    with _lock:
        _login.update(sid=sid, proc=proc)
    for _ in range(100):   # 二维码一般 1–2 秒就出来
        if (d / "state.json").exists() or proc.poll() is not None:
            break
        time.sleep(0.1)
    return sid


def login_state(sid: str) -> dict:
    """{state: waiting | scanned | done | refused | timeout | error, qr: data URL（等扫时才有）, expires}。"""
    with _lock:
        cur, proc = _login["sid"], _login["proc"]
    if not sid or sid != cur:
        return {"state": "gone"}
    d = _login_root() / sid
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {"state": "starting"}
    if st["state"] in ("starting", "waiting", "scanned") and proc is not None and proc.poll() is not None:
        st = {"state": "error", "detail": (d / "stderr.log").read_text(errors="replace")[-200:] if (d / "stderr.log").exists() else ""}
    out = {"state": st["state"], "kind": st.get("kind", ""), "expires": st.get("expires"), "detail": st.get("detail", "")}
    if st["state"] in ("waiting", "scanned") and st.get("qr"):
        qr = d / st["qr"]
        mime = "image/png" if qr.suffix == ".png" else "image/jpeg"
        out["qr"] = f"data:{mime};base64," + base64.b64encode(qr.read_bytes()).decode()
    if st["state"] == "done":
        _status["at"] = 0.0
    return out


def login_cancel() -> None:
    with _lock:
        proc, _login["proc"], _login["sid"] = _login["proc"], None, ""
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()


# ---------------------------------------------------------------- 定时续期

def start_refresher() -> None:
    def loop():
        time.sleep(120)
        while True:
            if cred_path().is_file() and python().exists():
                try:
                    r = refresh()
                    log.info("qqmusic refresh: ok=%s refreshed=%s expired=%s", r.get("ok"), r.get("refreshed"), r.get("expired"))
                except QQError as e:
                    log.warning("qqmusic refresh failed: %s", e)
            time.sleep(REFRESH_EVERY_S)

    threading.Thread(target=loop, daemon=True, name="qqmusic-refresh").start()


if __name__ == "__main__":   # 手动看一眼：python -m server.sing.qq
    print(json.dumps(status(fresh=True), ensure_ascii=False), file=sys.stdout)
