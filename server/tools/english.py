"""英文书写卡：在屏幕上用四线格演示一个单词怎么写（一笔一笔），孩子可以跟着写，写错会提示。说明在 server/prompts/english.md。

字母笔画数据和判错都在前端（server/static/letters.js、english.js），这里只检查单词、带上中文意思，让界面插卡。
"""

from __future__ import annotations

import json
import re

MAX_LEN = 16
SPECS = [{"type": "function", "function": {
    "name": "show_word_writing",
    "description": "在屏幕上用英语四线格演示一个英文单词（或字母）怎么写，一笔一笔动画，孩子可以点「我来写」跟着描，写错会提示。",
    "parameters": {"type": "object", "properties": {
        "word": {"type": "string", "description": f"英文单词或字母，只用 a–z、A–Z 和空格，≤{MAX_LEN} 个字母，如「apple」「Good morning」「Bb」"},
        "meaning": {"type": "string", "description": "中文意思，如「苹果」，字母就空着"},
    }, "required": ["word"]},
}}]
NAMES = {"show_word_writing"}


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return "失败：参数不是 JSON 对象"
    word = re.sub(r"\s+", " ", str((args or {}).get("word") or "")).strip()
    letters = re.sub(r"[^A-Za-z]", "", word)
    if not letters:
        return "失败：没有英文字母，先问用户要写哪个单词"
    if len(letters) > MAX_LEN:
        return f"失败：太长了（{len(letters)} 个字母），一次最多 {MAX_LEN} 个，拆成几个词分开写"
    if re.search(r"[^A-Za-z ]", word):
        word = re.sub(r"[^A-Za-z ]", "", word).strip()
    meaning = str((args or {}).get("meaning") or "").strip()[:20]
    return (f"已在屏幕上演示「{word}」的写法（{len(letters)} 个字母），可以点「我来写」跟着写。"
            + (f"意思：{meaning}。" if meaning else "") + f"\n播报：{word} 的写法看屏幕，跟着写一写吧。")
