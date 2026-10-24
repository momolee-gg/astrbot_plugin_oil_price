"""PIL 渲染：油价卡片与折线图。仅依赖 constants、reporting 与标准库。

所有 render_*_card 返回已保存 PNG 路径；line_chart_image 返回 PIL Image，
由调用方决定存盘（推送）还是转 base64（WebUI）。

每日卡片为海报引擎：10 套样式按「ISO 周 ×7 + 星期」轮换，
版式统一（描边大标题 / 地点胶囊 / 油号色块 / 涨跌胶囊 / 毛边横幅 / 倒计时条 / 底部波浪）。
"""

import io
import math
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .constants import (
    C_BG,
    C_GREEN,
    C_L_BG,
    C_L_LINE,
    C_L_MAIN,
    C_L_PANEL,
    C_L_SUB,
    C_LINE,
    C_MAIN,
    C_PANEL,
    C_RED,
    C_SUB,
    FONT_PATH,
    FUEL_NAMES,
    FUEL_ORDER,
    PALETTE,
    PALETTE_LIGHT,
)
from .reporting import _days_until, rise_hint_text

SCALE = 2  # 卡片与折线图超采样倍率（抗锯齿）


def _font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _diff(ch) -> tuple:
    if ch is None:
        return "—", C_SUB
    if ch > 0:
        return f"▲{ch:.2f}", C_RED
    if ch < 0:
        return f"▼{abs(ch):.2f}", C_GREEN
    return "持平", C_SUB


def _save(img: Image.Image, tmp_dir: str, name: str) -> str:
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, name)
    img.save(path, "PNG")
    return path


def save_image(img: Image.Image, tmp_dir: str, name: str) -> str:
    """公开接口：将任意渲染结果（如折线图）保存为 PNG。"""
    return _save(img, tmp_dir, name)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _active_fuels(data: dict) -> list:
    fuels = data.get("fuels") or {}
    return [k for k in FUEL_ORDER if (fuels.get(k) or {}).get("price")]


# ==================== 通用绘制工具 ====================


def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list:
    """按像素宽度逐字换行（中文场景）。"""
    lines, cur = [], ""
    for ch in text:
        if cur and d.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _pill(
    d: ImageDraw.ImageDraw,
    x: int,
    cy: int,
    text: str,
    font,
    bg: tuple,
    fg: tuple,
    pad_h: int = 24,
    align_right: int = None,
) -> int:
    """圆角胶囊。给 x 为左对齐起点；给 align_right 则右对齐。返回占用宽度。"""
    tw = int(d.textlength(text, font=font))
    pw, ph = tw + pad_h * 2, font.size + 20
    x0 = x if align_right is None else align_right - pw
    d.rounded_rectangle((x0, cy - ph // 2, x0 + pw, cy + ph // 2), radius=ph // 2, fill=bg)
    d.text((x0 + pad_h, cy - font.size // 2 - 2), text, font=font, fill=fg)
    return pw


def _grad_rounded(img: Image.Image, box: tuple, radius: int, top: tuple, bottom: tuple):
    """竖直渐变圆角面板（带圆角蒙版 paste 到目标图层）。坐标为已缩放值。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    grad = Image.new("RGB", (max(1, w), max(1, h)))
    gd = ImageDraw.Draw(grad)
    for i in range(h):
        t = i / max(1, h - 1)
        col = tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
        gd.line((0, i, w, i), fill=col)
    mask = Image.new("L", (max(1, w), max(1, h)), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    img.paste(grad, (x0, y0), mask)


def _bg_gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (max(1, w), max(1, h)))
    gd = ImageDraw.Draw(img)
    for i in range(h):
        t = i / max(1, h - 1)
        gd.line(
            (0, i, w, i),
            fill=tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3)),
        )
    return img


def _panel(
    d: ImageDraw.ImageDraw, box: tuple, radius: int, fill, line=None, width: int = 1
):
    d.rounded_rectangle(
        box, radius=radius, fill=fill, outline=line, width=width if line else 0
    )


def _mix(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))


def _with_shadow(
    img: Image.Image,
    shape_fn,
    blur: int = 12,
    dy: int = 10,
    alpha: int = 55,
    color=(20, 28, 45),
):
    """在 img 上为 shape_fn 画出的形状（坐标已缩放）添加柔和投影。"""
    lay = Image.new("L", img.size, 0)
    shape_fn(ImageDraw.Draw(lay))
    lay = lay.filter(ImageFilter.GaussianBlur(blur))
    mask = lay.point(lambda p: min(255, p * alpha // 255))
    img.paste(Image.new("RGB", img.size, color), (0, dy), mask)


def _text_sh(
    d: ImageDraw.ImageDraw,
    xy: tuple,
    text: str,
    font,
    fill: tuple,
    stroke: int = 0,
    stroke_fill=None,
    sh_dy: int = 4,
    sh_fill=(35, 38, 52),
):
    """带投影的文字。"""
    x, y = xy
    d.text(
        (x, y + sh_dy),
        text,
        font=font,
        fill=sh_fill,
        stroke_width=stroke,
        stroke_fill=sh_fill,
    )
    d.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke,
        stroke_fill=stroke_fill or fill,
    )


def _ribbon_mask(dr: ImageDraw.ImageDraw, x0: int, x1: int, cy: int, h: int):
    """丝带剪影（投影 mask 用）：中带 + 两侧燕尾。坐标已缩放。"""
    bx0, bx1 = x0 + int(h * 0.5), x1 - int(h * 0.5)
    th = int(h * 0.6)
    dr.rounded_rectangle(
        (bx0, cy - h // 2, bx1, cy + h // 2), radius=int(h * 0.12), fill=255
    )
    for side in (-1, 1):
        ax = bx0 if side < 0 else bx1
        tip = ax + side * int(h * 0.5)
        notch = int(h * 0.18)
        dr.polygon(
            [
                (ax, cy - th // 2),
                (tip, cy - th // 2),
                (tip - side * notch, cy),
                (tip, cy + th // 2),
                (ax, cy + th // 2),
            ],
            fill=255,
        )


def _ribbon(
    img: Image.Image,
    d: ImageDraw.ImageDraw,
    x0: int,
    x1: int,
    cy: int,
    h: int,
    color: tuple,
    dark: tuple,
):
    """丝带标题带：燕尾飘带 + 折角 + 渐变中带 + 柔和投影。坐标已缩放。"""
    _with_shadow(img, lambda m: _ribbon_mask(m, x0, x1, cy, h), blur=14, dy=12, alpha=70)
    bx0, bx1 = x0 + int(h * 0.5), x1 - int(h * 0.5)
    th = int(h * 0.6)
    for side in (-1, 1):
        ax = bx0 if side < 0 else bx1
        tip = ax + side * int(h * 0.5)
        notch = int(h * 0.18)
        d.polygon(
            [
                (ax, cy - th // 2),
                (tip, cy - th // 2),
                (tip - side * notch, cy),
                (tip, cy + th // 2),
                (ax, cy + th // 2),
            ],
            fill=dark,
        )
    for side in (-1, 1):
        ax = bx0 if side < 0 else bx1
        d.polygon(
            [
                (ax, cy - th // 2),
                (ax - side * int(h * 0.16), cy + th // 2),
                (ax, cy + th // 2),
            ],
            fill=dark,
        )
    _grad_rounded(
        img,
        (bx0, cy - h // 2, bx1, cy + h // 2),
        int(h * 0.12),
        _mix(color, (255, 255, 255), 0.16),
        _mix(color, (0, 0, 0), 0.18),
    )


def _banner_block(
    img: Image.Image,
    d: ImageDraw.ImageDraw,
    box: tuple,
    radius: int,
    top: tuple,
    bottom: tuple,
):
    """带投影的渐变圆角横幅块。坐标已缩放。"""
    x0, y0, x1, y1 = box
    _with_shadow(
        img,
        lambda m: m.rounded_rectangle(box, radius=radius, fill=255),
        blur=12,
        dy=10,
        alpha=60,
    )
    _grad_rounded(img, box, radius, top, bottom)


def _lightning(d: ImageDraw.ImageDraw, cx: int, cy: int, h: int, color):
    """自绘小闪电图标（CJK 字体无 emoji 字形）。"""
    w = h * 0.62
    x0, y0 = cx - w / 2, cy - h / 2
    pts = [
        (0.62, 0.0),
        (0.10, 0.56),
        (0.42, 0.56),
        (0.30, 1.0),
        (0.90, 0.40),
        (0.55, 0.40),
        (0.82, 0.0),
    ]
    d.polygon([(x0 + px * w, y0 + py * h) for px, py in pts], fill=color)


def _brush_rect(d: ImageDraw.ImageDraw, x0, y0, x1, y1, color, seed=1, jitter=4):
    """毛边矩形（海报笔刷块）：四边带轻微锯齿。坐标为已缩放值。"""
    import random

    rng = random.Random(seed)
    seg = 7

    def edge(ax, ay, bx, by, reverse=False):
        pts = []
        for i in range(seg + 1):
            t = i / seg
            j = rng.uniform(-jitter, jitter)
            if reverse:
                pts.append((bx + (ax - bx) * t, by + (ay - by) * t + j))
            else:
                pts.append((ax + (bx - ax) * t, ay + (by - ay) * t + j))
        return pts

    pts = (
        edge(x0, y0, x1, y0)
        + edge(x1, y0, x1, y1)
        + edge(x1, y1, x0, y1)
        + edge(x0, y1, x0, y0, reverse=True)
    )
    d.polygon(pts, fill=color)


def _calendar_icon(d: ImageDraw.ImageDraw, x: int, y: int, s: int, color):
    """自绘日历图标：s 为边长（已缩放）。"""
    d.rounded_rectangle((x, y + s * 0.12, x + s, y + s), radius=int(s * 0.14), fill=color)
    for i in (0.18, 0.66):
        d.rounded_rectangle(
            (x + s * i, y, x + s * (i + 0.16), y + s * 0.28),
            radius=int(s * 0.07),
            fill=color,
        )
    for r in range(2):
        for c in range(3):
            cx = x + s * (0.18 + c * 0.26)
            cy = y + s * (0.42 + r * 0.24)
            rr = max(1, int(s * 0.055))
            d.ellipse(
                (cx - rr, cy - rr, cx + rr, cy + rr),
                fill=(255, 255, 255) if sum(color) < 400 else color,
            )


def _decor(d: ImageDraw.ImageDraw, st: dict, W: int, H: int, sw):
    """背景装饰：sparkle 星星 / clouds 云朵 / pin 图钉 / dots 网点 / frame 发光框。"""
    kind = st["decor"]
    if kind == "sparkle":
        for fx, fy, s, col in (
            (0.10, 0.045, 14, st["title_hi"]),
            (0.86, 0.03, 11, st["band"]),
            (0.93, 0.10, 8, st["title_hi"]),
            (0.05, 0.11, 9, st["band"]),
        ):
            cx, cy, r = int(W * fx), int(H * fy), sw(s)
            d.polygon(
                [
                    (cx, cy - r),
                    (cx + r * 0.3, cy - r * 0.3),
                    (cx + r, cy),
                    (cx + r * 0.3, cy + r * 0.3),
                    (cx, cy + r),
                    (cx - r * 0.3, cy + r * 0.3),
                    (cx - r, cy),
                    (cx - r * 0.3, cy - r * 0.3),
                ],
                fill=col,
            )
    elif kind == "clouds":
        for fx, fy, s in ((0.80, 0.05, 1.0), (0.12, 0.09, 0.7), (0.9, 0.16, 0.55)):
            cx, cy, r = int(W * fx), int(H * fy), int(sw(34) * s)
            for dx, dy, rr in (
                (0, 0, r),
                (-r * 0.8, r * 0.25, r * 0.7),
                (r * 0.8, r * 0.25, r * 0.7),
                (r * 0.3, -r * 0.4, r * 0.6),
            ):
                d.ellipse(
                    (
                        cx + dx - rr,
                        cy + dy - rr * 0.8,
                        cx + dx + rr,
                        cy + dy + rr * 0.8,
                    ),
                    fill=(255, 255, 255),
                )
    elif kind == "pin":
        cx, cy = W // 2, sw(26)
        d.ellipse(
            (cx - sw(20), cy - sw(10) + sw(8), cx + sw(20), cy + sw(26)), fill=(0, 0, 0)
        )
        d.ellipse((cx - sw(16), cy - sw(16), cx + sw(16), cy + sw(16)), fill=st["band"])
        d.ellipse((cx - sw(6), cy - sw(10), cx + sw(2), cy - sw(2)), fill=(255, 255, 255))
    elif kind == "dots":
        col = st["panel_line"]
        for r in range(5):
            for c in range(14):
                x, y = sw(30) + c * sw(64), sw(34) + r * sw(50)
                if x < W - sw(30):
                    d.ellipse((x, y, x + sw(4), y + sw(4)), fill=col)
    elif kind == "frame":
        d.rounded_rectangle(
            (sw(12), sw(12), W - sw(12), H - sw(12)),
            radius=sw(22),
            outline=st["panel_line"],
            width=2 * SCALE,
        )


def _wave_footer(d: ImageDraw.ImageDraw, st: dict, W: int, H: int, foot: str, f_foot, sw):
    """底部波浪 + 页脚文字。"""
    wh = sw(30)
    pts = [(0, H)]
    for i in range(61):
        x = W * i / 60
        pts.append((x, H - wh - math.sin(i / 60 * math.pi * 2) * sw(9)))
    pts.append((W, H))
    d.polygon(pts, fill=st["wave"])
    d.text((sw(40), H - sw(34)), foot, font=f_foot, fill=st["foot_fg"])


# ==================== 每日卡片样式（10 套，参照海报版式） ====================

STYLES = [
    # 1 喜庆红（参考图1：粉白底、红笔刷标题带、白面板、红/橙/绿油号块、黄横幅+红条、深蓝倒计时）
    dict(
        name="喜庆红",
        bg_top=(255, 238, 232),
        bg_bottom=(255, 214, 200),
        decor="sparkle",
        scene=True,
        band=(226, 32, 38),
        title_fg=(255, 255, 255),
        title_hi=(255, 224, 96),
        title_stroke=(190, 22, 28),
        panel=(255, 252, 250),
        panel_line=(255, 214, 205),
        header_fg=(70, 40, 38),
        fuel_pills=[
            ((226, 32, 38), (255, 255, 255)),
            ((255, 122, 24), (255, 255, 255)),
            ((28, 140, 86), (255, 255, 255)),
        ],
        gas_fg=(90, 60, 58),
        price=(226, 32, 38),
        unit=(170, 110, 100),
        chg_bg=(226, 32, 38),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(24, 128, 84),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(238, 226, 222),
        flat_fg=(150, 120, 112),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(60, 36, 16),
        bn_hi=(210, 34, 40),
        band2_bg=(220, 40, 40),
        band2_fg=(255, 246, 230),
        band2_hi=(255, 224, 96),
        count_bg=(26, 42, 86),
        count_fg=(240, 244, 255),
        count_hi=(255, 214, 64),
        count_line=(26, 42, 86),
        wave=(232, 44, 44),
        foot_fg=(255, 255, 255),
    ),
    # 2 深蓝霓虹（参考图2：深蓝底、红标题带、深蓝面板）
    dict(
        name="深蓝霓虹",
        bg_top=(9, 15, 40),
        bg_bottom=(14, 30, 72),
        decor="frame",
        scene=False,
        band=(190, 28, 42),
        title_fg=(255, 255, 255),
        title_hi=(255, 214, 64),
        title_stroke=(110, 10, 20),
        panel=(16, 36, 86),
        panel_line=(60, 110, 210),
        header_fg=(232, 240, 255),
        fuel_pills=[
            ((200, 34, 50), (255, 255, 255)),
            ((28, 90, 200), (255, 255, 255)),
            ((190, 140, 30), (45, 32, 8)),
        ],
        gas_fg=(150, 185, 240),
        price=(255, 255, 255),
        unit=(130, 170, 235),
        chg_bg=(210, 40, 56),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(16, 120, 105),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(24, 48, 100),
        flat_fg=(130, 170, 235),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(50, 34, 10),
        bn_hi=(190, 26, 36),
        band2_bg=(170, 28, 40),
        band2_fg=(255, 238, 238),
        band2_hi=(255, 214, 110),
        count_bg=(12, 30, 76),
        count_fg=(170, 205, 255),
        count_hi=(255, 214, 64),
        count_line=(60, 110, 210),
        wave=(20, 44, 100),
        foot_fg=(150, 180, 235),
    ),
    # 3 牛皮纸（参考图3：纸色底、图钉、红笔刷、米黄面板）
    dict(
        name="牛皮纸",
        bg_top=(224, 198, 158),
        bg_bottom=(206, 176, 130),
        decor="pin",
        scene=False,
        band=(196, 44, 30),
        title_fg=(255, 246, 222),
        title_hi=(255, 214, 96),
        title_stroke=(150, 32, 20),
        panel=(250, 238, 212),
        panel_line=(216, 186, 142),
        header_fg=(84, 56, 30),
        fuel_pills=[
            ((196, 44, 30), (255, 246, 222)),
            ((214, 120, 40), (255, 250, 235)),
            ((94, 132, 72), (255, 255, 255)),
        ],
        gas_fg=(120, 86, 50),
        price=(196, 44, 30),
        unit=(150, 116, 80),
        chg_bg=(196, 44, 30),
        chg_fg=(255, 246, 222),
        chg_dn_bg=(94, 132, 72),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(232, 214, 180),
        flat_fg=(140, 108, 72),
        bn_y_bg=(255, 220, 130),
        bn_y_fg=(90, 54, 20),
        bn_hi=(196, 44, 30),
        band2_bg=(214, 66, 40),
        band2_fg=(255, 246, 224),
        band2_hi=(255, 226, 140),
        count_bg=(255, 246, 220),
        count_fg=(110, 76, 44),
        count_hi=(196, 44, 30),
        count_line=(216, 186, 142),
        wave=(214, 66, 40),
        foot_fg=(255, 248, 232),
    ),
    # 4 天空蓝（参考图4/大图：蓝天白云、红标题带、白面板、深蓝倒计时条）
    dict(
        name="天空蓝",
        bg_top=(150, 212, 255),
        bg_bottom=(218, 242, 255),
        decor="clouds",
        scene=True,
        band=(226, 44, 40),
        title_fg=(255, 255, 255),
        title_hi=(255, 224, 96),
        title_stroke=(180, 26, 24),
        panel=(255, 255, 255),
        panel_line=(198, 224, 248),
        header_fg=(44, 64, 92),
        fuel_pills=[
            ((226, 32, 38), (255, 255, 255)),
            ((30, 100, 200), (255, 255, 255)),
            ((24, 150, 90), (255, 255, 255)),
        ],
        gas_fg=(80, 104, 140),
        price=(226, 32, 38),
        unit=(120, 150, 190),
        chg_bg=(226, 32, 38),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(24, 140, 90),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(226, 238, 248),
        flat_fg=(110, 140, 180),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(70, 44, 14),
        bn_hi=(210, 30, 36),
        band2_bg=(220, 44, 40),
        band2_fg=(255, 250, 240),
        band2_hi=(255, 224, 96),
        count_bg=(28, 52, 110),
        count_fg=(230, 240, 255),
        count_hi=(255, 214, 64),
        count_line=(28, 52, 110),
        wave=(30, 96, 190),
        foot_fg=(255, 255, 255),
    ),
    # 5 曜石黑（参考图5：黑底、白描边标题、黑面板、黄横幅）
    dict(
        name="曜石黑",
        bg_top=(15, 15, 17),
        bg_bottom=(32, 32, 36),
        decor="frame",
        scene=False,
        band=(34, 34, 40),
        title_fg=(250, 250, 250),
        title_hi=(255, 214, 64),
        title_stroke=(90, 90, 100),
        panel=(24, 24, 28),
        panel_line=(90, 90, 100),
        header_fg=(240, 240, 242),
        fuel_pills=[
            ((220, 50, 56), (255, 255, 255)),
            ((60, 120, 210), (255, 255, 255)),
            ((30, 150, 90), (255, 255, 255)),
        ],
        gas_fg=(160, 160, 170),
        price=(255, 214, 64),
        unit=(150, 150, 160),
        chg_bg=(220, 50, 56),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(30, 150, 90),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(44, 44, 52),
        flat_fg=(150, 150, 160),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(40, 32, 16),
        bn_hi=(200, 34, 40),
        band2_bg=(44, 44, 52),
        band2_fg=(255, 224, 96),
        band2_hi=(255, 120, 110),
        count_bg=(28, 28, 34),
        count_fg=(220, 220, 228),
        count_hi=(255, 214, 64),
        count_line=(90, 90, 100),
        wave=(40, 40, 48),
        foot_fg=(150, 150, 160),
    ),
    # 6 红白简报（参考图6：白底、红标题带、白面板、彩油号块、红波浪）
    dict(
        name="红白简报",
        bg_top=(255, 255, 255),
        bg_bottom=(255, 238, 234),
        decor="dots",
        scene=False,
        band=(226, 32, 38),
        title_fg=(255, 255, 255),
        title_hi=(255, 224, 96),
        title_stroke=(190, 22, 28),
        panel=(255, 255, 255),
        panel_line=(242, 216, 212),
        header_fg=(60, 48, 46),
        fuel_pills=[
            ((226, 32, 38), (255, 255, 255)),
            ((30, 100, 200), (255, 255, 255)),
            ((120, 50, 200), (255, 255, 255)),
        ],
        gas_fg=(110, 90, 86),
        price=(226, 32, 38),
        unit=(150, 130, 126),
        chg_bg=(255, 92, 64),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(24, 140, 90),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(240, 230, 226),
        flat_fg=(150, 130, 126),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(60, 36, 16),
        bn_hi=(210, 34, 40),
        band2_bg=(255, 92, 64),
        band2_fg=(255, 255, 255),
        band2_hi=(255, 238, 180),
        count_bg=(226, 32, 38),
        count_fg=(255, 246, 240),
        count_hi=(255, 224, 96),
        count_line=(226, 32, 38),
        wave=(226, 32, 38),
        foot_fg=(255, 255, 255),
    ),
    # 7 蓝天宣传（参考图7：蓝天、红带黄字、彩油号块、黄横幅）
    dict(
        name="蓝天宣传",
        bg_top=(168, 222, 255),
        bg_bottom=(224, 246, 255),
        decor="clouds",
        scene=True,
        band=(226, 32, 38),
        title_fg=(255, 255, 255),
        title_hi=(255, 224, 64),
        title_stroke=(180, 24, 28),
        panel=(255, 255, 255),
        panel_line=(196, 224, 246),
        header_fg=(46, 60, 84),
        fuel_pills=[
            ((24, 150, 90), (255, 255, 255)),
            ((30, 100, 200), (255, 255, 255)),
            ((120, 50, 200), (255, 255, 255)),
        ],
        gas_fg=(84, 104, 134),
        price=(226, 32, 38),
        unit=(120, 140, 170),
        chg_bg=(226, 32, 38),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(24, 140, 90),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(228, 238, 248),
        flat_fg=(120, 140, 170),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(80, 48, 14),
        bn_hi=(200, 30, 36),
        band2_bg=(220, 40, 40),
        band2_fg=(255, 248, 236),
        band2_hi=(255, 224, 96),
        count_bg=(24, 56, 130),
        count_fg=(225, 238, 255),
        count_hi=(255, 214, 64),
        count_line=(24, 56, 130),
        wave=(24, 90, 180),
        foot_fg=(255, 255, 255),
    ),
    # 8 深空蓝（参考图8：近黑深蓝、荧光描边、发光元素）
    dict(
        name="深空蓝",
        bg_top=(4, 9, 26),
        bg_bottom=(8, 20, 52),
        decor="frame",
        scene=False,
        band=(12, 34, 88),
        title_fg=(240, 248, 255),
        title_hi=(0, 220, 255),
        title_stroke=(0, 150, 230),
        panel=(10, 26, 62),
        panel_line=(0, 150, 230),
        header_fg=(226, 240, 255),
        fuel_pills=[
            ((210, 40, 60), (255, 255, 255)),
            ((0, 130, 200), (255, 255, 255)),
            ((0, 170, 150), (255, 255, 255)),
        ],
        gas_fg=(120, 180, 240),
        price=(255, 255, 255),
        unit=(110, 170, 240),
        chg_bg=(210, 40, 60),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(0, 150, 140),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(16, 40, 92),
        flat_fg=(110, 170, 240),
        bn_y_bg=(255, 214, 64),
        bn_y_fg=(46, 32, 8),
        bn_hi=(190, 26, 36),
        band2_bg=(150, 26, 40),
        band2_fg=(255, 235, 235),
        band2_hi=(255, 214, 110),
        count_bg=(8, 26, 66),
        count_fg=(140, 205, 255),
        count_hi=(0, 229, 255),
        count_line=(0, 150, 230),
        wave=(10, 24, 58),
        foot_fg=(110, 160, 220),
    ),
    # 9 奶油手账（参考图9：奶油底、图钉、黄气泡横幅）
    dict(
        name="奶油手账",
        bg_top=(250, 240, 216),
        bg_bottom=(243, 224, 186),
        decor="pin",
        scene=False,
        band=(214, 52, 44),
        title_fg=(255, 252, 240),
        title_hi=(255, 214, 96),
        title_stroke=(160, 36, 30),
        panel=(252, 246, 228),
        panel_line=(230, 206, 160),
        header_fg=(92, 62, 38),
        fuel_pills=[
            ((214, 52, 44), (255, 255, 255)),
            ((230, 150, 40), (255, 250, 235)),
            ((80, 140, 80), (255, 255, 255)),
        ],
        gas_fg=(128, 94, 60),
        price=(214, 52, 44),
        unit=(160, 124, 88),
        chg_bg=(214, 52, 44),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(80, 140, 80),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(236, 220, 188),
        flat_fg=(160, 124, 88),
        bn_y_bg=(255, 222, 128),
        bn_y_fg=(96, 58, 20),
        bn_hi=(200, 44, 38),
        band2_bg=(214, 52, 44),
        band2_fg=(255, 248, 234),
        band2_hi=(255, 222, 128),
        count_bg=(110, 76, 44),
        count_fg=(255, 248, 230),
        count_hi=(255, 222, 128),
        count_line=(110, 76, 44),
        wave=(214, 52, 44),
        foot_fg=(255, 248, 234),
    ),
    # 10 黑金（参考图10：黑底金字、金描边）
    dict(
        name="黑金",
        bg_top=(12, 10, 10),
        bg_bottom=(38, 30, 22),
        decor="frame",
        scene=False,
        band=(52, 40, 26),
        title_fg=(255, 214, 96),
        title_hi=(255, 120, 80),
        title_stroke=(90, 66, 30),
        panel=(26, 21, 16),
        panel_line=(140, 104, 52),
        header_fg=(250, 238, 218),
        fuel_pills=[
            ((220, 60, 44), (255, 255, 255)),
            ((190, 140, 40), (40, 28, 12)),
            ((60, 150, 90), (255, 255, 255)),
        ],
        gas_fg=(180, 150, 110),
        price=(255, 214, 96),
        unit=(170, 140, 100),
        chg_bg=(220, 60, 44),
        chg_fg=(255, 255, 255),
        chg_dn_bg=(60, 150, 90),
        chg_dn_fg=(255, 255, 255),
        flat_bg=(46, 38, 28),
        flat_fg=(170, 140, 100),
        bn_y_bg=(196, 30, 34),
        bn_y_fg=(255, 238, 214),
        bn_hi=(255, 214, 96),
        band2_bg=(52, 40, 26),
        band2_fg=(255, 224, 140),
        band2_hi=(255, 120, 80),
        count_bg=(30, 24, 18),
        count_fg=(230, 210, 170),
        count_hi=(255, 214, 96),
        count_line=(140, 104, 52),
        wave=(44, 34, 24),
        foot_fg=(190, 160, 120),
    ),
]


def _style_index(now: datetime = None) -> int:
    """按周轮换：ISO 周 ×7 + 星期，一周 7 天互不重复，跨周错开，10 套均会轮到。"""
    d = now or datetime.now()
    iso = d.isocalendar()
    return (iso[1] * 7 + iso[2]) % len(STYLES)


def _style(idx: int = None) -> dict:
    return STYLES[_style_index() if idx is None else idx % len(STYLES)]


def _fuel_pill(st: dict, i: int) -> tuple:
    return st["fuel_pills"][i % len(st["fuel_pills"])]


# ==================== 每日卡片（海报版式） ====================


def _siren(d: ImageDraw.ImageDraw, cx: int, cy: int, s: int):
    """警灯图标：红圆顶 + 深色底座 + 放射线。"""
    d.pieslice(
        (cx - s, cy - int(s * 0.95), cx + s, cy + int(s * 0.55)),
        180,
        360,
        fill=(236, 50, 44),
    )
    d.ellipse(
        (cx - int(s * 0.42), cy - int(s * 0.6), cx - int(s * 0.05), cy - int(s * 0.25)),
        fill=(255, 190, 170),
    )
    d.rounded_rectangle(
        (cx - int(s * 0.78), cy + int(s * 0.35), cx + int(s * 0.78), cy + int(s * 0.8)),
        radius=int(s * 0.12),
        fill=(70, 22, 20),
    )
    for ang in (-150, -120, -90, -60, -30):
        a = math.radians(ang)
        d.line(
            (
                cx + math.cos(a) * s * 0.8,
                cy + math.sin(a) * s * 0.8,
                cx + math.cos(a) * s * 1.18,
                cy + math.sin(a) * s * 1.18,
            ),
            fill=(255, 214, 64),
            width=max(2, int(s * 0.1)),
        )


def _megaphone(d: ImageDraw.ImageDraw, cx: int, cy: int, s: int):
    """喇叭小图标（朝右）。"""
    d.polygon(
        [
            (cx - s, cy - s * 0.25),
            (cx, cy - s * 0.62),
            (cx, cy + s * 0.38),
            (cx - s, cy + s * 0.05),
        ],
        fill=(252, 250, 246),
    )
    d.ellipse(
        (cx - s * 1.08, cy - s * 0.38, cx - s * 0.72, cy + s * 0.18), fill=(236, 60, 50)
    )
    d.ellipse((cx, cy - s * 0.62, cx + s * 0.16, cy + s * 0.38), fill=(236, 60, 50))
    d.rounded_rectangle(
        (cx - s * 0.95, cy + s * 0.02, cx - s * 0.68, cy + s * 0.5),
        radius=int(s * 0.06),
        fill=(236, 60, 50),
    )
    for dy in (-0.42, -0.02, 0.38):
        d.arc(
            (cx + s * 0.24, cy + s * (dy - 0.2), cx + s * 0.8, cy + s * (dy + 0.2)),
            -60,
            60,
            fill=(252, 250, 246),
            width=max(2, int(s * 0.08)),
        )


def _pump(d: ImageDraw.ImageDraw, x: int, y: int, s: int, body: tuple):
    """扁平加油机小插画（参考图右下角装饰）。"""
    d.rounded_rectangle(
        (x - int(s * 0.95), y, x + int(s * 0.95), y + int(s * 0.16)),
        radius=int(s * 0.05),
        fill=(250, 248, 242),
    )
    d.rectangle(
        (x - int(s * 0.6), y + int(s * 0.16), x + int(s * 0.6), y + int(s * 0.24)),
        fill=body,
    )
    d.rounded_rectangle(
        (x - int(s * 0.45), y + int(s * 0.24), x + int(s * 0.45), y + s),
        radius=int(s * 0.08),
        fill=body,
    )
    d.rounded_rectangle(
        (x - int(s * 0.26), y + int(s * 0.34), x + int(s * 0.26), y + int(s * 0.58)),
        radius=int(s * 0.04),
        fill=(255, 255, 255),
    )
    d.rectangle(
        (x - int(s * 0.26), y + int(s * 0.64), x + int(s * 0.26), y + int(s * 0.7)),
        fill=(35, 32, 40),
    )
    d.arc(
        (x + int(s * 0.3), y + int(s * 0.45), x + int(s * 0.95), y + int(s * 0.92)),
        270,
        90,
        fill=(45, 42, 48),
        width=max(2, int(s * 0.07)),
    )


def _two_tone(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    segs: list,
    font,
    stroke: int = 0,
    stroke_fill=None,
    sh_dy: int = 0,
    sh_fill=(35, 38, 52),
) -> int:
    """按段绘制双色文本（segs: [(text, fill), ...]），返回总宽。sh_dy>0 时带投影。"""
    total = sum(d.textlength(t, font=font) for t, _ in segs)
    if sh_dy:
        cx = x
        for t, _ in segs:
            d.text(
                (cx, y + sh_dy),
                t,
                font=font,
                fill=sh_fill,
                stroke_width=stroke,
                stroke_fill=sh_fill,
            )
            cx += d.textlength(t, font=font)
    cx = x
    for t, fill in segs:
        d.text(
            (cx, y),
            t,
            font=font,
            fill=fill,
            stroke_width=stroke,
            stroke_fill=stroke_fill or fill,
        )
        cx += d.textlength(t, font=font)
    return total


def render_report_card(
    prov: str,
    data: dict,
    prev_data,
    today: str,
    tmp_dir: str,
    fuels: list = None,
    style_idx: int = None,
    bg_path: str = None,
) -> str:
    """单省海报卡（参照参考图版式）：

    通栏笔刷标题带（警灯图标 + 双色描边大字）→ 白色内容面板（省份头 + 油号色块行 +
    大字价格 + 涨跌胶囊）→ 黄色横幅（油种上调）→ 红色笔刷条（赶油提示）→
    深色倒计时条（日历图标 + 大数字）→ 底部波浪。
    样式按周轮换；style_idx 可强制指定；bg_path 提供 AI 插画背景时叠加其上。
    """
    st = STYLES[3] if bg_path else _style(style_idx)  # AI 背景固定配天空蓝版式
    all_fuels = data.get("fuels") or {}
    keys = [k for k in (fuels or FUEL_ORDER) if (all_fuels.get(k) or {}).get("price")]

    rise_names = [
        FUEL_NAMES.get(k, k + "号")
        for k in keys
        if ((all_fuels.get(k) or {}).get("change") or 0) > 0
    ]
    days, d_str = _days_until(data.get("next_change_time") or "")
    hint = rise_hint_text(data.get("next_change_time") or "") if rise_names else ""

    # ---- 布局测量（逻辑坐标，绘制时统一乘 SCALE） ----
    W, ML = 900, 36
    pad, band_h, gap = 26, 150, 18
    header_h, row_h, row_div = 88, 104, 14
    bn1_h = 92 if rise_names else 0
    bn2_line_h, bn2_pad, bn2_gap = 44, 20, 12
    count_h = 96
    wave_h, foot_h = 52, 40

    meas = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    f_title = _font(58 * SCALE)
    f_header, f_label, f_gas = _font(31 * SCALE), _font(29 * SCALE), _font(26 * SCALE)
    f_price, f_unit, f_chg = _font(50 * SCALE), _font(22 * SCALE), _font(25 * SCALE)
    f_bn, f_b2 = _font(30 * SCALE), _font(26 * SCALE)
    f_count, f_num = _font(25 * SCALE), _font(54 * SCALE)
    f_foot = _font(17 * SCALE)

    bn2_lines = _wrap(meas, hint, f_b2, (W - 2 * ML - 150) * SCALE) if hint else []
    bn2_h = (2 * bn2_pad + bn2_line_h * len(bn2_lines)) if bn2_lines else 0
    panel_h = header_h + row_h * len(keys) + row_div * max(0, len(keys) - 1) + 16

    # 纵向流：标题带 → 面板 → 黄横幅 → 红笔刷条 → 倒计时条 → 波浪页脚
    y_panel = pad + band_h + gap
    y_bn1 = y_panel + panel_h + gap
    y_bn2 = y_bn1 + bn1_h + (14 if bn1_h else 0)
    y_count = y_bn2 + bn2_h + (14 if bn2_lines else 0)
    y_end = y_count + (count_h if days is not None else 0)
    H = y_end + wave_h + foot_h

    if bg_path:
        bg = Image.open(bg_path).convert("RGB")
        img = bg.resize((W * SCALE, H * SCALE), Image.LANCZOS)
    else:
        img = _bg_gradient(W * SCALE, H * SCALE, st["bg_top"], st["bg_bottom"])
    d = ImageDraw.Draw(img)

    def sw(v):
        return int(v * SCALE)

    if not bg_path:
        _decor(d, st, W * SCALE, H * SCALE, sw)
        if st.get("scene"):
            _pump(d, sw(W - 96), sw(H - wave_h - 170), sw(58), st["band"])

    # ---- 丝带标题带（燕尾飘带 + 投影） ----
    rcy, rh = sw(pad + band_h // 2), sw(band_h - 34)
    _ribbon(
        img,
        d,
        sw(ML - 6),
        sw(W - ML + 6),
        rcy,
        rh,
        st["band"],
        _mix(st["band"], (0, 0, 0), 0.35),
    )
    ty = pad + (band_h - 76) // 2
    if rise_names:
        segs = [
            ("油价", st["title_fg"]),
            ("上涨", st["title_hi"]),
            ("提醒", st["title_fg"]),
        ]
    else:
        segs = [("今日油价", st["title_fg"])]
    total = _two_tone(d, 0, -999, segs, f_title)  # 仅测量
    tx = (W * SCALE - total) / 2
    _two_tone(
        d,
        tx,
        sw(ty),
        segs,
        f_title,
        stroke=4 * SCALE,
        stroke_fill=st["title_stroke"],
        sh_dy=6 * SCALE,
        sh_fill=_mix(st["title_stroke"], (0, 0, 0), 0.35),
    )
    if rise_names:
        _siren(d, int(tx) - sw(86), rcy, sw(32))
        _siren(d, int(tx + total) + sw(86), rcy, sw(32))
    y = pad + band_h + gap

    # ---- 白色内容面板（投影） ----
    pbox = (sw(ML), sw(y), sw(W - ML), sw(y + panel_h))
    _with_shadow(
        img,
        lambda m: m.rounded_rectangle(pbox, radius=sw(28), fill=255),
        blur=16,
        dy=12,
        alpha=70,
    )
    _panel(d, pbox, sw(28), st["panel"], st["panel_line"], SCALE)
    # ---- 面板头：加油机图标 + 省份油价（日期） ----
    icx, icy, s = sw(ML + 62), sw(y + header_h // 2), sw(24)
    d.rounded_rectangle(
        (icx - s // 2, icy - s + sw(6), icx + s // 2, icy + s - sw(6)),
        radius=sw(6),
        fill=st["band"],
    )
    d.rounded_rectangle(
        (icx - s // 3, icy - s // 2, icx + s // 3, icy),
        radius=sw(4),
        fill=(255, 255, 255),
    )
    d.rounded_rectangle(
        (icx + s // 2, icy - s // 3, icx + s // 2 + sw(6), icy + s // 3),
        radius=sw(3),
        fill=st["band"],
    )
    d.text(
        (sw(ML + 96), sw(y + 22)),
        f"{prov}油价（{today[5:]}）",
        font=f_header,
        fill=st["header_fg"],
    )
    d.line(
        (sw(ML + 30), sw(y + header_h - 8), sw(W - ML - 30), sw(y + header_h - 8)),
        fill=st["panel_line"],
        width=SCALE,
    )

    ry = y + header_h + 4
    for i, fuel in enumerate(keys):
        entry = all_fuels.get(fuel) or {}
        ch = entry.get("change")
        if ch is None and prev_data:
            prev_e = (prev_data.get("fuels") or {}).get(fuel)
            if prev_e and prev_e.get("price"):
                ch = round(entry["price"] - float(prev_e["price"]), 2)
        cy = sw(ry + row_h // 2)
        # 油号色块 + “汽油”
        pb, pf = _fuel_pill(st, i)
        lab = fuel + "号"
        lw = int(d.textlength(lab, font=f_label)) + 46 * SCALE
        d.rounded_rectangle(
            (sw(ML + 34), cy - sw(28), sw(ML + 34) + lw, cy + sw(28)),
            radius=sw(28),
            fill=pb,
        )
        d.text(
            (sw(ML + 34) + 23 * SCALE, cy - f_label.size // 2 - 2),
            lab,
            font=f_label,
            fill=pf,
        )
        d.text(
            (sw(ML + 34) + lw + 18 * SCALE, cy - f_gas.size // 2 - 2),
            "汽油",
            font=f_gas,
            fill=st["gas_fg"],
        )
        # 涨跌胶囊（最右）
        up, down = (ch or 0) > 0, (ch or 0) < 0
        if ch is not None:
            t = f"↑涨 {ch:.2f}" if up else f"↓降 {abs(ch):.2f}" if down else "持平"
            cbg = st["chg_bg"] if up else st["chg_dn_bg"] if down else st["flat_bg"]
            cfg = st["chg_fg"] if (up or down) else st["flat_fg"]
        else:
            t, cbg, cfg = "—", st["flat_bg"], st["flat_fg"]
        cw = int(d.textlength(t, font=f_chg)) + 44 * SCALE
        d.rounded_rectangle(
            (sw(W - ML - 34) - cw, cy - sw(25), sw(W - ML - 34), cy + sw(25)),
            radius=sw(25),
            fill=cbg,
        )
        d.text(
            (sw(W - ML - 34) - cw + 22 * SCALE, cy - f_chg.size // 2 - 2),
            t,
            font=f_chg,
            fill=cfg,
        )
        # 大字价格 + 单位（在胶囊左侧，立体描边）
        px_right = sw(W - ML - 34) - cw - 26 * SCALE
        uw = d.textlength("元/升", font=f_unit)
        d.text((px_right - uw, cy - sw(2)), "元/升", font=f_unit, fill=st["unit"])
        price = f"{entry['price']:g}"
        pw = d.textlength(price, font=f_price)
        _text_sh(
            d,
            (px_right - uw - sw(10) - pw, cy - sw(28)),
            price,
            f_price,
            st["price"],
            stroke=2 * SCALE,
            stroke_fill=st["panel"],
            sh_dy=3 * SCALE,
            sh_fill=st["panel_line"],
        )
        if i < len(keys) - 1:
            d.line(
                (
                    sw(ML + 34),
                    sw(ry + row_h + row_div // 2),
                    sw(W - ML - 34),
                    sw(ry + row_h + row_div // 2),
                ),
                fill=st["panel_line"],
                width=SCALE,
            )
        ry += row_h + row_div
    y += panel_h + gap

    # ---- 黄色横幅：油种上调（渐变块 + 投影） ----
    if rise_names:
        _banner_block(
            img,
            d,
            (sw(ML), sw(y), sw(W - ML), sw(y + bn1_h)),
            sw(20),
            _mix(st["bn_y_bg"], (255, 255, 255), 0.18),
            _mix(st["bn_y_bg"], (0, 0, 0), 0.14),
        )
        _lightning(d, sw(ML + 52), sw(y + bn1_h // 2), sw(34), st["bn_hi"])
        l1 = "、".join(rise_names) + " "
        by = y + (bn1_h - 36) // 2
        w1 = _two_tone(d, 0, -999, [(l1, st["bn_y_fg"])], f_bn)  # 测量（缩放像素）
        w2 = d.textlength("上调！", font=f_bn)
        avail = (W - 2 * ML - 150) * SCALE
        lx = sw(ML + 88) + (avail - w1 - w2) / 2
        _two_tone(
            d,
            lx,
            sw(by),
            [(l1, st["bn_y_fg"]), ("上调！", st["bn_hi"])],
            f_bn,
            sh_dy=3 * SCALE,
            sh_fill=_mix(st["bn_y_bg"], (0, 0, 0), 0.35),
        )
        y += bn1_h

    # ---- 红色横条：赶油提示（渐变块 + 投影） ----
    if bn2_lines:
        y2 = y + bn2_gap
        _banner_block(
            img,
            d,
            (sw(ML), sw(y2), sw(W - ML), sw(y2 + bn2_h)),
            sw(20),
            _mix(st["band2_bg"], (255, 255, 255), 0.14),
            _mix(st["band2_bg"], (0, 0, 0), 0.18),
        )
        ly = y2 + bn2_pad
        for line in bn2_lines:
            lw = d.textlength(line, font=f_b2)
            _text_sh(
                d,
                ((W * SCALE - lw) / 2, sw(ly)),
                line,
                f_b2,
                st["band2_fg"],
                sh_dy=3 * SCALE,
                sh_fill=_mix(st["band2_bg"], (0, 0, 0), 0.4),
            )
            ly += bn2_line_h
        y = y2 + bn2_h

    # ---- 倒计时条（渐变块 + 投影 + 大数字立体） ----
    if days is not None:
        _banner_block(
            img,
            d,
            (sw(ML), sw(y), sw(W - ML), sw(y + count_h)),
            sw(24),
            _mix(st["count_bg"], (255, 255, 255), 0.12),
            _mix(st["count_bg"], (0, 0, 0), 0.22),
        )
        _calendar_icon(d, sw(ML + 30), sw(y + 22), sw(40), st["count_fg"])
        cy = y + count_h // 2
        if days >= 2:
            seg_a = "距下次调价窗口"
            seg_b = f"（{d_str} 24时）"
            ax = sw(ML + 96)
            d.text(
                (ax, sw(cy - f_count.size // 2 - 4)),
                seg_a,
                font=f_count,
                fill=st["count_fg"],
            )
            ax += d.textlength(seg_a, font=f_count) + 6 * SCALE
            d.text(
                (ax, sw(cy - f_count.size // 2 - 4)),
                seg_b,
                font=f_count,
                fill=st["count_hi"],
            )
            nx = sw(W - ML - 40)
            w_tian = d.textlength("天", font=f_count)
            nx -= w_tian
            d.text(
                (nx, sw(cy - f_count.size // 2 - 4)),
                "天",
                font=f_count,
                fill=st["count_fg"],
            )
            nx -= d.textlength(str(days), font=f_num) + sw(6)
            _text_sh(
                d,
                (nx, sw(cy) - sw(32)),
                str(days),
                f_num,
                st["count_hi"],
                sh_dy=4 * SCALE,
                sh_fill=(20, 24, 44),
            )
            nx -= d.textlength("还有", font=f_count) + sw(10)
            d.text(
                (nx, sw(cy - f_count.size // 2 - 4)),
                "还有",
                font=f_count,
                fill=st["count_fg"],
            )
        else:
            line = (
                "明晚 24 时调价，今晚加油还按现价！"
                if days == 1
                else "就是今晚 24 时调价窗口！"
            )
            d.text(
                (sw(ML + 96), sw(cy - f_count.size // 2 - 4)),
                line,
                font=f_count,
                fill=st["count_hi"],
            )
        y += count_h + gap

    # ---- 底部：AI 背景自带地面场景，仅叠页脚；否则波浪 + 页脚 ----
    if bg_path:
        d.text(
            (sw(40), sw(H - 34)),
            f"猫饼 · 数据来源 ShowAPI · {today}",
            font=f_foot,
            fill=(80, 80, 84),
        )
    else:
        _wave_footer(
            d,
            st,
            W * SCALE,
            H * SCALE,
            f"猫饼 · 数据来源 ShowAPI · {today}",
            f_foot,
            sw,
        )

    out = img.resize((W, H), Image.LANCZOS)
    return _save(out, tmp_dir, f"report_{prov}_{today}.png")


def render_multi_card(
    rows: list, today: str, tmp_dir: str, fuels: list = None, style_idx: int = None
) -> str:
    """多省速览海报长图：通栏标题带 + 每省一个白面板（省份头 + 油种横排），样式按周轮换。"""
    st = _style(style_idx)
    seed = _style_index() if style_idx is None else style_idx
    W, ML = 900, 36
    pad, band_h, gap = 26, 150, 18
    header_h, cell_h = 74, 118
    count_h = 96
    wave_h, foot_h = 52, 40

    days, d_str = _days_until((rows[0][1] if rows else {}).get("next_change_time") or "")
    panels = [
        (prov, data, [k for k in (fuels or FUEL_ORDER) if k in _active_fuels(data)][:4])
        for prov, data in rows
    ]

    def panel_h(keys_):
        return header_h + (cell_h if keys_ else 48) + 12

    body_top = pad + band_h + gap
    y_rows = body_top + (count_h + gap if days is not None else 0)
    body_h = sum(panel_h(k) + gap for _, _, k in panels)
    H = y_rows + body_h + wave_h + foot_h

    img = _bg_gradient(W * SCALE, H * SCALE, st["bg_top"], st["bg_bottom"])
    d = ImageDraw.Draw(img)

    def sw(v):
        return int(v * SCALE)

    f_title = _font(58 * SCALE)
    f_header, f_fuel, f_price, f_diff = (
        _font(29 * SCALE),
        _font(22 * SCALE),
        _font(40 * SCALE),
        _font(20 * SCALE),
    )
    f_count, f_small, f_foot = _font(25 * SCALE), _font(20 * SCALE), _font(17 * SCALE)

    _decor(d, st, W * SCALE, H * SCALE, sw)

    # 通栏标题带
    _brush_rect(
        d,
        -sw(6),
        sw(pad),
        W * SCALE + sw(6),
        sw(pad + band_h),
        st["band"],
        seed=seed + 1,
        jitter=sw(5),
    )
    segs = [("今日油价", st["title_fg"]), ("速览", st["title_hi"])]
    total = _two_tone(d, 0, -999, segs, f_title)
    _two_tone(
        d,
        (W * SCALE - total) / 2,
        sw(pad + (band_h - 76) // 2),
        segs,
        f_title,
        stroke=3 * SCALE,
        stroke_fill=st["title_stroke"],
    )

    if days is not None:
        _panel(
            d,
            (sw(ML), sw(body_top), sw(W - ML), sw(body_top + count_h)),
            sw(24),
            st["count_bg"],
            st["count_line"],
            SCALE,
        )
        _calendar_icon(d, sw(ML + 30), sw(body_top + 22), sw(40), st["count_fg"])
        cy = body_top + count_h // 2
        if days >= 2:
            d.text(
                (sw(ML + 96), sw(cy - f_count.size // 2 - 4)),
                f"距下次调价窗口（{d_str} 24时）还有 {days} 天",
                font=f_count,
                fill=st["count_hi"],
            )
        else:
            line = (
                "明晚 24 时调价，今晚加油还按现价！"
                if days == 1
                else "就是今晚 24 时调价窗口！"
            )
            d.text(
                (sw(ML + 96), sw(cy - f_count.size // 2 - 4)),
                line,
                font=f_count,
                fill=st["count_hi"],
            )

    col_w = (W - 2 * ML - 60) // 4
    y = y_rows
    for pi, (prov, data, keys) in enumerate(panels):
        ph = panel_h(keys)
        _panel(
            d,
            (sw(ML), sw(y), sw(W - ML), sw(y + ph)),
            sw(26),
            st["panel"],
            st["panel_line"],
            SCALE,
        )
        pb, pf = _fuel_pill(st, pi)
        pw = int(d.textlength(prov, font=f_header)) + 46 * SCALE
        d.rounded_rectangle(
            (sw(ML + 30), sw(y + 14), sw(ML + 30) + pw, sw(y + 58)),
            radius=sw(22),
            fill=pb,
        )
        d.text((sw(ML + 30) + 23 * SCALE, sw(y + 22)), prov, font=f_header, fill=pf)
        d.line(
            (sw(ML + 30), sw(y + header_h - 6), sw(W - ML - 30), sw(y + header_h - 6)),
            fill=st["panel_line"],
            width=SCALE,
        )
        cy = y + header_h
        if keys:
            for i, fuel in enumerate(keys):
                entry = (data.get("fuels") or {}).get(fuel) or {}
                x = ML + 30 + i * col_w
                d.text(
                    (sw(x), sw(cy + 8)),
                    fuel + "#",
                    font=f_fuel,
                    fill=_fuel_pill(st, i)[0],
                )
                d.text(
                    (sw(x), sw(cy + 34)),
                    f"{entry['price']:g}",
                    font=f_price,
                    fill=st["price"],
                    stroke_width=SCALE,
                    stroke_fill=st["panel"],
                )
                ch = entry.get("change") or 0
                t, _ = _diff(entry.get("change"))
                fg = (
                    st["chg_bg"] if ch > 0 else st["chg_dn_bg"] if ch < 0 else st["flat_fg"]
                )
                d.text((sw(x), sw(cy + 88)), t, font=f_diff, fill=fg)
        else:
            d.text((sw(ML + 34), sw(cy + 14)), "暂无数据", font=f_small, fill=st["gas_fg"])
        y += ph + gap

    _wave_footer(
        d, st, W * SCALE, H * SCALE, f"猫饼 · 数据来源 ShowAPI · {today}", f_foot, sw
    )
    out = img.resize((W, H), Image.LANCZOS)
    return _save(out, tmp_dir, f"report_multi_{today}.png")


# ==================== 单油种卡 / 加油小票 ====================


def render_price_card(
    prov: str, fuel_name: str, price: float, change, pct, today: str, tmp_dir: str
) -> str:
    """单油种大字卡片。"""
    W, H = 760, 520
    img = Image.new("RGB", (W, H), C_BG)
    d = ImageDraw.Draw(img)
    f_sub, f_big, f_unit, f_mid = _font(26), _font(120), _font(28), _font(30)

    d.rounded_rectangle((30, 30, W - 30, H - 30), radius=24, fill=C_PANEL)
    title = f"{prov} · {fuel_name}"
    d.text(((W - d.textlength(title, font=f_mid)) / 2, 80), title, font=f_mid, fill=C_MAIN)
    pw = d.textlength(f"{price:g}", font=f_big)
    d.text(((W - pw) / 2, 160), f"{price:g}", font=f_big, fill=C_MAIN)
    d.text(
        ((W - d.textlength("元/升", font=f_unit)) / 2, 300),
        "元/升",
        font=f_unit,
        fill=C_SUB,
    )

    t, col = _diff(change)
    line = f"较上次调价 {t}" if change is not None else "较上次调价 —"
    if change is not None and pct is not None:
        line += f"（{pct:+.2f}%）"
    d.text(((W - d.textlength(line, font=f_mid)) / 2, 372), line, font=f_mid, fill=col)
    d.text(
        ((W - d.textlength(today, font=f_sub)) / 2, H - 96),
        today,
        font=f_sub,
        fill=C_SUB,
    )
    return _save(img, tmp_dir, f"price_{prov}_{today}.png")


def render_receipt(
    liters: float,
    unit_price: float,
    total: float,
    prov: str,
    fuel_name: str,
    change_total,
    today: str,
    tmp_dir: str,
) -> str:
    """加油电子小票。"""
    W, H = 820, 700
    img = Image.new("RGB", (W, H), (247, 247, 242))
    d = ImageDraw.Draw(img)
    dark = (40, 40, 40)
    f_title, f_mid, f_body, f_total = _font(40), _font(30), _font(28), _font(56)

    d.rounded_rectangle(
        (30, 30, W - 30, H - 30), radius=16, outline=(210, 210, 200), width=3
    )
    d.text(
        ((W - d.textlength("猫饼加油站 · 电子小票", font=f_title)) / 2, 60),
        "猫饼加油站 · 电子小票",
        font=f_title,
        fill=dark,
    )
    y = 160
    for label, val in (
        ("油品", fuel_name),
        ("省份", prov),
        ("单价", f"{unit_price} 元/升"),
        ("升数", f"{liters:g} 升"),
    ):
        d.text((80, y), label, font=f_body, fill=dark)
        d.text((W - 100 - d.textlength(val, font=f_body), y), val, font=f_body, fill=dark)
        y += 62
    for i in range(14):
        x0 = 80 + i * 48
        d.line((x0, y + 8, x0 + 24, y + 8), fill=(190, 190, 180), width=2)
    y += 40
    tw = d.textlength(f"¥ {total:.2f}", font=f_total)
    d.text(((W - tw) / 2, y), f"¥ {total:.2f}", font=f_total, fill=dark)
    y += 90
    if change_total is not None:
        tip = (
            f"较上次调价多花 {change_total:.2f} 元"
            if change_total > 0
            else f"较上次调价省下 {abs(change_total):.2f} 元"
        )
        d.text(
            ((W - d.textlength(tip, font=f_mid)) / 2, y),
            tip,
            font=f_mid,
            fill=C_RED if change_total > 0 else C_GREEN,
        )
    foot = "猫饼提醒：涨了要趁早加喵~"
    d.text(
        ((W - d.textlength(foot, font=f_body)) / 2, H - 110),
        foot,
        font=f_body,
        fill=(120, 120, 120),
    )
    d.text(
        ((W - d.textlength(today, font=f_body)) / 2, H - 70),
        today,
        font=f_body,
        fill=(150, 150, 150),
    )
    return _save(img, tmp_dir, f"receipt_{liters:g}_{today}.png")


# ==================== 折线图（统计） ====================


def line_chart_image(
    lines: list, title: str, subtitle: str = "", theme: str = "dark"
) -> Image.Image:
    """油价走势折线图。

    lines: [{"label": "江苏 92#", "points": [("2026-10-01", 8.09), ...]}, ...]
    点按日期升序；缺日不连线断点（按已有点直接相连）。
    theme: dark（默认，QQ 推送用）/ light（WebUI 统计页浅色模式）。
    """
    W, H = 1040, 620
    ML, MR, MT, MB = 100, 40, 150, 90
    legend_per_row = 3
    legend_rows = math.ceil(len(lines) / legend_per_row) if lines else 0
    MB += legend_rows * 48  # 底部预留：日期标签行 + 图例区（互不重叠）

    light = theme == "light"
    c_bg = C_L_BG if light else C_BG
    c_panel = C_L_PANEL if light else C_PANEL
    c_line = C_L_LINE if light else C_LINE
    c_main = C_L_MAIN if light else C_MAIN
    c_sub = C_L_SUB if light else C_SUB
    palette = PALETTE_LIGHT if light else PALETTE

    img = Image.new("RGB", (W * SCALE, H * SCALE), c_bg)
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_tick, f_legend = (
        _font(38 * SCALE),
        _font(22 * SCALE),
        _font(20 * SCALE),
        _font(22 * SCALE),
    )

    def sw(v):
        return int(v * SCALE)

    d.rounded_rectangle(
        (sw(30), sw(30), sw(W - 30), sw(118)),
        radius=sw(24),
        fill=c_panel,
        outline=c_line if light else None,
        width=SCALE,
    )
    d.text((sw(60), sw(42)), title, font=f_title, fill=c_main)
    if subtitle:
        d.text((sw(60), sw(88)), subtitle, font=f_sub, fill=c_sub)

    px0, py0 = sw(ML), sw(MT)
    px1, py1 = sw(W - MR), sw(H - MB)

    pts_all = [p for line in lines for _, p in line["points"]]
    if not pts_all:
        d.text(
            (
                sw(W / 2) - d.textlength("暂无历史数据", font=f_sub) / 2,
                (py0 + py1) // 2,
            ),
            "暂无历史数据",
            font=f_sub,
            fill=c_sub,
        )
        return img.resize((W, H), Image.LANCZOS)

    lo, hi = min(pts_all), max(pts_all)
    pad = (hi - lo) * 0.08 or 0.4
    lo, hi = lo - pad, hi + pad

    def y_of(v):
        return py1 - (v - lo) / (hi - lo) * (py1 - py0)

    # 横向网格与 Y 轴刻度
    for i in range(6):
        v = lo + (hi - lo) * i / 5
        yy = y_of(v)
        d.line((px0, yy, px1, yy), fill=c_line, width=SCALE)
        label = f"{v:.2f}"
        d.text(
            (px0 - sw(14) - d.textlength(label, font=f_tick), yy - sw(12)),
            label,
            font=f_tick,
            fill=c_sub,
        )

    # X 轴：全局日期轴（缺失日期不画点，按日期对齐）
    all_dates = sorted({dt for line in lines for dt, _ in line["points"]})
    date_idx = {dt: i for i, dt in enumerate(all_dates)}
    if all_dates:
        step = max(1, math.ceil(len(all_dates) / 6))
        for dt in all_dates[::step]:
            xx = px0 + date_idx[dt] / max(1, len(all_dates) - 1) * (px1 - px0)
            d.text(
                (xx - d.textlength(dt[5:], font=f_tick) / 2, py1 + sw(12)),
                dt[5:],
                font=f_tick,
                fill=c_sub,
            )
        d.line((px0, py1, px1, py1), fill=c_line, width=SCALE)

    # 折线
    n_dates = max(1, len(all_dates) - 1)

    def x_of(dt):
        return px0 + date_idx[dt] / n_dates * (px1 - px0)

    for li, line in enumerate(lines):
        color = palette[li % len(palette)]
        coords = [(x_of(dt), y_of(p)) for dt, p in line["points"]]
        if len(coords) >= 2:
            d.line(coords, fill=color, width=3 * SCALE, joint="curve")
        for xx, yy in coords:
            r = 3 * SCALE
            d.ellipse((xx - r, yy - r, xx + r, yy + r), fill=color)

    # 图例（在日期标签下方，独立区域）
    ly = py1 + sw(46)
    for li, line in enumerate(lines):
        color = palette[li % len(palette)]
        row, col_i = divmod(li, legend_per_row)
        lx = sw(ML) + col_i * sw((W - ML - MR) / legend_per_row)
        yy = ly + row * sw(48)
        d.rectangle((lx, yy, lx + sw(26), yy + sw(12)), fill=color)
        d.text((lx + sw(36), yy - sw(8)), line["label"], font=f_legend, fill=c_main)

    return img.resize((W, H), Image.LANCZOS)


def build_lines(
    store_history_getter, provinces: list, fuels: list, start: str, end: str
) -> list:
    """从历史存储组装折线数据。store_history_getter: () -> history dict"""
    history = store_history_getter()
    lines = []
    for prov in provinces:
        prov_h = history.get(prov, {})
        for fuel in fuels:
            points = []
            for dt in sorted(k for k in prov_h if start <= k <= end):
                e = (prov_h[dt].get("fuels") or {}).get(fuel)
                if e and e.get("price"):
                    points.append((dt, float(e["price"])))
            if len(points) >= 2:
                lines.append({"label": f"{prov} {fuel}#", "points": points})
    return lines


def summarize_lines(lines: list) -> list:
    """每条折线的区间统计：起止价、涨跌、最高/最低/均价。"""
    out = []
    for line in lines:
        prices = [p for _, p in line["points"]]
        if not prices:
            continue
        first, last = prices[0], prices[-1]
        out.append(
            {
                "label": line["label"],
                "first": first,
                "last": last,
                "diff": round(last - first, 2),
                "diff_pct": round((last - first) / first * 100, 2) if first else None,
                "min": min(prices),
                "max": max(prices),
                "avg": round(sum(prices) / len(prices), 2),
                "days": len(prices),
            }
        )
    return out
