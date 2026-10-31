"""成员和设备（docs/architecture.md 6.0、6.4）。

members.json  [{id, role: parent|kid, name, call, avatar, status: active|disabled, created,
                kid 另有 thread（他那条线程的 id）/ grade / style，parent 另有 voice_sample}]
devices.json  [{id, member, kind: pad|phone|web, name, token_sha256, created, last_seen}]

谁在用（Who）：
- admin：用 .env 的 XIAOXIN_KEY 登的（?k= 链接 / Bearer），全部权限，外加成员和设备管理。
- member：用设备 token 登的，权限看这个成员的角色。token 只给一次（配对时），服务端只存 sha256。

配对：管理台给某个成员「添加设备」→ 6 位配对码（5 分钟、一次性）→ 设备在 /pair 页输入 → 拿到长期 token。
第一次用到时（ensure）：没有 members.json 就把阶段 1 那条线程认作第一个孩子的，老的 clock.json 跟着挪过去。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass

from server import store

ROLES = ("parent", "kid")
KINDS = ("pad", "phone", "web")
PAIR_TTL_S = 300
PAIR_MAX_FAILS = 10          # 每分钟全局最多猜错这么多次，防暴力试配对码
TOKEN_PREFIX = "xd_"
FIRST_KID = {"name": "卞依一", "call": "卞依一", "avatar": "👧"}  # 迁移时那条线程的主人：响铃提示和热词里就是她

_lock = threading.RLock()
_pairs: dict[str, dict] = {}
_fails: list[float] = []


class MemberError(ValueError):
    pass


@dataclass(frozen=True)
class Who:
    kind: str                 # admin | member
    member: dict | None = None
    device: dict | None = None

    @property
    def role(self) -> str:
        return "admin" if self.kind == "admin" else self.member["role"]

    @property
    def id(self) -> str:
        return "admin" if self.kind == "admin" else self.member["id"]

    def at_least(self, role: str) -> bool:
        order = {"kid": 0, "parent": 1, "admin": 2}
        return order[self.role] >= order[role]


ADMIN = Who("admin")


def _members_path():
    return store.DATA / "members.json"


def _devices_path():
    return store.DATA / "devices.json"


def _read(path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- 迁移

def ensure() -> list[dict]:
    """有 members.json 就读；没有就建第一个孩子，认领阶段 1 的那条线程和老闹钟。"""
    with _lock:
        path = _members_path()
        if path.is_file():
            return _read(path)
        tid = store.thread_id()
        kid = _new_member("kid", FIRST_KID["name"], FIRST_KID["call"], FIRST_KID["avatar"], thread=tid)
        store._write_json(path, [kid])
        old = store.DATA / "clock.json"
        dest = store.DATA / "clock" / f"{kid['id']}.json"
        data = json.loads(old.read_text(encoding="utf-8")) if old.is_file() else {}
        if store.settings().get("ring_text") and not data.get("ring_text"):
            data["ring_text"] = store.settings()["ring_text"]  # 老的全家默认提示归她
        if data:
            store._write_json(dest, data)
            old.unlink(missing_ok=True)
        return [kid]


def _new_member(role: str, name: str, call: str, avatar: str, **extra) -> dict:
    m = {"id": store._new_id(), "role": role, "name": name, "call": call or name, "avatar": avatar or "",
         "status": "active", "created": time.time()}
    if role == "kid":
        m.update(thread=extra.get("thread") or store.create_thread(name), grade=extra.get("grade", ""),
                 style=extra.get("style", ""))
    else:
        m.update(voice_sample=extra.get("voice_sample", ""))
    return m


# ---------------------------------------------------------------- 成员

def all_members() -> list[dict]:
    return ensure()


def get(mid: str) -> dict:
    for m in ensure():
        if m["id"] == mid:
            return m
    raise KeyError(mid)


def kids(active_only: bool = True) -> list[dict]:
    return [m for m in ensure() if m["role"] == "kid" and (m["status"] == "active" or not active_only)]


def kid_of_thread(tid: str) -> dict | None:
    return next((m for m in ensure() if m["role"] == "kid" and m.get("thread") == tid), None)


def _clean(field: str, value, limit: int) -> str:
    v = str(value or "").strip()
    if len(v) > limit:
        raise MemberError(f"{field}最多 {limit} 个字")
    return v


def add(role: str, name: str, call: str = "", avatar: str = "", grade: str = "", style: str = "") -> dict:
    if role not in ROLES:
        raise MemberError("角色只能是家长或孩子")
    name = _clean("名字", name, 12)
    if not name:
        raise MemberError("名字不能空")
    with _lock:
        ms = ensure()
        if any(m["name"] == name and m["status"] == "active" for m in ms):
            raise MemberError(f"已经有叫「{name}」的成员了")
        m = _new_member(role, name, _clean("称呼", call, 12), _clean("头像", avatar, 4),
                        grade=_clean("年级", grade, 12), style=_clean("说话要求", style, 200))
        ms.append(m)
        store._write_json(_members_path(), ms)
    return m


EDITABLE = {"name": 12, "call": 12, "avatar": 4, "grade": 12, "style": 200}


def update(mid: str, **fields) -> dict:
    """改名字、称呼、头像（孩子另有年级、说话要求）。角色不能改：孩子的线程和闹钟跟着人走。"""
    with _lock:
        ms = ensure()
        m = next((x for x in ms if x["id"] == mid), None)
        if m is None:
            raise KeyError(mid)
        for k, v in fields.items():
            if k == "role":
                raise MemberError("角色不能改")
            if k not in EDITABLE or (k in ("grade", "style") and m["role"] != "kid"):
                continue
            m[k] = _clean({"name": "名字", "call": "称呼", "avatar": "头像", "grade": "年级", "style": "说话要求"}[k],
                          v, EDITABLE[k])
        if not m["name"]:
            raise MemberError("名字不能空")
        store._write_json(_members_path(), ms)
    return m


def set_status(mid: str, active: bool) -> dict:
    """停用 = 默认的「删除」：吊销他所有设备，历史和线程都留着，可以恢复。"""
    with _lock:
        ms = ensure()
        m = next((x for x in ms if x["id"] == mid), None)
        if m is None:
            raise KeyError(mid)
        m["status"] = "active" if active else "disabled"
        store._write_json(_members_path(), ms)
        if not active:
            for d in devices(mid):
                revoke(d["id"])
    return m


def remove(mid: str) -> None:
    """彻底删除（管理台二次确认后）：孩子的线程先打包进 backup/ 再删；设备全吊销。"""
    import shutil

    with _lock:
        ms = ensure()
        m = next((x for x in ms if x["id"] == mid), None)
        if m is None:
            raise KeyError(mid)
        for d in devices(mid):
            revoke(d["id"])
        if m["role"] == "kid" and m.get("thread"):
            dest = store.BACKUP / f"member-{mid}-{time.strftime('%Y%m%d-%H%M%S')}"
            dest.mkdir(parents=True, exist_ok=True)
            src = store.CONV / f"{m['thread']}.json"
            if src.is_file():
                shutil.copy2(src, dest / src.name)
            clk = store.DATA / "clock" / f"{mid}.json"
            if clk.is_file():
                shutil.copy2(clk, dest / "clock.json")
                clk.unlink()
            store.delete(m["thread"])
        ms.remove(m)
        store._write_json(_members_path(), ms)


# ---------------------------------------------------------------- 设备

def devices(mid: str | None = None) -> list[dict]:
    ds = _read(_devices_path())
    return [d for d in ds if mid is None or d["member"] == mid]


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "token_sha256"}


def revoke(did: str) -> None:
    with _lock:
        ds = _read(_devices_path())
        left = [d for d in ds if d["id"] != did]
        if len(left) == len(ds):
            raise KeyError(did)
        store._write_json(_devices_path(), left)


def pair_code(mid: str, kind: str, name: str = "") -> dict:
    """给某个成员发一个配对码（6 位数字、5 分钟、一次性）。"""
    m = get(mid)
    if m["status"] != "active":
        raise MemberError("这个成员停用了，先恢复")
    if kind not in KINDS:
        raise MemberError("设备类型只能是 pad / phone / web")
    now = time.time()
    with _lock:
        for c in [c for c, p in _pairs.items() if p["expires"] < now]:
            _pairs.pop(c)
        while True:
            code = f"{secrets.randbelow(10**6):06d}"
            if code not in _pairs:
                break
        _pairs[code] = {"member": mid, "kind": kind, "name": _clean("设备名", name, 20) or f"{m['name']}的{kind}",
                        "expires": now + PAIR_TTL_S}
    return {"code": code, "expires": now + PAIR_TTL_S, "member": mid}


def redeem(code: str) -> tuple[str, dict]:
    """设备拿配对码换 token。返回 (token, 设备)，token 只在这里出现一次。"""
    now = time.time()
    with _lock:
        _fails[:] = [t for t in _fails if now - t < 60]
        if len(_fails) >= PAIR_MAX_FAILS:
            raise MemberError("试错太多次了，等一分钟再试")
        p = _pairs.pop(str(code or "").strip(), None)
        if p is None or p["expires"] < now:
            _fails.append(now)
            raise MemberError("配对码不对或过期了，在管理台重新生成一个")
        if get(p["member"])["status"] != "active":
            raise MemberError("这个成员停用了")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        d = {"id": store._new_id(), "member": p["member"], "kind": p["kind"], "name": p["name"],
             "token_sha256": _sha(token), "created": now, "last_seen": now}
        ds = _read(_devices_path())
        ds.append(d)
        store._write_json(_devices_path(), ds)
    return token, public(d)


_seen_write: dict[str, float] = {}


def authenticate(token: str) -> Who | None:
    """设备 token → Who。停用的成员、吊销的设备返回 None。最近在线时间最多每分钟落一次盘。"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    h = _sha(token)
    d = next((x for x in _read(_devices_path()) if secrets.compare_digest(x["token_sha256"], h)), None)
    if d is None:
        return None
    try:
        m = get(d["member"])
    except KeyError:
        return None
    if m["status"] != "active":
        return None
    now = time.time()
    if now - _seen_write.get(d["id"], 0) > 60:
        _seen_write[d["id"]] = now
        with _lock:
            ds = _read(_devices_path())
            for x in ds:
                if x["id"] == d["id"]:
                    x["last_seen"] = now
            store._write_json(_devices_path(), ds)
    return Who("member", m, public(d))


# ---------------------------------------------------------------- 权限

def can_thread(who: Who, tid: str) -> bool:
    """能不能进这条线程：管理员和家长能进每个孩子的；孩子只能进自己的。"""
    if who.kind == "admin":
        return True
    if who.role == "parent":
        return kid_of_thread(tid) is not None
    return who.member.get("thread") == tid


def default_kid(who: Who, wanted: str = "") -> dict | None:
    """这次请求是哪个孩子的：孩子就是自己；家长和管理员按 wanted（?kid= 或界面选的），没有就第一个孩子。"""
    if who.kind == "member" and who.role == "kid":
        return who.member
    ks = kids()
    return next((k for k in ks if k["id"] == wanted), ks[0] if ks else None)
