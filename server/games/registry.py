"""棋种登记表：设置 →「下棋」页按这里列二级标签。加中国象棋、国际象棋、跳棋时在这里加一项，
再在 server/games/ 下写它的规则和引擎（见 docs/board-games.md 第 7 节）。"""

from __future__ import annotations

from server import store
from server.games import go

GAMES = [
    {"id": "go", "name": "围棋", "ready": True},
    {"id": "xiangqi", "name": "中国象棋", "ready": False},
    {"id": "chess", "name": "国际象棋", "ready": False},
    {"id": "checkers", "name": "跳棋", "ready": False},
]
DEFAULTS = {"go": go.DEFAULTS}


def settings(game: str) -> dict:
    return {**DEFAULTS[game], **((store.settings().get("games") or {}).get(game) or {})}


def update(game: str, patch: dict) -> dict:
    """只认 DEFAULTS 里有的键，类型按默认值的类型转；档位、执色、让子、贴目各自校验。"""
    if game not in DEFAULTS:
        raise ValueError(f"还不支持：{game}")
    cur = settings(game)
    for k, v in (patch or {}).items():
        if k not in DEFAULTS[game]:
            continue
        d = DEFAULTS[game][k]
        if isinstance(d, bool):
            cur[k] = bool(v)
        elif k == "level":
            if v not in go.LEVEL_NAME:
                raise ValueError(f"没有这个档位：{v}")
            cur[k] = v
        elif k == "color":
            if v not in ("B", "W"):
                raise ValueError("执色只能是 B 或 W")
            cur[k] = v
        elif k == "handicap":
            cur[k] = max(0, min(int(v), 9))
        elif k == "hint_max":
            cur[k] = max(0, min(int(v), 30))
        elif k == "komi":
            cur[k] = max(-10.0, min(float(v), 20.0))
    allg = dict(store.settings().get("games") or {})
    allg[game] = cur
    store.update_settings(games=allg)
    return cur
