"""小新卷子的照片 → 拉正的 A4：读四角二维码（OpenCV），认出是哪份卷子第几页，透视拉正成和原卷一样的 1654×2339。

全是确定的计算，不用模型（家长 2026-11-05：读不到码就让孩子重拍，不要大模型兜底认卷子）：
  - 合格：任意一组对角（左上+右下，或右上+左下）的码都读到，且都是同一份卷子、同一页。
  - 还要拉正后整张 A4 都在照片里（四个角映射回去在画面内）；有一块在外面，那块的题会当成没写，也让重拍。
二维码内容和位置见 server/worksheet.py（qr_text、CORNERS）。每个码 OpenCV 给 4 个角点（按码自己的方向，倒拍也对），
两个码就有 8 个点，findHomography 算透视。
"""

from __future__ import annotations

import re

from server import worksheet

# 模拟 120 张（tests/test_check_work.py 的 _photo）：纸整张在画面里的 109 张，100 张合格；
# 要重拍的 9 张都是纸只占画面六七成、又糊的（码一个模块不到 4 像素）。所以话里说「靠近一点」。
RETAKE = "请把卷子拍完整、拍清楚：四个角上的二维码都要拍进来，别挡住；靠近一点，让纸差不多占满画面。"
MIXED = "这张照片里的二维码不是同一张卷子的，请一张一张拍。"
CODE = re.compile(r"^XX\.([0-9A-F]{8})\.(\d+)\.([1-4])$")


class PaperError(ValueError):
    """照片不合格：说给孩子的话就是 str(e)。"""


def _run(det, img, sx: float = 1.0, ox: int = 0, oy: int = 0) -> dict:
    import cv2

    try:
        ok, txt, pts, _ = det.detectAndDecodeMulti(img)
    except cv2.error:
        return {}
    out = {}
    if ok:
        for t, p in zip(txt, pts):
            if t and CODE.match(t):
                out[t] = p / sx + [ox, oy]
    return out


def _enough(found: dict) -> bool:
    c = {t.rsplit(".", 1)[1] for t in found}
    return len(c) == 4


def detect(img) -> dict:
    """读照片上的小新二维码：{内容: 4×2 角点}。几趟：整图；灰度增强对比；四个角各切一块放大 2 倍（码小、糊的时候）。
    四个码都读到就不再试（一趟 ~0.05 秒，最多 ~0.3 秒）。"""
    import cv2

    det = cv2.QRCodeDetectorAruco()   # 比普通 QRCodeDetector 稳得多（模拟手机照 2/8 → 6/8，见 worksheet.QR_MOD）
    found = _run(det, img)
    if not _enough(found):
        g = cv2.createCLAHE(2.0, (8, 8)).apply(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        for t, p in _run(det, g).items():
            found.setdefault(t, p)
    if not _enough(found):
        h, w = img.shape[:2]
        ch, cw = int(h * 0.55), int(w * 0.55)
        for y0, x0 in ((0, 0), (0, w - cw), (h - ch, 0), (h - ch, w - cw)):
            c = cv2.resize(img[y0:y0 + ch, x0:x0 + cw], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            for t, p in _run(det, c, 2.0, x0, y0).items():
                found.setdefault(t, p)
            if _enough(found):
                break
    return found


def _paper_points(corner: int):
    """第 corner 个角（1 左上 2 右上 3 右下 4 左下）的码在 A4 上的 4 个角点（码自己的左上、右上、右下、左下）。"""
    x, y = worksheet.CORNERS[corner - 1]
    q = worksheet.QR
    return [[x, y], [x + q, y], [x + q, y + q], [x, y + q]]


def locate(img) -> dict:
    """认卷子 + 算透视。合格返回 {short, page, corners, M}（M：照片 → A4 的 3×3 矩阵）；不合格抛 PaperError。"""
    import cv2
    import numpy as np

    found = detect(img)
    if not found:
        raise PaperError(RETAKE)
    sheets = {}
    for t, p in found.items():
        short, page, corner = CODE.match(t).groups()
        sheets.setdefault((short, int(page)), {})[int(corner)] = p
    # 同一张照片里有两份 / 两页的码：取码多的那份；那份自己不成对角才说混了
    (short, page), codes = max(sheets.items(), key=lambda kv: len(kv[1]))
    have = set(codes)
    if not ({1, 3} <= have or {2, 4} <= have):
        raise PaperError(MIXED if len(sheets) > 1 and sum(len(c) for c in sheets.values()) >= 2 else RETAKE)
    src = np.float32([pt for c in sorted(codes) for pt in codes[c]])
    dst = np.float32([pt for c in sorted(codes) for pt in _paper_points(c)])
    M, _ = cv2.findHomography(src, dst, 0)
    if M is None:
        raise PaperError(RETAKE)
    # 整张 A4 在不在照片里：A4 四角映射回照片（留 1% 余量）
    h, w = img.shape[:2]
    inv = np.linalg.inv(M)
    a4 = np.float32([[0, 0], [worksheet.W, 0], [worksheet.W, worksheet.H], [0, worksheet.H]]).reshape(-1, 1, 2)
    back = cv2.perspectiveTransform(a4, inv).reshape(-1, 2)
    mx, my = 0.01 * w, 0.01 * h
    if any(not (-mx <= x <= w + mx and -my <= y <= h + my) for x, y in back):
        raise PaperError(RETAKE)
    return {"short": short, "page": page, "corners": sorted(codes), "M": M}


def deskew(img, M):
    """按 locate 的矩阵把照片拉成 A4 正视图（1654×2339，BGR）。"""
    import cv2

    return cv2.warpPerspective(img, M, (worksheet.W, worksheet.H), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def read(path) -> "numpy.ndarray":
    """读照片（按 EXIF 转正），BGR。"""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        return np.asarray(im)[:, :, ::-1].copy()
