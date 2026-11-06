"""翻唱的分享视频：模糊封面做底、转动的唱片、滚动歌词、进度条，配上翻唱的声音，竖屏 720×1280 mp4。
发到微信是一条「视频」，点开就能听。可以只发一段（起点、终点，秒）；微信聊天里的视频最长 5 分钟，超了截到 MAX_S、结尾淡出。

画面用 Pillow 一帧帧画、从管道喂给 ffmpeg 编码，放在子进程里跑（python -m server.sing.video spec.json），不占服务进程的 GIL。
做好的放在 DATA/sing/videos/<key()>.mp4，同一段只做一次。ffmpeg 用系统的，没有就用 .venv-qqmusic 里 imageio-ffmpeg 带的。
"""

from __future__ import annotations

import functools
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("xiaoxin.sing")

ROOT = Path(__file__).resolve().parents[2]
W, H = 720, 1280
FPS = 12
MAX_S = 299.0        # 微信聊天视频 ≤ 5 分钟
MIN_S = 3.0
FADE_S = 2.5
KEEP = 40            # 视频最多留几个（一首整的 10 MB 上下）
MAX_RUNNING = 2
WORKERS = 8          # 一个视频几个进程一起画帧
FONTS = {
    "bold": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc"],
    "sans": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc"],
}
_KEY = re.compile(r"^[0-9a-f]{6,40}(?:_\d+_\d+)?(?:_o-?\d+)?$")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_slots = threading.BoundedSemaphore(MAX_RUNNING)


class VideoError(ValueError):
    pass


def videos_dir() -> Path:
    from server import store

    return store.DATA / "sing" / "videos"


def ffmpeg_exe() -> str | None:
    exe = os.environ.get("XIAOXIN_FFMPEG") or shutil.which("ffmpeg")
    if exe:
        return exe
    found = sorted(glob.glob(str(ROOT / ".venv-qqmusic" / "lib" / "python*" / "site-packages" / "imageio_ffmpeg" / "binaries" / "ffmpeg*")))
    return found[0] if found else None


def span(start: float | None, end: float | None, seconds: float) -> tuple[float, float]:
    """要发的那段：没给就是整首；不够 MIN_S 报错；超过 MAX_S 从起点截 MAX_S。"""
    seconds = max(0.0, float(seconds or 0))
    a = max(0.0, float(start or 0))
    b = float(end) if end else seconds
    if seconds:
        b = min(b, seconds)
    if b - a < MIN_S:
        raise VideoError(f"选的这段太短了，至少 {MIN_S:.0f} 秒")
    return round(a, 1), round(min(b, a + MAX_S), 1)


def key(fid: str, a: float, b: float, seconds: float, off: float = 0.0) -> str:
    """<文件 id>[_起_止][_o歌词提前量]，单位 0.1 秒。"""
    k = fid if a <= 0 and (not seconds or b >= seconds - 0.05) else f"{fid}_{round(a * 10)}_{round(b * 10)}"
    return f"{k}_o{round(off * 10)}" if round(off * 10) else k


def path(k: str) -> Path | None:
    if not _KEY.match(k or ""):
        return None
    p = videos_dir() / f"{k}.mp4"
    return p if p.exists() else None


def state(k: str) -> dict:
    with _lock:
        j = dict(_jobs.get(k) or {})
    if path(k):
        return {"state": "ready", "key": k}
    if j:
        return {"key": k, **j}
    return {"state": "none", "key": k}


def ensure(fid: str, audio: Path, sing: dict, start: float | None = None, end: float | None = None, off: float = 0.0) -> dict:
    """这段视频：做好了就返回 ready；没做就起子进程去做（返回 making + 进度）；做坏了返回一次 error，之后再要就重做。"""
    seconds = float(sing.get("seconds") or 0)
    a, b = span(start, end, seconds)
    k = key(fid, a, b, seconds, off)
    st = state(k)
    if st["state"] in ("ready", "making"):
        return st
    if st["state"] == "error":   # 做坏了报一次（前端停下来说一声），再要才重做
        with _lock:
            _jobs.pop(k, None)
        return st
    if not ffmpeg_exe():
        raise VideoError("服务器上没有 ffmpeg，做不了视频")
    d = videos_dir()
    d.mkdir(parents=True, exist_ok=True)
    from server.sing import job

    cover = job.cover_path(sing.get("cover", ""))
    spec = {"audio": str(audio), "out": str(d / f"{k}.mp4"), "start": a, "end": b, "seconds": seconds,
            "cover": str(cover) if cover and cover.exists() else "", "lrc": sing.get("lrc") or "",
            "off": float(off or 0), "title": sing.get("title", ""), "artist": sing.get("artist", ""),
            "singer": sing.get("singer", "")}
    with _lock:
        if (_jobs.get(k) or {}).get("state") == "making":
            return {"key": k, **_jobs[k]}
        _jobs[k] = {"state": "making", "pct": 0}
    threading.Thread(target=_run, args=(k, spec), daemon=True, name="sing-video").start()
    return {"state": "making", "pct": 0, "key": k}


def _run(k: str, spec: dict) -> None:
    sp = videos_dir() / f"{k}.json"
    err = ""
    with _slots:
        try:
            sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            cmd = [sys.executable, "-m", "server.sing.video", str(sp)]
            if shutil.which("nice"):
                cmd = ["nice", "-n", "10", *cmd]
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in p.stdout:
                if line.startswith("pct "):
                    with _lock:
                        _jobs[k]["pct"] = int(line.split()[1])
            err = p.stderr.read()[-600:]
            ok = p.wait() == 0 and Path(spec["out"]).exists()
        except Exception as e:  # noqa: BLE001 起不来也要把状态改掉，不然一直「做视频中」
            ok, err = False, str(e)
        finally:
            sp.unlink(missing_ok=True)
    with _lock:
        if ok:
            _jobs.pop(k, None)
        else:
            log.warning("sing video %s failed: %s", k, err)
            _jobs[k] = {"state": "error", "error": "视频没做出来"}
    if ok:
        _prune()


def _prune() -> None:
    vids = sorted(videos_dir().glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in vids[KEEP:]:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 画（子进程里跑）

@functools.lru_cache(maxsize=64)
def _font(kind: str, size: int):
    from PIL import ImageFont

    for f in FONTS[kind]:
        if Path(f).is_file():
            return ImageFont.truetype(f, size, index=2 if "CJK" in f else 0)
    return ImageFont.load_default(size)


def _fit(text: str, kind: str, size: int, width: int):
    """一行放不下就把字缩小，最小缩到 60%。"""
    for s in range(size, int(size * 0.6) - 1, -2):
        f = _font(kind, s)
        if f.getlength(text) <= width:
            return f
    return _font(kind, int(size * 0.6))


def _text(text: str, font, fill) -> "Image.Image":
    from PIL import Image, ImageDraw

    l, t, r, b = font.getbbox(text)
    im = Image.new("RGBA", (max(1, r - l + 8), max(1, b - t + 8)), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((4 - l, 4 - t), text, font=font, fill=fill)
    return im


def _background(cover: str) -> "Image.Image":
    from PIL import Image, ImageDraw, ImageFilter

    if cover:
        src = Image.open(cover).convert("RGB")
        s = max(W / src.width, H / src.height) * 1.15
        src = src.resize((math.ceil(src.width * s), math.ceil(src.height * s)), Image.LANCZOS)
        x, y = (src.width - W) // 2, (src.height - H) // 2
        bg = src.crop((x, y, x + W, y + H)).filter(ImageFilter.GaussianBlur(42))
    else:
        bg = Image.new("RGB", (W, H), (58, 46, 92))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=(12, 10, 20, int(120 + 90 * (y / H))))
    return Image.alpha_composite(bg.convert("RGBA"), shade)


def _disc(cover: str, r: int) -> "Image.Image":
    from PIL import Image, ImageDraw, ImageOps

    n = r * 2
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, n - 1, n - 1), fill=(18, 18, 20, 255))
    for i, rr in enumerate(range(r - 8, int(r * 0.66), -6)):
        g = 34 + (i % 3) * 6
        d.ellipse((r - rr, r - rr, r + rr, r + rr), outline=(g, g, g + 2, 255), width=1)
    d.arc((14, 14, n - 15, n - 15), 200, 250, fill=(90, 90, 96, 255), width=3)   # 一道反光，转起来才看得出在转
    art = int(r * 0.64)
    if cover:
        c = ImageOps.fit(Image.open(cover).convert("RGB"), (art * 2, art * 2), Image.LANCZOS)
    else:
        c = Image.new("RGB", (art * 2, art * 2), (120, 96, 170))
    mask = Image.new("L", c.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, art * 2 - 1, art * 2 - 1), fill=255)
    im.paste(c, (r - art, r - art), mask)
    d.ellipse((r - 9, r - 9, r + 9, r + 9), fill=(18, 18, 20, 255))
    return im


def _clock(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


class _Scene:
    """一个视频的画面：不变的部分（底图、唱片、渐隐蒙版）先做好，frame(n) 画第 n 帧（rgb24 字节）。"""

    disc_r, disc_c = 210, (W // 2, 500)
    ly_top, ly_h, lh = 780, 330, 62
    bar_y, bar_x0, bar_x1 = 1160, 90, W - 90

    def __init__(self, spec: dict):
        from PIL import Image, ImageDraw

        from server.sing.practice import parse_lrc

        self.a, b = float(spec["start"]), float(spec["end"])
        self.dur = b - self.a
        cover = spec.get("cover") or ""
        self.lines = [(t - float(spec.get("off") or 0), w) for t, w in parse_lrc(spec.get("lrc") or "")]

        base = _background(cover)
        title = f"《{spec.get('title') or '这首歌'}》"
        t_im = _text(title, _fit(title, "bold", 58, W - 80), (255, 255, 255, 255))
        base.alpha_composite(t_im, ((W - t_im.width) // 2, 96))
        sub = f"{spec.get('singer') or '家里人'} 的 AI 翻唱" + (f" · 原唱 {spec['artist']}" if spec.get("artist") else "")
        s_im = _text(sub, _fit(sub, "sans", 30, W - 100), (255, 255, 255, 190))
        base.alpha_composite(s_im, ((W - s_im.width) // 2, 96 + t_im.height + 14))
        mark = _text("小新 · AI 翻唱", _font("sans", 22), (255, 255, 255, 120))
        base.alpha_composite(mark, ((W - mark.width) // 2, H - 66))
        self.base = base
        self.disc = _disc(cover, self.disc_r)
        self.ly_mid = self.ly_h // 2 - 26
        self.cache = {}
        self.fade = Image.new("L", (W, self.ly_h), 0)
        fd = ImageDraw.Draw(self.fade)
        for y in range(self.ly_h):
            e = min(y, self.ly_h - 1 - y) / 70
            fd.line([(0, y), (W, y)], fill=int(255 * min(1.0, e)))
        self.f_clock = _font("sans", 22)

    def line_im(self, i: int, cur: bool):
        if (i, cur) not in self.cache:
            f = _fit(self.lines[i][1], "bold" if cur else "sans", 40 if cur else 31, W - 80)
            self.cache[i, cur] = _text(self.lines[i][1], f, (255, 255, 255, 255) if cur else (255, 255, 255, 140))
        return self.cache[i, cur]

    def frame(self, n: int) -> bytes:
        from PIL import Image, ImageChops, ImageDraw

        lines, lh = self.lines, self.lh
        vt = n / FPS
        st = self.a + vt
        fr = self.base.copy()
        rot = self.disc.rotate(-(st * 360 / 18) % 360, resample=Image.BICUBIC)
        fr.alpha_composite(rot, (self.disc_c[0] - self.disc_r, self.disc_c[1] - self.disc_r))

        if lines:
            idx = -1
            for i, (t, _) in enumerate(lines):
                if t <= st:
                    idx = i
                else:
                    break
            pos = float(idx)
            if idx >= 0 and st - lines[idx][0] < 0.35:
                x = (st - lines[idx][0]) / 0.35
                pos = idx - 1 + (1 - (1 - x) ** 3)
            layer = Image.new("RGBA", (W, self.ly_h), (0, 0, 0, 0))
            for j in range(max(0, int(pos) - 3), min(len(lines), int(pos) + 5)):
                y = self.ly_mid + round((j - pos) * lh)
                if -lh < y < self.ly_h:
                    im = self.line_im(j, j == idx)
                    layer.paste(im, ((W - im.width) // 2, y - (im.height - 40) // 2))   # 行距比字高，不会叠
            layer.putalpha(ImageChops.multiply(layer.getchannel("A"), self.fade))
            fr.alpha_composite(layer, (0, self.ly_top))

        d = ImageDraw.Draw(fr)
        x0, x1, by = self.bar_x0, self.bar_x1, self.bar_y
        d.rounded_rectangle((x0, by, x1, by + 6), 3, fill=(255, 255, 255, 60))
        px = x0 + (x1 - x0) * min(1.0, vt / self.dur)
        d.rounded_rectangle((x0, by, px, by + 6), 3, fill=(255, 255, 255, 230))
        d.ellipse((px - 8, by - 5, px + 8, by + 11), fill=(255, 255, 255, 255))
        d.text((x0, by + 18), _clock(vt), font=self.f_clock, fill=(255, 255, 255, 170))
        tot = _clock(self.dur)
        d.text((x1 - self.f_clock.getlength(tot), by + 18), tot, font=self.f_clock, fill=(255, 255, 255, 170))
        return fr.convert("RGB").tobytes()


_scene: _Scene | None = None


def _scene_init(spec: dict) -> None:
    global _scene
    _scene = _Scene(spec)


def _scene_frame(n: int) -> bytes:
    return _scene.frame(n)


def render(spec: dict, progress=None) -> None:
    """一帧要 20 ms（大半是转唱片），单进程画整首要一分钟；分给几个进程一起画，按顺序喂给 ffmpeg。"""
    import multiprocessing

    a, b = float(spec["start"]), float(spec["end"])
    dur = b - a
    seconds = float(spec.get("seconds") or 0)
    ff = [spec.get("ffmpeg") or ffmpeg_exe(), "-y", "-loglevel", "error",
          "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
          "-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", spec["audio"], "-map", "0:v", "-map", "1:a"]
    af = []
    if a > 0.5:
        af.append("afade=t=in:st=0:d=0.4")
    if not seconds or b < seconds - 0.5:
        af.append(f"afade=t=out:st={max(0.0, dur - FADE_S):.2f}:d={FADE_S}")
    if af:
        ff += ["-af", ",".join(af)]
    out = Path(spec["out"])
    part = out.with_name(out.stem + ".part.mp4")
    # 手机经公网那台机器下载只有 0.2～0.8 MB/s，视频越小越快发出去：medium + crf30 + 关键帧 10 秒一个，比 veryfast/crf28
    # 小 22%，编码不慢，歌词照样清楚（2026-11-07 实测 81 秒那段 3.14 → 2.44 MB）。翻唱本来就是 24 kHz 单声道，声音给 64k 单声道够了
    ff += ["-c:v", "libx264", "-preset", "medium", "-crf", "30", "-pix_fmt", "yuv420p", "-profile:v", "main",
           "-g", str(FPS * 10), "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", "-shortest", str(part)]
    frames = max(1, math.ceil(dur * FPS))
    workers = min(WORKERS, os.cpu_count() or 1, max(1, frames // (FPS * 2)))
    pool = None
    if workers > 1:
        ctx = multiprocessing.get_context("fork" if sys.platform == "linux" else "spawn")
        pool = ctx.Pool(workers, initializer=_scene_init, initargs=(spec,))
        it = pool.imap(_scene_frame, range(frames), chunksize=FPS)
    else:
        scene = _Scene(spec)
        it = map(scene.frame, range(frames))
    p = subprocess.Popen(ff, stdin=subprocess.PIPE)
    last = -1
    try:
        for n, fr in enumerate(it):
            p.stdin.write(fr)
            pct = n * 100 // frames
            if progress and pct != last:
                progress(pct)
                last = pct
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg exit {p.returncode}")
        part.replace(out)
    finally:
        if pool:
            pool.terminate()
        if p.poll() is None:
            p.kill()
        part.unlink(missing_ok=True)


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    render(spec, lambda pct: print(f"pct {pct}", flush=True))
