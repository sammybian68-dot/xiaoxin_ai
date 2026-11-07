"""翻唱的工具：record_singing_voice（屏幕上弹录音界面，录谁唱歌的声音）、sing_song（用谁的声音唱哪首，后台做）。
说明在 server/prompts/sing.md，后台流程在 server/sing/job.py，界面在 static/sing.js（按工具结果里的 [sing-rec:名字] 弹录音）。"""

from __future__ import annotations

import json

from server.sing import job, qq, voices
from server.tools import printer as printer_tools

SPECS = [
    {"type": "function", "function": {
        "name": "record_singing_voice",
        "description": "录某个人唱歌的声音（以后能用这个声音唱任何歌）：屏幕上弹出录音界面：挑一首歌（儿歌、流行、老歌）跟着歌词唱、能先听原唱，30 秒倒计时。"
                       "「我是爸爸，录一下我唱歌的声音」「帮妈妈录唱歌的声音」",
        "parameters": {"type": "object", "properties": {
            "singer": {"type": "string", "description": "是谁的声音，用他的称呼（「爸爸」「卞依一」）；说「我」没报名字就不填"},
        }},
    }},
    {"type": "function", "function": {
        "name": "sing_song",
        "description": "用家里某个人录过的唱歌声音唱一首歌（AI 翻唱）：在后台从 QQ 音乐拿原曲换成他的声音，大约一分钟，"
                       "好了在聊天里出一张能播放、带歌词的音乐卡片。「用爸爸的声音唱江南」",
        "parameters": {"type": "object", "properties": {
            "singer": {"type": "string", "description": "用谁的声音（「爸爸」「卞依一」）；说「用我的声音」就不填"},
            "song": {"type": "string", "description": "歌名，只写歌名（「江南」「孤勇者」）"},
            "artist": {"type": "string", "description": "原唱，说了才填（「林俊杰的江南」填 林俊杰）"},
        }, "required": ["song"]},
    }},
    {"type": "function", "function": {
        "name": "sing_list",
        "description": "AI 翻唱过的歌：聊天里出一张歌单卡片（按谁的声音分开，点一首就放，歌多了能上下翻）。"
                       "「把爸爸翻唱的歌都列出来」「我唱过哪些歌」「打开歌单」都用这个，不要自己翻聊天记录数。",
        "parameters": {"type": "object", "properties": {
            "singer": {"type": "string", "description": "只看谁的声音唱的（「爸爸」；「我唱过的」填 我）；看全部不填"},
        }},
    }},
]
NAMES = {"record_singing_voice", "sing_song", "sing_list"}
LIST_SAY = 8   # 工具结果里给模型念的歌名最多几首
_ME = {"", "我", "自己", "我自己", "我的"}


def _ctx() -> dict:
    return printer_tools.ctx.get()


def _speaker() -> dict | None:
    """这句话是谁说的（这台设备的主人）。管理员登的就当是线程的孩子。"""
    from server.family import members

    c = _ctx()
    for mid in (c.get("by"), c.get("kid")):
        if mid and mid != "admin":
            try:
                return members.get(mid)
            except KeyError:
                continue
    return None


def _who(raw: str) -> str:
    name = voices.clean(raw)
    if name in _ME:
        p = _speaker()
        return (p.get("call") or p["name"]) if p else ""
    return name


def _kid() -> dict:
    from server.family import members

    c = _ctx()
    try:
        return members.get(c["kid"]) if c.get("kid") else {"name": c.get("kid_name", "")}
    except KeyError:
        return {"name": c.get("kid_name", "")}


def _threads(thread: str) -> list[str]:
    """和 /api/sing/songs 一样的范围：家长、管理员说的看全家，孩子看自己这条线程。"""
    from server.family import members

    by = _ctx().get("by")
    if by == "admin" or (_speaker() or {}).get("role") == "parent":
        return [k["thread"] for k in members.kids(active_only=False) if k.get("thread")] or [thread]
    return [thread]


def _list(thread: str, singer: str) -> str:
    songs = job.library(_threads(thread))
    if not songs:
        return "还没有唱好的歌。[sing-list:]\n播报：还没有翻唱过的歌呢，说「用爸爸的声音唱江南」就能唱一首。"
    by: dict[str, list[str]] = {}
    for s in songs:
        by.setdefault(s["singer"], []).append(s["title"])
    pick = singer if singer in by else ""
    sums = "；".join(f"{n} {len(t)} 首：{'、'.join(t[:LIST_SAY])}{' 等' if len(t) > LIST_SAY else ''}" for n, t in by.items())
    if singer and not pick:
        head = f"「{singer}」还没有唱过的歌，卡片上列的是全部。"
        say = f"{singer}还没有翻唱过的歌，我把全部的列出来了，点一首就能听。"
    else:
        n = len(by[pick]) if pick else len(songs)
        head = f"歌单卡片已经发在聊天里（{'只看' + pick if pick else '全部'}，{n} 首）。"
        say = f"{pick + '翻唱的' if pick else '翻唱过的'} {n} 首歌都列在屏幕上了，点一首就能听。"
    return f"{head}{sums}。[sing-list:{pick}]\n播报：{say}"


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    thread = _ctx().get("thread", "")
    if not thread:
        return "失败：这里不能唱歌（不在聊天里）"
    raw = str(args.get("singer") or "").strip()
    if name == "sing_list":
        return _list(thread, _who(raw) if raw else "")
    singer = _who(raw)
    if name == "record_singing_voice":
        if not singer:
            return "失败：不知道是谁的声音，先问一句「你是谁呀」"
        again = "（之前录过，这次录好会换掉旧的）" if voices.find(singer) else ""
        return (f"屏幕上打开了录音界面，录「{singer}」唱歌的声音{again}。[sing-rec:{singer}]\n"
                f"播报：好的，在屏幕右边挑一首会唱的歌，跟着歌词唱，不会起调可以先听一遍原唱。准备好了点「开始录」，唱满三十秒。")
    song = str(args.get("song") or "").strip().strip("《》")[:40]
    if not song:
        return "失败：没说唱哪首歌，先问一句"
    if not singer:
        return f"失败：不知道用谁的声音。录过声音的有：{voices.names()}"
    voice = voices.find(singer)
    if not voice:
        return (f"失败：还没有「{singer}」唱歌的声音（录过的有：{voices.names()}）。\n"
                f"播报：还没有{singer}唱歌的声音呢，先说「小新，我是{singer}，录一下我唱歌的声音」。")
    if not job.service_up():
        return "失败：唱歌的服务没开（gpu2 上的 SoulX-Singer），等会儿再试\n播报：唱歌的机器现在没开，等会儿再试试吧。"
    if not qq.usable():
        return ("失败：QQ 音乐没登录或登录过期了，拿不到原曲\n"
                "播报：QQ 音乐要重新登录一下，请爸爸妈妈在设置里的「唱歌」扫码登录。")
    try:
        info = job.pick(song, str(args.get("artist") or "").strip()[:40])
    except (job.SingError, qq.QQError) as e:
        return f"失败：{e}"
    if not job.start(thread, _kid(), voice, info):
        return "失败：手上已经有两首在唱了，等唱好再点\n播报：我手上已经有两首歌在唱了，等它们好了再点吧。"
    m, s = divmod(int(info["seconds"]), 60)
    return (f"已开始用{voice['name']}的声音唱《{info['title']}》（原唱 {info['artist']}，{m} 分 {s} 秒），"
            f"大约一分钟，唱好会发到聊天里。\n播报：好，我用{voice['name']}的声音唱《{info['title']}》，大概一分钟。")
