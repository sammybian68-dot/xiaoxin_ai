"""文生图 / 以图改图，走同一个代理，默认 gpt-image-2。

给了参考图走 /v1/images/edits（multipart），没给走 /v1/images/generations。
参考图先在本机缩到最长边 1024 再传：原图几 MB，上传慢，模型也用不到那么高的分辨率。
gpt-image-2 出一张 high 质量的图要几分钟，超时默认 600 秒。
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path

import requests

from src.llm import client

DEFAULT_IMAGE_MODEL = "gpt-image-2"


def _shrink(src: Path, longest: int = 1024) -> bytes:
    """转 PNG、按 EXIF 摆正、最长边缩到 longest。gpu2 是 Linux 没有 sips，以前就原样传了几 MB 的原图。"""
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((longest, longest))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def _post(base: str, key: str, body: dict, refs: list, timeout: float) -> requests.Response:
    headers = {"Authorization": f"Bearer {key}"}
    if not refs:
        return requests.post(f"{base}/v1/images/generations", headers=headers, json=body, timeout=timeout)
    field = "image[]" if len(refs) > 1 else "image"
    files = [(field, (f"ref{i}.png", _shrink(Path(p)), "image/png")) for i, p in enumerate(refs)]
    data = {k: str(v) for k, v in body.items()}
    return requests.post(f"{base}/v1/images/edits", headers=headers, files=files, data=data, timeout=timeout)


def generate(
    prompt: str,
    out: str | Path,
    *,
    ref: str | Path | list | None = None,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = "1024x1024",
    quality: str | None = None,
    background: str | None = None,
    timeout: float = 600,
) -> Path:
    """出一张图写到 out，返回路径。ref 是参考图（一张或多张），有就走 edits。"""
    key, base = client.config()
    refs = ref if isinstance(ref, list) else ([ref] if ref else [])
    body = {"model": model, "prompt": prompt, "size": size, "n": 1}
    body.update({k: v for k, v in {"quality": quality, "background": background}.items() if v})
    t0 = time.monotonic()
    try:
        r = _post(base, key, body, refs, timeout)
    except requests.RequestException as e:
        raise client.ProxyError(f"{model} {time.monotonic() - t0:.0f}s: {e!r}") from e
    if r.status_code != 200:
        raise client.ProxyError(f"HTTP {r.status_code}: {r.text[:500]}")
    item = r.json()["data"][0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        data = requests.get(item["url"], timeout=timeout).content
    else:
        raise client.ProxyError(f"no image in response: {str(item)[:300]}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"{model} {time.monotonic() - t0:.1f}s -> {out}", flush=True)
    return out
