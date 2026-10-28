"""文字版播报文案。仅依赖 constants。"""

import random
from datetime import datetime

from .constants import FUEL_NAMES, FUEL_ORDER

# 油价持平日的随机一句话（每次播报随机挑一条）
FLAT_NOTICES = [
    "今日油价按兵不动，该加油随时加，不用赶时间喵～",
    "本轮油价保持平稳，和上期一模一样，正常加油即可！",
    "油价暂无涨跌，钱包无压力，按需加油就好～",
    "今日油价与上期持平，放心安排加油计划！",
    "油价继续躺平，一分没涨一分没降，随加随走～",
]


def _fmt_change(ch) -> str:
    if ch is None:
        return "—"
    if ch > 0:
        return f"↑{ch:.2f}"
    if ch < 0:
        return f"↓{abs(ch):.2f}"
    return "持平"


def fuel_line(fuel: str, entry: dict) -> str:
    name = FUEL_NAMES.get(fuel, fuel + "号")
    price = entry["price"]
    ch = entry.get("change")
    if ch is None:
        return f"{name}：{price:g} 元/升"
    if ch > 0:
        return f"{name}：{price:g} 元/升（↑涨 {ch:.2f}）"
    if ch < 0:
        return f"{name}：{price:g} 元/升（↓降 {abs(ch):.2f}）"
    return f"{name}：{price:g} 元/升（持平）"


def _days_until(next_time: str) -> tuple:
    """距下次调价窗口的天数与日期串（如 (6, "10月30日")）；无窗口返回 (None, "")。"""
    if not next_time:
        return None, ""
    try:
        target = datetime.strptime(str(next_time), "%Y%m%d")
    except ValueError:
        return None, ""
    return (target.date() - datetime.now().date()).days, target.strftime("%m月%d日")


def days_until(next_time: str) -> tuple:
    """_days_until 的公开别名（调价提醒判断剩余天数用）。"""
    return _days_until(next_time)


def adjust_countdown_text(next_time: str) -> str:
    """不含换行的调价倒计时短句，无窗口时返回空串。"""
    days, d_str = _days_until(next_time)
    if days is None:
        return ""
    if days > 1:
        return f"距下次调价窗口（{d_str} 24时）还有 {days} 天"
    if days == 1:
        return "明晚 24 时调价窗口开启！要加油的今晚抓紧"
    if days == 0:
        return "就是今晚 24 时！调价窗口开启"
    return ""


def rise_hint_text(next_time: str) -> str:
    """上涨提醒的赶油提示：截止时间按下次调价窗口动态计算，不写死“今晚”。"""
    days, d_str = _days_until(next_time)
    if days == 0:
        return "就是今晚 24 时调价！按现价加油最后一天，要加油的赶紧冲！"
    if days == 1:
        return "明晚 24 时调价，今晚加油还按现价，要加油的抓紧！"
    if days and days > 1:
        return f"本轮涨幅已生效，下次调价窗口（{d_str} 24时）前趁现价加满更划算！"
    return "本轮涨幅已生效，要加油的尽早加满喵！"


def drop_hint_text(next_time: str) -> str:
    """下调提醒：降幅已生效的省钱提示。"""
    days, d_str = _days_until(next_time)
    if days and days > 1:
        return (
            f"本轮降幅已生效，今天起加油更省钱！下次调价窗口（{d_str} 24时）前价格保持不变"
        )
    if days == 1:
        return "本轮降幅已生效，今天起加油更省钱！明晚 24 时迎来下一个调价窗口"
    if days == 0:
        return "本轮降幅已生效，今天起加油更省钱！今晚 24 时就是下一个调价窗口"
    return "本轮降幅已生效，今天起加油更省钱！"


def build_remind_text(rows: list, fuels: list = None) -> str:
    """调价提醒全文（oil_1003）：每省倒计时 + 当前油价明细。

    行结构同时服务两个消费者：QQ 文字消息直接阅读；AI 海报提示词按行
    分类（含「元/升」进数据面板、含「调价窗口」进倒计时区）。多省时
    价格行带省份前缀，海报上不致混淆归属。
    rows: [(prov, data), ...]；无任何倒计时行时返回空串（调用方不发）。
    """
    show = [f for f in (fuels or FUEL_ORDER)]
    lines = ["🚨🚨 调价提醒 🚨🚨"]
    multi = len(rows) > 1
    for prov, data in rows:
        line = adjust_countdown_text(data.get("next_change_time") or "")
        if not line:
            continue
        lines.append(f"⛽ {prov}：{line}")
        for fuel in FUEL_ORDER:
            if fuel not in show:
                continue
            entry = (data.get("fuels") or {}).get(fuel)
            if not entry:
                continue
            prefix = f"{prov} " if multi else ""
            lines.append(f"{prefix}{fuel_line(fuel, entry)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_text_report(
    prov: str, data: dict, trend_series=None, trend_days: int = 14, fuels: list = None
) -> str:
    """单省完整文字报告。fuels: 只展示的油号列表，None 表示全部。"""
    show = [f for f in (fuels or FUEL_ORDER)]
    today = datetime.now().strftime("%m-%d")
    lines = [f"⛽ {prov}油价（{today}）"]
    rise_names, drop_names = [], []
    for fuel in FUEL_ORDER:
        if fuel not in show:
            continue
        entry = (data.get("fuels") or {}).get(fuel)
        if entry:
            lines.append(fuel_line(fuel, entry))
            ch = entry.get("change") or 0
            if ch > 0:
                rise_names.append(FUEL_NAMES.get(fuel, fuel))
            elif ch < 0:
                drop_names.append(FUEL_NAMES.get(fuel, fuel))
    text = "\n".join(lines)

    if trend_series:
        series = trend_series(prov, trend_days)
        if len(series) >= 2:
            parts = []
            for fuel in show:
                vals = [
                    x[1]["fuels"][fuel]["price"]
                    for x in series
                    if fuel in (x[1].get("fuels") or {})
                ]
                if len(vals) >= 2:
                    parts.append(f"{fuel}号 {vals[0]:g}→{vals[-1]:g}")
            if parts:
                text += f"\n\n📈 近期走势（{len(series)} 日）：{'；'.join(parts)}"

    next_time = data.get("next_change_time") or ""
    if rise_names:
        hint = rise_hint_text(next_time)
        text = (
            "🚨🚨 油价上涨提醒 🚨🚨\n"
            + text
            + f"\n\n⚡ {'、'.join(rise_names)}上调！{hint}"
        )
    elif drop_names:
        hint = drop_hint_text(next_time)
        text = (
            "🎉🎉 油价下调提醒 🎉🎉\n"
            + text
            + f"\n\n⚡ {'、'.join(drop_names)}下调！{hint}"
        )
    else:
        # 持平提示语按 (日期, 省份) 确定选取（oil_1002）：当天内稳定可作缓存键，
        # 跨天/跨省仍保持随机感
        rng = random.Random(f"{today}|{prov}")
        text += f"\n\n💡 {rng.choice(FLAT_NOTICES)}"
    countdown = adjust_countdown_text(next_time)
    if countdown:
        text += f"\n\n📅 {countdown}"
    return text


def build_multi_text(rows: list, fuels: list = None) -> str:
    """多省紧凑文字报告。rows: [(prov, data), ...]"""
    show = [f for f in (fuels or FUEL_ORDER)]
    today = datetime.now().strftime("%m-%d")
    lines = [f"⛽ 今日油价速览（{today}）"]
    for prov, data in rows:
        parts = []
        for fuel in FUEL_ORDER:
            if fuel not in show:
                continue
            entry = (data.get("fuels") or {}).get(fuel)
            if entry:
                parts.append(
                    f"{fuel}#{entry['price']:g}({_fmt_change(entry.get('change'))})"
                )
        lines.append(f"【{prov}】{' '.join(parts)}")
    text = "\n".join(lines)
    countdown = adjust_countdown_text(
        (rows[0][1] if rows else {}).get("next_change_time") or ""
    )
    if countdown:
        text += f"\n\n📅 {countdown}"
    return text
