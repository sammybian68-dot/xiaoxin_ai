"""围棋复盘：挑关键步（纯数字）→ 用代码算出这一步的事实 → 大模型照事实讲 → 校验讲解里的坐标和数字。
设计和为什么这样做见 docs/board-games.md 第 5 节：大模型不会算棋，讲解里每个坐标、每个数字都必须来自 facts。

analysis[i] 是第 i 手之后的局面（黑方视角）；孩子的第 i 手（moves[i-1]）的损失 = analysis[i-1] → analysis[i]。
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from server.games import go_rules as R

log = logging.getLogger("xiaoxin.go_review")

BIG = (0.20, 8.0)     # 胜率掉 ≥ 20% 或目差掉 ≥ 8 目：大失误
MID = (0.10, 4.0)     # ≥ 10% 或 ≥ 4 目：失误
DECIDED = 0.05        # 孩子胜率 < 5% 或 > 95%：胜负已定，只看目差
MID_FALLBACK = 3     # 一手大亏都没有时，讲几手小亏
DEAD, DANGER = 0.75, 0.4   # 一块棋的子平均归对方 ≥ 0.75「很可能被吃」，≥ 0.4「危险」
AREA_MIN = 3.0        # 一个区域（角 / 边 / 中腹）归属合计变了 ≥ 3 个点才说
LOCAL_R = 2           # 「这手本身起没起作用」看落点周围 5×5
USELESS = 1.0         # 周围 5×5 归属合计只涨了 < 1 个点，而这手亏了 ≥ 4 目：这手几乎没用
WORSE = 0.6           # 一块棋（≥ 2 颗）平均归属往对方倒了 ≥ 0.6，且现在偏向对方：「从活变得危险 / 被吃」
PV_LEN = 6
MODELS = ["claude-sonnet-5-5", "claude-opus-5-5"]
NAME = {"B": "黑", "W": "白"}


def _mine(a: dict, me: str) -> tuple[float, float]:
    """(孩子的胜率, 孩子的目差)。"""
    s = 1 if me == "B" else -1
    return (a["winrate"] if me == "B" else 1 - a["winrate"]), a["score"] * s


def losses(g: dict) -> list[dict]:
    """孩子每一手：前后胜率、目差、损失、级别。"""
    me = g["kid_color"]
    out = []
    for i, (color, mv) in enumerate(g["moves"], start=1):
        if color != me or i >= len(g["analysis"]) or not g["analysis"][i - 1] or not g["analysis"][i]:
            continue
        (w0, s0), (w1, s1) = _mine(g["analysis"][i - 1], me), _mine(g["analysis"][i], me)
        dw, ds = w0 - w1, s0 - s1
        decided = w0 < DECIDED or w0 > 1 - DECIDED
        def lvl(th):
            return ds >= th[1] if decided else (dw >= th[0] or ds >= th[1])
        grade = "big" if lvl(BIG) else "mid" if lvl(MID) else ""
        out.append({"n": i, "move": mv, "before": round(w0, 3), "after": round(w1, 3),
                    "score_before": round(s0, 1), "score_after": round(s1, 1),
                    "loss_wr": round(dw, 3), "loss_score": round(ds, 1), "grade": grade, "decided": decided})
    return out


def _weight(x: dict) -> float:
    """这手对胜负影响多大，用来挑关键步。胜率掉了多少最要紧（57% → 2% 就是这盘输掉的那一手）；
    胜负已定以后胜率不动、只看目差，按 40 目 ≈ 胜率掉满来折算，排在真正翻盘的那几手后面。
    原来两类混排：已定后亏 17 目（17/20 = 0.85）排在 57% → 2%（0.55）前面，翻盘那手反而没挑上（2026-11-04 实战）。"""
    return x["loss_score"] / 40 if x["decided"] else x["loss_wr"] + x["loss_score"] / 200


def key_moves(g: dict) -> list[dict]:
    """要讲的手：所有「大亏」的（胜率掉 ≥ 20% 或目差掉 ≥ 8 目），不限手数（家长 2026-11-04：有多少亏大发的都要讲）。
    一手大亏都没有时，挑小亏（≥ 10% 或 ≥ 4 目）里最大的 MID_FALLBACK 手讲，免得复盘里什么都没有。"""
    ls = losses(g)
    big = [x for x in ls if x["grade"] == "big"]
    if big:
        return big
    mid = sorted((x for x in ls if x["grade"] == "mid"), key=lambda x: (-_weight(x), x["n"]))
    return sorted(mid[:MID_FALLBACK], key=lambda x: x["n"])


# ------------------------------------------------------------ 事实（全是代码算的）
def _where(pts) -> str:
    """一片点在棋盘哪儿：右上角 / 上边 / 中腹……（按平均位置）。"""
    pts = list(pts)
    x = sum(p[0] for p in pts) / len(pts)
    y = sum(p[1] for p in pts) / len(pts)
    h = "左" if x < 6 else "右" if x > 12 else ""
    v = "上" if y < 6 else "下" if y > 12 else ""
    if h and v:
        return f"{h}{v}角"
    if h or v:
        return f"{h or v}边"
    return "中腹"


def _stones_str(stones) -> str:
    """一块棋怎么称呼：一个子写坐标，多个写「Q16 那 6 颗」（取最上最左的子当名字）。"""
    s = sorted(stones, key=lambda p: (p[1], p[0]))
    return R.to_gtp(*s[0]) if len(s) == 1 else f"{R.to_gtp(*s[0])} 那 {len(s)} 颗"


def _group_facts(board: R.Board, own: list, me: str, own_before: list | None = None) -> list[str]:
    """盘面上值得说的：被叫吃（1 气）的块（≥ 2 颗）；这手前后从「还行」倒向对方的块；
    对方本来能被吃、这手之后活了的（连成片说）；本来就很可能被吃的大块（≥ 3 颗）。"""
    out, escaped = [], []
    for color in (me, R.other(me)):
        who = "你的" if color == me else "对手的"
        s = 1 if color == "B" else -1
        for stones, libs in board.groups(color):
            n = len(stones)
            if len(libs) == 1 and n >= 2:
                out.append(f"{who}{_stones_str(stones)}只剩 1 口气（在 {R.to_gtp(*next(iter(libs)))}），对方下一手就能提掉")
                continue
            if not own:
                continue
            avg = sum(own[y * R.N + x] * s for x, y in stones) / n   # >0 归自己，<0 归对方
            if own_before and n >= 2:
                was = sum(own_before[y * R.N + x] * s for x, y in stones) / n
                if color == me and was - avg >= WORSE and avg < 0:
                    state = "很可能被吃" if avg <= -DEAD else "变得很危险"
                    out.append(f"{who}{_stones_str(stones)}（{_where(stones)}）这手之前还好好的，这手之后小新看{state}")
                    continue
                # 对方的块：这手之前 AI 判断能吃掉，这手之后它活了 / 逃出来了（孩子错过了吃棋的机会）。
                # 先攒着，下面把挨得近的连成一片说（被围的一大片常常是好几块，分开说一长串）
                if color != me and avg - was >= WORSE and was < 0:
                    escaped.append(stones)
                    continue
            if n < 3:
                continue
            if avg <= -DEAD:
                out.append(f"{who}{_stones_str(stones)}（{_where(stones)}）小新看很可能被吃")
            elif avg <= -DANGER:
                out.append(f"{who}{_stones_str(stones)}（{_where(stones)}）很危险")
    for blob in _clusters(escaped):
        n = len(blob)
        out.insert(0, f"对手{_where(blob)}一共 {n} 颗子（{_stones_str(blob).split(' ')[0]} 一带），这手之前小新看你能吃掉，这手之后活了")
    return out


def _clusters(groups: list[set], gap: int = 2) -> list[set]:
    """几块棋挨得近（任两颗子横竖距离和 ≤ gap+1）就并成一片。"""
    blobs: list[set] = []
    for g in groups:
        merged = set(g)
        for b in [b for b in blobs if any(abs(x1 - x2) + abs(y1 - y2) <= gap + 1 for x1, y1 in g for x2, y2 in b)]:
            merged |= b
            blobs.remove(b)
        blobs.append(merged)
    return sorted(blobs, key=len, reverse=True)


def _pv_facts(board: R.Board, pv: list[str], first: str) -> tuple[list[str], int, int]:
    """把 AI 的变化在棋盘上实际走一遍：每一手提了几颗。返回 (说明, 黑提, 白提)。"""
    b, color, notes, got = board.copy(), first, [], {"B": 0, "W": 0}
    for k, mv in enumerate(pv[:PV_LEN], start=1):
        if mv.lower() == "pass":
            color = R.other(color)
            continue
        try:
            dead = b.play(color, *R.to_xy(mv))
        except R.IllegalMove:
            break
        if dead:
            got[color] += len(dead)
            notes.append(f"第 {k} 手 {NAME[color]} {mv} 提掉 {len(dead)} 颗")
        color = R.other(color)
    return notes, got["B"], got["W"]


AREAS = [(h + v) for v in ("上", "", "下") for h in ("左", "", "右")]


def _area_of(x: int, y: int) -> str:
    h = "左" if x < 6 else "右" if x > 12 else ""
    v = "上" if y < 6 else "下" if y > 12 else ""
    return f"{h}{v}角" if h and v else f"{h or v}边" if h or v else "中腹"


def _territory(before: list, after: list, me: str) -> list[str]:
    """按区域（四个角、四条边、中腹）把归属变化加起来（子和空点都算）：「右下角你少了大约 8 个点」。
    单个点很少完全翻转，损失通常散在一片里，所以按片合计，比数「翻了的点」可靠。"""
    if len(before) != R.N * R.N or len(after) != R.N * R.N:
        return []
    s = 1 if me == "B" else -1
    tot: dict[str, float] = {}
    for i in range(R.N * R.N):
        a = _area_of(i % R.N, i // R.N)
        tot[a] = tot.get(a, 0.0) + (after[i] - before[i]) * s
    out = []
    for a, v in sorted(tot.items(), key=lambda t: t[1]):
        if v <= -AREA_MIN:
            out.append(f"{a}你的地少了大约 {round(-v)} 个点")
        elif v >= AREA_MIN:
            out.append(f"{a}你的地多了大约 {round(v)} 个点")
    return out


def _local_gain(before: list, after: list, x: int, y: int, me: str) -> float:
    """落点周围 5×5 的归属合计涨了多少（孩子视角）：这手在它下的地方起了多少作用。"""
    if len(before) != R.N * R.N or len(after) != R.N * R.N:
        return 0.0
    s = 1 if me == "B" else -1
    return sum((after[j * R.N + i] - before[j * R.N + i]) * s
               for i in range(max(0, x - LOCAL_R), min(R.N, x + LOCAL_R + 1))
               for j in range(max(0, y - LOCAL_R), min(R.N, y + LOCAL_R + 1)))


def _best_saves(board: R.Board, best: str, me: str) -> list[str]:
    """AI 建议的那一手和棋盘上的块是什么关系（都是数气，100% 准）：
    给自己只剩 1–2 口气的块长气（「救」）、把对方的块紧到 1 口气（「叫吃」）或直接提掉。"""
    x, y = R.to_xy(best)
    out, seen = [], set()
    blocks, seen_mine = [], set()   # 绿圈四周孩子自己的几块棋（按块去重）：两块以上 = 下在这里能连起来
    for n in R.neighbors(x, y):
        if board.at(*n) == me and n not in seen_mine:
            st = board.group(*n)[0]
            seen_mine |= st
            blocks.append(st)
    if len(blocks) >= 2:
        names = "和".join(_stones_str(st) for st in blocks[:3])
        out.append(f"小新建议的 {best}（绿圈）能把你{_where(set().union(*blocks))}的{names}连成一块")
    for n in R.neighbors(x, y):
        v = board.at(*n)
        if v == "." or n in seen:
            continue
        stones, libs = board.group(*n)
        seen |= stones
        if v == me and len(libs) <= 2:
            after = board.copy()
            try:
                after.play(me, x, y)
            except R.IllegalMove:
                continue
            now = len(after.group(x, y)[1])
            if now > len(libs):
                out.append(f"小新建议的 {best}（绿圈）紧挨着你{_where(stones)}的{_stones_str(stones)}，是给这块棋长气：原来只有 {len(libs)} 口气，下了以后有 {now} 口")
        elif v == R.other(me):
            after = board.copy()
            try:
                dead = after.play(me, x, y)
            except R.IllegalMove:
                continue
            if set(stones) <= set(dead):
                out.append(f"小新建议的 {best}（绿圈）能直接提掉对手的{_stones_str(stones)}")
            elif len(after.group(*n)[1]) == 1 and len(stones) >= 2:
                out.append(f"小新建议的 {best}（绿圈）能把对手的{_stones_str(stones)}紧到只剩 1 口气（叫吃）")
    return out


def _taken(g: dict, n: int, best: str, me: str) -> list[str]:
    """AI 建议的点，小新紧接着（第 n+1 手）就下在了那里：说明这是双方都要的点。
    再看小新下完以后，绿圈旁边孩子的哪几块棋归属掉了（≥ 0.5）：「你的 C11、C13 变弱了」。都是实际发生的，不是预测。"""
    if n >= len(g["moves"]) or g["moves"][n][1].upper() != best.upper() or n + 1 >= len(g["analysis"]) or not g["analysis"][n + 1]:
        return []
    out = [f"绿圈 {best} 这个点双方都想要：你没下，对手紧接着就下在了这里"]
    o0, o2 = g["analysis"][n - 1].get("ownership") or [], g["analysis"][n + 1].get("ownership") or []
    if len(o0) != R.N * R.N or len(o2) != R.N * R.N:
        return out
    board = R.replay(g["moves"][: n + 1], g.get("stones"))
    s = 1 if me == "B" else -1
    x, y = R.to_xy(best)
    weak, seen = [], set()
    for nb in [q for p in R.neighbors(x, y) for q in [p, *R.neighbors(*p)]]:   # 绿圈两步以内
        if board.at(*nb) != me or nb in seen:
            continue
        st = board.group(*nb)[0]
        seen |= st
        was = sum(o0[j * R.N + i] * s for i, j in st) / len(st)
        now = sum(o2[j * R.N + i] * s for i, j in st) / len(st)
        if was - now >= 0.5:
            weak.append(st)
    if weak:
        out.append(f"对手下了 {best} 以后，你{_where(set().union(*weak))}的" + "、".join(_stones_str(st) for st in weak[:3]) + "变得很弱，可能被吃")
    return out


def _line(x: int, y: int) -> int:
    return min(x, y, R.N - 1 - x, R.N - 1 - y) + 1


def facts(g: dict, n: int) -> dict:
    """孩子第 n 手（1 起）的全部事实。讲解只能用这里出现的坐标和数字。"""
    me = g["kid_color"]
    color, mv = g["moves"][n - 1]
    a0, a1 = g["analysis"][n - 1], g["analysis"][n]
    (w0, s0), (w1, s1) = _mine(a0, me), _mine(a1, me)
    before = R.replay(g["moves"][: n - 1], g.get("stones"))
    after = R.replay(g["moves"][:n], g.get("stones"))
    best = a0["best"][0] if a0.get("best") else None
    lines = [f"这是第 {n} 手，你（{NAME[me]}）下在 {mv}。",
             f"这手之前你的胜率 {w0:.0%}，之后 {w1:.0%}；目差从 {_lead(s0)} 变成 {_lead(s1)}。"]
    if best and best["move"].upper() != mv.upper():
        bw, bs = _mine(best, me)
        lines.append(f"小新建议下在 {best['move']}（绿圈），下那里你的胜率是 {bw:.0%}，目差 {_lead(bs)}。")
        # 整串变化不给模型（黑白交替，模型会把对方的手说成「你接着下」，2026-11-04 实测 7 步里 3 步这样），
        # 只给代码在变化里数出来的提子；变化本身在棋盘上「看 AI 的下法」一步步摆
        notes, _, _ = _pv_facts(before, best["pv"], me)
        lines += [f"按小新建议的下法走下去：{x}" for x in notes]
    # 这一手之后 AI 觉得小新最好的应手，和小新实际下的（预测不等于发生了：模型会说成「被我抢先下了」）
    reply = a1["best"][0] if a1.get("best") else None
    actual = g["moves"][n][1] if n < len(g["moves"]) else ""
    if reply and reply["move"].lower() != "pass":
        same = actual.upper() == reply["move"].upper()
        lines.append(f"你下了 {mv} 以后，小新觉得对手最好的应手是 {reply['move']}"
                     + ("，对手实际也下了这里。" if same else f"（只是看法），对手实际下的是 {actual}。" if actual else "。"))
        notes, _, _ = _pv_facts(after, reply["pv"], R.other(me))
        lines += [f"如果对手下 {reply['move']} 接着走下去：{x}" for x in notes]
    o0, o1 = a0.get("ownership") or [], a1.get("ownership") or []
    if mv.lower() != "pass":
        x, y = R.to_xy(mv)
        gain = _local_gain(o0, o1, x, y, me)
        if gain < USELESS and s0 - s1 >= MID[1]:
            where = f"（{_area_of(x, y)}的第 {_line(x, y)} 线）" if _line(x, y) <= 2 else ""
            lines.append(f"你这手{where}在它周围几乎没起作用，等于白白少下了一手。")
    if best and best["move"].upper() != mv.upper() and best["move"].lower() != "pass":
        lines += _best_saves(before, best["move"], me)
        lines += _taken(g, n, best["move"], me)
    lines += [f"你下完以后：{x}" for x in _group_facts(after, o1, me, o0)]
    lines += [f"和这手之前比：{x}" for x in _territory(o0, o1, me)]
    return {"n": n, "move": mv, "best": best["move"] if best else "", "lines": lines,
            "before": round(w0, 3), "after": round(w1, 3), "score_before": round(s0, 1), "score_after": round(s1, 1)}


def _lead(s: float) -> str:
    return f"你领先 {s:.1f} 目" if s >= 0.05 else f"你落后 {-s:.1f} 目" if s <= -0.05 else "差不多平"


# ------------------------------------------------------------ 讲解 + 校验
PROMPT = """你是陪孩子下围棋的小新（讲解里说「我」就是小新，复盘时你是他的老师）。孩子在上幼儿园，围棋业余 3 级左右，还不认字：
你的讲解会念给他听，他一边听一边看棋盘——棋盘上红圈是他下的这一手，绿圈是你建议下的点。
下面是关于这一手的全部事实，都是围棋程序算出来的，是准的（「小新建议 / 小新看」是你这个老师的判断，
「对手」是刚才和他下棋的那一方，讲的时候说「对手」或「我」都行）：

{facts}

请像老师当面跟小朋友说话那样讲这一手，2 到 3 句短句：
1. 先用一句话说这手亏了：胜率、目差这些数最多挑一个说，说成孩子听得懂的话也行（比如「本来还有机会赢，这一下就很难赢了」）。
2. 重点讲这手的问题：从事实里挑最要紧的一两条（比如「几乎没起作用」「哪块棋只剩几口气」「哪块棋变危险了」「绿圈是给哪块棋长气」）。
   有「你能吃掉 / 它活了」「双方都想要」「连成一块」「变得很弱」这类关于整块棋的事实，优先讲它；「哪里的地少了几个点」最多提一个。
3. 最后说「小新建议下在绿圈那里」和为什么（只用事实里写了的理由；没写理由就只说「小新建议下在绿圈那里」）。
要求：
- 讲解里不要出现「AI」两个字，说「小新觉得」「小新建议」。
- 孩子的这手说「红圈这一手」，建议的点说「绿圈那里」，不要念它们的坐标；别的坐标也尽量少说，要说就说位置（「右上角」「左边」）。
- 说到某一块棋，一定带上它在哪（「左上角那块」「绿圈旁边那块」「右边那 17 颗」），孩子要能在棋盘上找到它；不要只说「有一块棋」。
- 只能用上面事实里出现过的坐标和数字，不许自己加新的坐标、新的变化、新的数字。
- 每一条事实是单独的，不许把两条事实连成因果（比如「对手下了 X，所以你的地少了」），也不许猜孩子的想法（「你是为了……」）。
- 「小新觉得……」「小新建议……」是看法，不是棋局里发生了的事，不要说成发生了；对手实际下在哪不用提。
- 事实里说某块棋「只剩几口气」「很危险」，只说它现在的样子，不要说成是这一手造成的。
- 「对手下了 X 以后……变得很弱」说的是对手占了那个点之后，要把「对手占了绿圈那里以后」这层意思说出来，不能说成「现在」。
- 孩子看不到上面这份清单，不要说「事实里」「材料里」。
- 句子要短，词要简单，不用「厚薄」「味道」「本手」这类术语，不要开场白，不要 markdown。"""
# 讲解里不该出现的说法：提到「事实」本身（孩子不知道有一份事实清单）、
# 「这是最要紧的问题」这类给某条事实加评判的话（事实里没说哪条最要紧）
BANNED = ["事实", "最要紧", "资料里", "材料里", "AI"]
COORD = re.compile(r"(?<![A-Za-z])([A-HJ-T](?:1[0-9]|[1-9]))(?![0-9A-Za-z])")
NUM = re.compile(r"\d+(?:\.\d+)?")


def check(text: str, fx: dict) -> list[str]:
    """讲解里有事实里没有的坐标 / 数字：返回问题清单（空 = 通过）。"""
    src = "\n".join(fx["lines"])
    coords_ok = set(COORD.findall(src))
    nums_ok = set(NUM.findall(src)) | {"1", "2", "3", "4"}   # 「一两手」「第 2 步」这类小数字放过
    probs = [f"不要说「{w}」" for w in BANNED if w in text]
    for c in COORD.findall(text):
        if c not in coords_ok:
            probs.append(f"坐标 {c} 在事实里没有")
    stripped = COORD.sub("", text)
    for x in NUM.findall(stripped):
        if x not in nums_ok:
            probs.append(f"数字 {x} 在事实里没有")
    return probs


def template(fx: dict) -> str:
    """模型两次都没过校验时的兜底：按事实拼的固定说法（念给孩子听，不念坐标）。"""
    s = f"红圈这一手下完，胜率从 {fx['before']:.0%} 掉到了 {fx['after']:.0%}。"
    if fx["best"] and fx["best"].upper() != fx["move"].upper():
        s += "小新建议下在绿圈那里，可以点「看小新的下法」一步步看。"
    return s


def explain(fx: dict, chat=None) -> dict:
    """{"text", "source": model|template, "problems"}。chat(prompt) -> str，测试里换掉。"""
    if chat is None:
        from src.llm import client

        def chat(msgs):
            return client.chat_full(MODELS, msgs, max_tokens=600, timeout=60).text
    msgs = [{"role": "user", "content": PROMPT.format(facts="\n".join(fx["lines"]))}]
    problems = []
    for _ in range(2):
        try:
            text = (chat(msgs) or "").strip()
        except Exception as e:  # noqa: BLE001 模型挂了就用模板
            log.warning("review explain failed: %s", e)
            break
        problems = check(text, fx)
        if text and not problems:
            return {"text": text, "source": "model", "problems": []}
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": "这几处在事实里找不到，去掉或改成事实里的说法再说一遍：" + "；".join(problems)}]
    return {"text": template(fx), "source": "template", "problems": problems}


WORKERS = 8   # 讲解并行生成几手（每手一次模型调用 ~10 秒）
REVIEW_V = 2  # 讲解的版本：2 = 建议用 1500 次深算、讲解里说「小新建议」。旧版本的缓存当作没有，重新生成


def review(g: dict, *, chat=None, with_text: bool = True, wait: bool = True) -> dict:
    """整盘复盘：胜率曲线（孩子视角）、每手损失、关键步（带事实和讲解）。讲解缓存在 g["review"]（调用方负责存盘）。
    wait=False：只给已经生成好的讲解，没好的那几手 text 为空、pending=True（界面先出曲线和棋盘，讲解后台补，见 warm）。"""
    me = g["kid_color"]
    curve = [round(_mine(a, me)[0], 3) if a else None for a in g["analysis"]]
    score = [round(_mine(a, me)[1], 1) if a else None for a in g["analysis"]]
    keys = key_moves(g)
    cache = g.setdefault("review", {})
    if wait:
        fill(g, keys, chat=chat, with_text=with_text)
    for k in keys:
        c = cache.get(str(k["n"]))
        if c is not None and c.get("v") != REVIEW_V:
            c = None
        if c is None:
            fx = facts(g, k["n"])     # 事实是纯代码，很快：讲解没好也先给数字
            c = {"facts": fx["lines"], "best": fx["best"]}
        k.update(c)
        k["pending"] = with_text and "text" not in c
    mine = losses(g)
    best = [a["best"][0]["move"] if a and a.get("best") else "" for a in g["analysis"]]
    pv = [a["best"][0]["pv"][:PV_LEN] if a and a.get("best") else [] for a in g["analysis"]]
    for k in keys:   # 关键步：棋盘上的绿圈、「看小新的下法」用深算的那份
        if k.get("pv"):
            best[k["n"] - 1], pv[k["n"] - 1] = k["best"], k["pv"]
    return {"id": g["id"], "kid_color": me, "curve": curve, "score": score, "keys": keys,
            "pending": sum(k["pending"] for k in keys),
            "summary": summary(g, keys), "moves": g["moves"], "stones": g.get("stones", []),
            "best": best, "pv": pv,
            "stats": {"moves": len(mine), "big": sum(x["grade"] == "big" for x in mine),
                      "mid": sum(x["grade"] == "mid" for x in mine),
                      "avg_loss": round(sum(max(0, x["loss_score"]) for x in mine) / max(1, len(mine)), 2)}}


def fill(g: dict, keys: list[dict] | None = None, *, chat=None, with_text: bool = True, on_one=None, deepen=None) -> int:
    """把缺的讲解补上（并行），写进 g["review"]。on_one(n, c)：每好一手调一次（后台生成时用它存盘、叫醒界面）。
    deepen(g, i) -> best 列表：把第 i 手之后的局面用更多次搜索重算「最好的点」（建议用中国冠军档 1500 次，
    每手分析只有 400 次，变化看得浅）。返回补了几手。"""
    keys = key_moves(g) if keys is None else keys
    cache = g.setdefault("review", {})
    todo = [k for k in keys if (cache.get(str(k["n"])) or {}).get("v") != REVIEW_V
            or (with_text and "text" not in cache[str(k["n"])])]
    if g.get("result") is None:   # 还在下：小新还没应的那手（讲解要用小新的应手）先不讲
        todo = [k for k in todo if k["n"] + 1 < len(g["analysis"])]

    def one(k):
        n, gg = k["n"], g
        deep = None
        if deepen:
            try:
                deep = deepen(g, n - 1)
            except Exception as e:  # noqa: BLE001 深算失败就用 400 次的
                log.warning("deepen %s/%s failed: %s", g.get("id"), n, e)
        if deep:   # 只换这一手之前那次分析的「最好的点」（胜率曲线、亏了多少仍按 400 次的，前后一致）
            ana = list(g["analysis"])
            ana[n - 1] = {**ana[n - 1], "best": deep}
            gg = {**g, "analysis": ana}
        fx = facts(gg, n)
        c = {"facts": fx["lines"], "best": fx["best"], "v": REVIEW_V,
             "pv": deep[0]["pv"][:PV_LEN] if deep else None, "deep": bool(deep)}
        if with_text:
            c.update(explain(fx, chat))
        return n, c
    if todo:
        with ThreadPoolExecutor(min(WORKERS, len(todo))) as ex:
            for n, c in ex.map(one, todo):
                cache[str(n)] = c
                if on_one:
                    on_one(n, c)
    return len(todo)


def summary(g: dict, keys: list[dict]) -> str:
    """一句话总结（纯模板，不用模型）：赢了 / 输了，主要输在哪几手。"""
    r = g.get("result") or {}
    me = g["kid_color"]
    head = "" if not r else ("这盘你赢了。" if r.get("winner") == me else "这盘输了。")
    big = [k for k in keys if k["grade"] == "big"]
    if not keys:
        return head + "没有明显的坏棋，下得很稳。"
    top = sorted(big or keys, key=lambda k: -_weight(k))[:3]
    names = "、".join(f"第 {k['n']} 手" for k in sorted(top, key=lambda k: k["n"]))
    more = f"（一共 {len(big)} 手亏得多，下面都有讲）" if len(big) > 3 else ""
    return head + (f"最亏的是 {names}{more}。" if big else f"可以改进的是 {names}。")
