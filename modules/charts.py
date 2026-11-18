"""PIL 渲染：加油小票与折线图。仅依赖 constants 与标准库。

save_image/_save 返回已保存 PNG 路径；line_chart_image 返回 PIL Image，
由调用方决定存盘（推送/WebUI）还是转 bytes（WebUI 统计页）。
"""

import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

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
    PALETTE,
    PALETTE_LIGHT,
)

SCALE = 2  # 折线图超采样倍率（抗锯齿）


def _font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _save(img: Image.Image, tmp_dir: str, name: str) -> str:
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, name)
    img.save(path, "PNG")
    return path


def save_image(img: Image.Image, tmp_dir: str, name: str) -> str:
    """公开接口：将任意渲染结果（如折线图）保存为 PNG。"""
    return _save(img, tmp_dir, name)


def png_bytes(img: Image.Image) -> bytes:
    """公开接口：将渲染结果转 PNG bytes（WebUI 统计页直出）。"""
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ==================== 加油小票 ====================


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


# WebUI 页面模式（ui=True）的刻度/图例文字色：对齐页面正文灰，替代默认更浅的 c_sub
_UI_TICK_LIGHT = (96, 98, 105)  # ≈ 页面正文 #606266
_UI_TICK_DARK = (216, 220, 228)


def line_chart_image(
    lines: list, title: str, subtitle: str = "", theme: str = "dark", ui: bool = False
) -> Image.Image:
    """油价走势折线图。

    lines: [{"label": "江苏 92#", "points": [("2026-10-01", 8.09), ...]}, ...]
    点按日期升序；缺日不连线断点（按已有点直接相连）。
    theme: dark（QQ 推送用）/ light（WebUI 统计页浅色模式）。
    ui: True=WebUI 页面模式（oil_1001 F10）——窄画布减小 CSS 缩放、更大字号、
    正文灰刻度 + 微加粗，使视觉字号与页面正文对齐；False（默认）保持 QQ 推送
    原版式（月度推送视觉零回归）。
    """
    if ui:
        W, H = 760, 600
        f_title, f_sub, f_tick, f_legend = (
            _font(30 * SCALE),
            _font(24 * SCALE),
            _font(24 * SCALE),
            _font(24 * SCALE),
        )
        c_tick = _UI_TICK_LIGHT if theme == "light" else _UI_TICK_DARK
        stroke = SCALE  # 同色描边模拟加粗，消除「又细又浅」的观感
    else:
        W, H = 1040, 620
        f_title, f_sub, f_tick, f_legend = (
            _font(38 * SCALE),
            _font(22 * SCALE),
            _font(20 * SCALE),
            _font(22 * SCALE),
        )
        c_tick = None
        stroke = 0
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
    if c_tick is None:
        c_tick = c_sub

    img = Image.new("RGB", (W * SCALE, H * SCALE), c_bg)
    d = ImageDraw.Draw(img)

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
            fill=c_tick,
            stroke_width=stroke,
            stroke_fill=c_tick,
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
                fill=c_tick,
                stroke_width=stroke,
                stroke_fill=c_tick,
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
        d.text(
            (lx + sw(36), yy - sw(8)),
            line["label"],
            font=f_legend,
            fill=c_main,
            stroke_width=stroke,
            stroke_fill=c_main,
        )

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
