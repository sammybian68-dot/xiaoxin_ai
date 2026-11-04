"""19 路围棋规则：落子、提子、禁入点、打劫（简单劫）、中国规则数子。纯函数，不碰引擎。

坐标两套：
  内部 (x, y)：x 0–18 从左到右，y 0–18 从上到下（和 KataGo ownership 的行优先顺序一致：A19 在 0）
  棋谱 "Q16"：列 A–T 跳过 I，行 1–19 从下往上（KataGo、SGF 软件都这么写）
"""

from __future__ import annotations

from dataclasses import dataclass, field

N = 19
COLS = "ABCDEFGHJKLMNOPQRST"
B, W = "B", "W"
STAR = [(x, y) for x in (3, 9, 15) for y in (3, 9, 15)]
# 让子的星位顺序（日本、中国规则通用）：2 子对角，3 子加一个角……
HANDICAP = {2: ["Q16", "D4"], 3: ["Q16", "D4", "D16"], 4: ["Q16", "D4", "D16", "Q4"],
            5: ["Q16", "D4", "D16", "Q4", "K10"], 6: ["Q16", "D4", "D16", "Q4", "D10", "Q10"],
            7: ["Q16", "D4", "D16", "Q4", "D10", "Q10", "K10"],
            8: ["Q16", "D4", "D16", "Q4", "D10", "Q10", "K16", "K4"],
            9: ["Q16", "D4", "D16", "Q4", "D10", "Q10", "K16", "K4", "K10"]}


class IllegalMove(ValueError):
    """不能下：消息是给孩子看的。"""


def other(c: str) -> str:
    return W if c == B else B


def to_xy(s: str) -> tuple[int, int]:
    s = s.strip().upper()
    if len(s) < 2 or s[0] not in COLS or not s[1:].isdigit() or not 1 <= int(s[1:]) <= N:
        raise IllegalMove(f"没有这个点：{s}")
    return COLS.index(s[0]), N - int(s[1:])


def to_gtp(x: int, y: int) -> str:
    return f"{COLS[x]}{N - y}"


def neighbors(x: int, y: int):
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < N and 0 <= ny < N:
            yield nx, ny


@dataclass
class Board:
    grid: list[list[str]] = field(default_factory=lambda: [["." for _ in range(N)] for _ in range(N)])
    ko: tuple[int, int] | None = None          # 刚提劫：对方下一手不能马上下这里
    captures: dict = field(default_factory=lambda: {B: 0, W: 0})  # 各方提了对方几颗

    def copy(self) -> "Board":
        return Board([row[:] for row in self.grid], self.ko, dict(self.captures))

    def at(self, x: int, y: int) -> str:
        return self.grid[y][x]

    def group(self, x: int, y: int) -> tuple[set, set]:
        """(x, y) 所在那块棋的 (子集合, 气集合)。"""
        color = self.at(x, y)
        stones, libs, todo = {(x, y)}, set(), [(x, y)]
        while todo:
            cx, cy = todo.pop()
            for n in neighbors(cx, cy):
                v = self.at(*n)
                if v == ".":
                    libs.add(n)
                elif v == color and n not in stones:
                    stones.add(n)
                    todo.append(n)
        return stones, libs

    def groups(self, color: str) -> list[tuple[set, set]]:
        seen, out = set(), []
        for y in range(N):
            for x in range(N):
                if self.grid[y][x] == color and (x, y) not in seen:
                    g = self.group(x, y)
                    seen |= g[0]
                    out.append(g)
        return out

    def play(self, color: str, x: int, y: int) -> list[tuple[int, int]]:
        """落子，返回提掉的子。不合法抛 IllegalMove（盘面不变）。"""
        if not (0 <= x < N and 0 <= y < N):
            raise IllegalMove("棋盘外面不能下")
        if self.grid[y][x] != ".":
            raise IllegalMove("这里已经有子了")
        if self.ko == (x, y):
            raise IllegalMove("打劫：刚被提，要先在别处下一手才能提回来")
        self.grid[y][x] = color
        dead = []
        for n in neighbors(x, y):
            if self.at(*n) == other(color):
                stones, libs = self.group(*n)
                if not libs:
                    dead.extend(stones)
        for sx, sy in set(dead):
            self.grid[sy][sx] = "."
        dead = sorted(set(dead))
        if not self.group(x, y)[1]:
            self.grid[y][x] = "."
            for sx, sy in dead:  # 不会走到这：能提子就有气。留着保险
                self.grid[sy][sx] = other(color)
            raise IllegalMove("这里不能下：下了自己没有气")
        # 简单劫：提了正好一颗，且落下的这颗自己只剩一口气（就是被提的那个点）→ 对方不能马上提回
        stones, libs = self.group(x, y)
        self.ko = dead[0] if len(dead) == 1 and len(stones) == 1 and len(libs) == 1 else None
        self.captures[color] += len(dead)
        return dead

    def legal(self, color: str, x: int, y: int) -> bool:
        try:
            self.copy().play(color, x, y)
            return True
        except IllegalMove:
            return False

    def area(self) -> dict:
        """中国规则数子：子 + 只挨着一方的空（双方都挨着的是公气，不算）。死子要先拿掉再数。"""
        score = {B: 0, W: 0}
        seen = set()
        for y in range(N):
            for x in range(N):
                v = self.grid[y][x]
                if v in (B, W):
                    score[v] += 1
                elif (x, y) not in seen:
                    region, border, todo = {(x, y)}, set(), [(x, y)]
                    while todo:
                        c = todo.pop()
                        for n in neighbors(*c):
                            nv = self.at(*n)
                            if nv == "." and n not in region:
                                region.add(n)
                                todo.append(n)
                            elif nv in (B, W):
                                border.add(nv)
                    seen |= region
                    if len(border) == 1:
                        score[border.pop()] += len(region)
        return score


def replay(moves: list, handicap: list[str] | None = None) -> Board:
    """从空棋盘（加让子）按棋谱摆到最后。moves 是 [["B", "Q16"], ["W", "pass"], …]。"""
    b = Board()
    for s in handicap or []:
        x, y = to_xy(s)
        b.grid[y][x] = B
    for color, mv in moves:
        if mv.lower() == "pass":
            b.ko = None
        else:
            b.play(color, *to_xy(mv))
    return b
