"""文字版播报文案。仅依赖 constants。"""

import random
from datetime import datetime, timedelta

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
    """距下次调价窗口的天数与窗口日期串（如 (6, "10月29日")）；无/过期返回 (None, "")。

    ShowAPI 的日期是**新价生效日**（如 20261030 = 10-29 24 时调整、10-30 起
    按新价执行，与官方“9月24日24时起调整→生效日 9/25”口径一致），因此
    窗口时刻 = 生效日前一日 24 时——倒计时与文案日期都按窗口日计算。
    """
    if not next_time:
        return None, ""
    try:
        effective = datetime.strptime(str(next_time), "%Y%m%d").date()
    except ValueError:
        return None, ""
    window_day = effective - timedelta(days=1)
    days = (window_day - datetime.now().date()).days
    return days, (window_day.strftime("%m月%d日") if days >= 0 else "")


def days_until(next_time: str) -> tuple:
    """_days_until 的公开别名（调价提醒判断剩余天数用）。"""
    return _days_until(next_time)


def adjust_countdown_text(next_time: str, direction: str = None, official=False) -> str:
    """不含换行的调价倒计时短句，无窗口时返回空串。

    direction: 下次调价预测方向（"rise"/"drop"/"flat"，None=未知），
    仅 days≤1 的建议行分向：涨价提醒赶在涨价前加满，降价提醒等降价，
    未知/搁浅保持中性——不再默认按涨价场景建议（oil_1008）。
    official=True（官方公告已出）时「预计」改「已公告」。
    """
    days, d_str = _days_until(next_time)
    if days is None:
        return ""
    if days > 1:
        return f"距下次调价窗口（{d_str} 24时）还有 {days} 天"
    if days == 1:
        if direction == "rise":
            word = "已公告涨价" if official else "预计涨价"
            advice = "要加油的今晚抓紧"
            return f"明晚 24 时调价窗口开启！{word}，{advice}"
        if direction == "drop":
            word = "已公告降价" if official else "预计降价"
            return f"明晚 24 时调价窗口开启！{word}，等明晚降价后再加更划算"
        return "明晚 24 时调价窗口开启"
    if days == 0:
        if direction == "rise":
            word = "已公告涨价" if official else "预计涨价"
            advice = "要加油的今晚抓紧"
            return f"就是今晚 24 时调价窗口开启！{word}，{advice}"
        if direction == "drop":
            word = "已公告降价" if official else "预计降价"
            return f"就是今晚 24 时调价窗口开启！{word}，明天起加油更划算"
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


def _remind_fuel_line(
    fuel: str, entry: dict, delta: float = None, official: bool = False
) -> str:
    """提醒价格行：现价 + 下次调价标注（oil_1008）。

    delta 为下次调价每升调整额（带符号）。公告后每吨幅度已确认，
    但每升值仍是按油号系数折算，标「↓降约 0.51」；公告前标
    「预计 ↓降约 0.51」。无预测或搁浅（delta 为 None/0）只报现价
    ——提醒语境下绝不能再把上一次调价的涨跌额（数据源 change 字段）
    挂出来冒充下次调整。
    """
    name = FUEL_NAMES.get(fuel, fuel + "号")
    price = entry["price"]
    if not delta:
        return f"{name}：现价 {price:g} 元/升"
    arrow = "↑涨" if delta > 0 else "↓降"
    if official:
        return f"{name}：现价 {price:g} 元/升（{arrow}约 {abs(delta):.2f}）"
    return f"{name}：现价 {price:g} 元/升（预计 {arrow}约 {abs(delta):.2f}）"


def _forecast_notice(forecast: dict) -> str:
    """预测摘要行（进海报提示区）：只写元/吨口径，避免含「元/升」
    被 genimage 行分类误归入数据面板。"""
    direction = forecast.get("dir")
    if direction == "flat":
        if forecast.get("official"):
            return "⚡ 官方公告：本轮调价搁浅，价格不作调整"
        # 50 元/吨与 forecast.FLAT_THRESHOLD 同源：搁浅线
        return "⚡ 预计本轮调价幅度不足 50 元/吨，大概率搁浅维持现价（以发改委公告为准）"
    word = "上调" if direction == "rise" else "下调"
    if forecast.get("official"):
        qy = abs(int(forecast.get("per_ton") or 0))
        cy = forecast.get("per_ton_diesel")
        notice = f"⚡ 官方公告：汽油每吨{word} {qy} 元"
        if cy is not None:
            notice += f"、柴油每吨{word} {abs(int(cy))} 元"
        return notice + "，今晚 24 时起执行"
    return (
        f"⚡ 预计每吨{word}约 {abs(int(forecast.get('per_ton') or 0))} 元，以发改委公告为准"
    )


def build_remind_text(rows: list, fuels: list = None, forecast: dict = None) -> str:
    """调价提醒全文（oil_1003；oil_1008 改为下次调价预警）。

    行结构同时服务两个消费者：QQ 文字消息直接阅读；AI 海报提示词按行
    分类（含「元/升」进数据面板、含「调价窗口」进倒计时区）。多省时
    价格行带省份前缀，海报上不致混淆归属。
    rows: [(prov, data), ...]；forecast: forecast.estimate()/官方公告结构（可
    None，None 时只报现价、方向中性，不带任何涨跌标注）。
    无任何倒计时行时返回空串（调用方不发）。
    """
    show = [f for f in (fuels or FUEL_ORDER)]
    lines = ["🚨🚨 调价提醒 🚨🚨"]
    direction = (forecast or {}).get("dir")
    official = bool((forecast or {}).get("official"))
    # 搁浅/未知方向不给价格行加标注（delta 只在明确涨/跌时展示）
    delta = (forecast or {}).get("delta") or {}
    if direction not in ("rise", "drop"):
        delta = {}
    multi = len(rows) > 1
    for prov, data in rows:
        line = adjust_countdown_text(
            data.get("next_change_time") or "", direction, official
        )
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
            lines.append(
                f"{prefix}{_remind_fuel_line(fuel, entry, delta.get(fuel), official)}"
            )
    if forecast and len(lines) > 1:
        lines.append(_forecast_notice(forecast))
    return "\n".join(lines) if len(lines) > 1 else ""


def build_text_report(
    prov: str,
    data: dict,
    trend_series=None,
    trend_days: int = 14,
    fuels: list = None,
    alarm: bool = True,
) -> str:
    """单省完整文字报告。fuels: 只展示的油号列表，None 表示全部。

    alarm=True（推送路径）涨跌日套「油价上涨/下调提醒」警报标题与 ⚡ 提示；
    alarm=False（用户主动查询，oil_1007）中性播报——不加警报标题与提示，
    涨跌仍体现在每行 ↑涨/↓降 标注里。查询与推送文案不同，当日生图缓存
    也因此天然分开，不再返回与落地确认推送逐字节相同的海报。
    """
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
    if rise_names and alarm:
        hint = rise_hint_text(next_time)
        text = (
            "🚨🚨 油价上涨提醒 🚨🚨\n"
            + text
            + f"\n\n⚡ {'、'.join(rise_names)}上调！{hint}"
        )
    elif drop_names and alarm:
        hint = drop_hint_text(next_time)
        text = (
            "🎉🎉 油价下调提醒 🎉🎉\n"
            + text
            + f"\n\n⚡ {'、'.join(drop_names)}下调！{hint}"
        )
    elif not rise_names and not drop_names:
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
