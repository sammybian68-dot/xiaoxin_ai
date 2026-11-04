"""围棋规则（docs/board-games.md 8.1 的 R1–R11）。"""

from __future__ import annotations

import random

import pytest

from server.games import go_rules as R
from server.games.go_rules import B, W, Board, IllegalMove


def put(b: Board, color: str, *pts: str) -> None:
    for p in pts:
        x, y = R.to_xy(p)
        b.grid[y][x] = color


def test_r1_capture_one():
    b = Board()
    put(b, W, "K10")
    put(b, B, "J10", "L10", "K11")
    dead = b.play(B, *R.to_xy("K9"))
    assert dead == [R.to_xy("K10")] and b.at(*R.to_xy("K10")) == "." and b.captures[B] == 1


def test_r2_capture_group():
    b = Board()
    put(b, W, "A1", "B1", "C1")
    put(b, B, "A2", "B2", "C2")
    assert len(b.group(*R.to_xy("A1"))[1]) == 1
    assert len(b.play(B, *R.to_xy("D1"))) == 3 and b.captures[B] == 3


def test_r3_suicide_rejected():
    b = Board()
    put(b, B, "B1", "A2")
    with pytest.raises(IllegalMove, match="没有气"):
        b.play(W, *R.to_xy("A1"))
    assert b.at(*R.to_xy("A1")) == "."


def test_r4_no_liberty_but_captures_is_ok():
    b = Board()
    put(b, B, "B1", "A2")      # 黑两子
    put(b, W, "C1", "B2", "A3")  # 白把黑两子围得只剩 A1 一口气
    dead = b.play(W, *R.to_xy("A1"))  # A1 自己没气，但提了黑
    assert sorted(dead) == sorted([R.to_xy("B1"), R.to_xy("A2")])


def test_r5_ko():
    b = Board()
    put(b, B, "D5", "E6", "F5")          # 黑三面围住白 E5
    put(b, W, "E5", "D4", "F4", "E3")    # 白三面围住 E4
    # 黑 E4 提白 E5 → 黑 E4 只剩 E5 一口气 = 劫
    assert b.play(B, *R.to_xy("E4")) == [R.to_xy("E5")]
    with pytest.raises(IllegalMove, match="打劫"):
        b.play(W, *R.to_xy("E5"))
    b.play(W, *R.to_xy("Q16"))  # 找劫材
    b.play(B, *R.to_xy("R16"))  # 黑应
    assert b.play(W, *R.to_xy("E5")) == [R.to_xy("E4")]  # 隔了手就能提回


def test_r6_occupied_and_offboard():
    b = Board()
    b.play(B, *R.to_xy("Q16"))
    with pytest.raises(IllegalMove, match="已经有子"):
        b.play(W, *R.to_xy("Q16"))
    with pytest.raises(IllegalMove):
        b.play(W, 19, 0)
    with pytest.raises(IllegalMove):
        R.to_xy("I5")  # 没有 I 列
    with pytest.raises(IllegalMove):
        R.to_xy("A20")


def test_r8_area_scoring():
    b = Board()
    # 竖着一条线把棋盘分开：黑占左边 0–8 列，白占右边 10–18 列，第 9 列（K）空着是公气
    for y in range(R.N):
        b.grid[y][8] = B
        b.grid[y][10] = W
    s = b.area()
    assert s == {B: 19 * 9, W: 19 * 9}  # 左边 8 列空 + 1 列子 = 9 列；K 列两边都挨着，不算
    b.grid[0][9] = B  # K19 黑占了，公气断开一点但依旧两边挨着
    assert b.area()[B] == 19 * 9 + 1


def test_r9_handicap_replay():
    b = R.replay([["W", "Q3"]], handicap=R.HANDICAP[4])
    assert all(b.at(*R.to_xy(p)) == B for p in R.HANDICAP[4]) and b.at(*R.to_xy("Q3")) == W


def test_r10_replay_with_capture_and_pass():
    moves = [["B", "B1"], ["W", "A1"], ["B", "A2"], ["W", "pass"]]
    b = R.replay(moves)
    assert b.at(0, 18) == "." and b.captures[B] == 1  # A1 白被提


def test_r11_coords_roundtrip():
    rnd = random.Random(1)
    for _ in range(1000):
        x, y = rnd.randrange(19), rnd.randrange(19)
        assert R.to_xy(R.to_gtp(x, y)) == (x, y)
    assert R.to_xy("A19") == (0, 0) and R.to_xy("T1") == (18, 18) and R.to_gtp(3, 3) == "D16"


def test_legal_does_not_change_board():
    b = Board()
    put(b, B, "B1", "A2")
    assert not b.legal(W, *R.to_xy("A1")) and b.legal(B, *R.to_xy("A1"))
    assert b.at(*R.to_xy("A1")) == "."
