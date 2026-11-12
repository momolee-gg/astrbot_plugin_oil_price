"""下次调价预测（oil_1008 预警）：按发改委「10 个工作日一调」机制测算。

数据源（东方财富公开 JSON 接口，无需 key）：
- NYMEX 原油日线收盘价（RPTA_WEB_JY_HQ，datacenter 接口；push2his kline
  接口对 urllib 握手不友好，弃用）；
- 国内汽柴油历次调价记录（RPTA_WEB_YJ_BD，DIM_DATE=生效日，
  与 ShowAPI next_change_time 口径一致：窗口时刻=生效日前一日 24 时）。

测算口径（38 个真实调价周期回测校准：方向 32/32 全对，幅度平均误差≈87
元/吨 ≈ 0.07 元/升；模型 = 变化率 × 上期均价 × 0.62，系数吸收官方挂靠
篮子（布伦特/迪拜/米纳斯）相对 NYMEX 的平均偏离）：
- 上期均价 = 截止上次生效日前最后一个交易日（=上期窗口日）的 10 日均价；
- 本期均价 = 上期窗口日之后至最新一根行情（数据滞后时为不完整周期）；
- 变化率 = 本期均价 / 上期均价 - 1；幅度（元/吨）= 变化率(%) × 0.62 × 上期均价；
- |幅度| < 50 元/吨按搁浅处理（发改委规则：不足 50 元/吨不作调整）；
- 每升折算（按官方口径反推）：92/89号 ×0.00079、95/98号 ×0.00084、
  0号柴油 ×0.00085（2026-11-12 轮 -645/-625 元/吨 ↔ 92号 -0.51、
  95号 -0.54、0号 -0.53，两轮全部吻合）。
残差来源：预警时点拿不到窗口日当天行情（滞后 1 根），且单周期篮子偏离
无法消除——预测仅供预警参考，文案须带「以发改委公告为准」。
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .constants import USER_AGENT

_TIMEOUT = 10
_NYMEX_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get?"
    + urllib.parse.urlencode(
        {
            "reportName": "RPTA_WEB_JY_HQ",
            "columns": "ALL",
            "sortColumns": "date",
            "sortTypes": "-1",  # 降序取最近 120 条，代码内反转回升序
            "token": "894050c76af8597a853f5b408b759f5d",  # datacenter 公开页面 token
            "pageNumber": "1",
            "pageSize": "120",
            "source": "WEB",
        }
    )
)
_ADJ_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(
    {
        "reportName": "RPTA_WEB_YJ_BD",
        "columns": "ALL",
        "sortColumns": "DIM_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": "5",
        "source": "WEB",
    }
)

MA_WINDOW = 10  # 发改委调价周期：10 个工作日的移动均价
# 每吨幅度 = 变化率% × 上期均价(USD/桶) × PCT_PER_BBL。0.62 由 38 个真实
# 调价周期回测中位数标定（7.33 桶/吨 × 汇率 × 篮子/WTI 平均偏离，一并吸收）
PCT_PER_BBL = 0.62
FLAT_THRESHOLD = 50.0  # 搁浅线：调价金额不足 50 元/吨不作调整
# 每升折算：元/吨 → 元/升，按官方口径分油种标定（两轮实际调价全部吻合）
L_COEF = {"92": 0.00079, "95": 0.00084, "98": 0.00084, "89": 0.00079, "0": 0.00085}
_MIN_GAP = 8  # 本期已积累的交易日数下限（再少则变化率噪声过大）
_MAX_GAP = 20  # 超过说明调价历史滞后错期；过小说明窗口决策已公布、无「下次」可预测

# 按天缓存：估算一天内不变（多省份集提醒/指令共用）；官方公告单独缓存——
# 公告一般在窗口日 ~16 时落地，缓存分开才能在下午探测到公告后立即切换口径
_CACHE = {"date": None, "value": None}
_OFFICIAL_CACHE = {"date": None, "value": None}


class ForecastError(RuntimeError):
    """预测数据获取失败（网络/接口结构变化）。"""


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise ForecastError(f"网络请求失败: {e}") from e


def fetch_wti_closes() -> list:
    """NYMEX 原油日线收盘序列，升序 [(日期, 收盘价), ...]；接口结构变化抛 ForecastError。"""
    root = _http_json(_NYMEX_URL)
    rows = ((root.get("result") or {}).get("data")) or []
    closes = []
    for row in rows:
        try:
            closes.append((str(row.get("DATE") or "")[:10], float(row.get("CLOSE"))))
        except (TypeError, ValueError):
            continue
    if not closes:
        raise ForecastError("NYMEX 日线数据为空（接口结构可能已变化）")
    closes.reverse()  # 接口降序返回，反转回升序供 estimate 使用
    return closes


def fetch_last_adjust() -> dict:
    """最近一次调价记录：{"eff": 生效日, "qy_fd"/"cy_fd": 汽/柴油每吨幅度}。

    无数据抛 ForecastError。东财在发改委公告后（窗口日 ~16 时，早于 24 时
    生效）即录入该行——生效日=明天即代表官方数字已出、今晚 24 时落地。
    """
    root = _http_json(_ADJ_URL)
    data = ((root.get("result") or {}).get("data")) or []
    if not data:
        raise ForecastError("调价历史为空（接口结构可能已变化）")
    row = data[0]
    eff = str(row.get("DIM_DATE") or "")[:10]

    def _num(key):
        try:
            v = row.get(key)
            return float(v) if v not in ("", None) else None
        except (TypeError, ValueError):
            return None

    return {"eff": eff, "qy_fd": _num("QY_FD"), "cy_fd": _num("CY_FD")}


def estimate(closes: list, last_eff: str) -> dict:
    """按两期均价变化率测算下次调价方向与幅度。

    closes: 升序 NYMEX 收盘序列；last_eff: 上次调价生效日（YYYY-MM-DD）。
    数据不足/日期错位返回 None（调用方降级为不带预测的提醒文案）。
    """
    if len(closes) < MA_WINDOW * 2 + 2:  # 至少 22 根：两期均价 + 错期余量
        return None
    dates = [d for d, _ in closes]
    px = [p for _, p in closes]
    # 上期基准：截止生效日前最后一个交易日（=上期窗口日）的 10 日均价
    idxs = [i for i, d in enumerate(dates) if d < last_eff]
    if not idxs:
        return None
    i = idxs[-1]
    if i - MA_WINDOW + 1 < 0:
        return None
    gap = (len(dates) - 1) - i  # 本期已积累的交易日数（含滞后缺失）
    if not (_MIN_GAP <= gap <= _MAX_GAP):
        # 过小说明最近窗口的调价已公布（无「下次」可预测）；过长说明调价
        # 历史滞后错期（跨了多个周期）
        return None
    ma_prev = sum(px[i - MA_WINDOW + 1 : i + 1]) / MA_WINDOW
    if ma_prev <= 0:
        return None
    # 本期均价：上期窗口日之后至最新一根——数据滞后时为不完整周期，
    # 但严格不混入上期数据（对齐官方「本次调价的前10个工作日」口径）
    cycle = px[i + 1 :][-MA_WINDOW:]
    ma_now = sum(cycle) / len(cycle)
    rate = (ma_now / ma_prev - 1) * 100.0
    per_ton = rate * PCT_PER_BBL * ma_prev
    if abs(per_ton) < FLAT_THRESHOLD:
        direction = "flat"
    else:
        direction = "rise" if per_ton > 0 else "drop"
    delta = {fuel: round(per_ton * coef, 2) for fuel, coef in L_COEF.items()}
    return {
        "dir": direction,
        "rate": round(rate, 2),
        "per_ton": round(per_ton),
        "delta": delta,
        "est_date": dates[-1],  # 测算基准日（最后一个交易日）
        "base_eff": last_eff,  # 上次调价生效日
    }


def _official_forecast(adj: dict) -> dict:
    """官方公告分支：发改委已公布（生效日=明天，今晚 24 时落地），直接用官方数。

    adj: fetch_last_adjust() 结构；柴油用其自带的 CY_FD（与汽油 QY_FD 幅度
    略有差异）。官方调价必 ≥ 50 元/吨（搁浅不发布），异常小时防御归 flat。
    """
    per_ton = adj["qy_fd"]
    if abs(per_ton) < FLAT_THRESHOLD:
        direction = "flat"
    else:
        direction = "rise" if per_ton > 0 else "drop"
    delta = {fuel: round(per_ton * coef, 2) for fuel, coef in L_COEF.items()}
    if adj.get("cy_fd") is not None:
        delta["0"] = round(adj["cy_fd"] * L_COEF["0"], 2)
    return {
        "dir": direction,
        "per_ton": round(per_ton),
        "delta": delta,
        "official": True,
        "est_date": "",  # 官方数无需测算基准
        "base_eff": adj["eff"],
    }


def build_forecast(today: str) -> dict:
    """拉取数据并测算，返回 estimate()/官方公告结构；数据不可用返回 None。

    官方公告已出（调价记录生效日 = 明天）时直接采用官方每吨幅度，
    不再估算；估算仅用于公告前的窗口预告（oil_1008）。
    """
    try:
        last = fetch_last_adjust()
        if last["qy_fd"] is not None and last["eff"] == _tomorrow(today):
            return _official_forecast(last)
        closes = fetch_wti_closes()
    except ForecastError:
        return None
    return estimate(closes, last["eff"])


def _tomorrow(today: str) -> str:
    """ISO 日期串 +1 天。"""
    d = datetime.strptime(today, "%Y-%m-%d").date() + timedelta(days=1)
    return d.isoformat()


def get_today_forecast(today: str) -> dict:
    """按天缓存的预测入口；失败返回 None（当天内不缓存失败，可重试）。

    每次调用先轻量现查调价历史单接口：公告一旦落地（生效日=明天）立即
    切换官方口径并单独缓存，不被公告前的估算缓存挡住（窗口日白天估算
    已缓存、下午公告落地，两者必须能换挡）。
    """
    if _OFFICIAL_CACHE["date"] == today and _OFFICIAL_CACHE["value"] is not None:
        return _OFFICIAL_CACHE["value"]
    try:
        last = fetch_last_adjust()
    except ForecastError:
        last = None
    if last and last["qy_fd"] is not None and last["eff"] == _tomorrow(today):
        value = _official_forecast(last)
        _OFFICIAL_CACHE["date"] = today
        _OFFICIAL_CACHE["value"] = value
        return value
    if _CACHE["date"] == today and _CACHE["value"] is not None:
        return _CACHE["value"]
    if last is None:
        return None
    try:
        closes = fetch_wti_closes()
    except ForecastError:
        return None
    value = estimate(closes, last["eff"])
    if value is not None:
        _CACHE["date"] = today
        _CACHE["value"] = value
    return value
