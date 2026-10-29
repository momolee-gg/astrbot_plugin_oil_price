"""ShowAPI（万维）油价数据源客户端。仅依赖 constants。"""

import json
import urllib.parse
import urllib.request

from .constants import API_BASE, API_TIMEOUT, FUEL_FIELDS, USER_AGENT


class ShowApiError(RuntimeError):
    """接口调用失败（网络/平台错误/业务错误）。"""


def fetch_province(prov: str, app_key: str) -> dict:
    """查询单省行情，返回规整数据：

    {"fuels": {"92": {"price", "before", "change", "pct"}, ...},
     "before_change_time": str, "next_change_time": "YYYYMMDD"（新价生效日，
     窗口时刻为其前一日 24 时，如 20261030 → 10-29 24 时调整）
    }
    """
    if not app_key:
        raise ShowApiError("未配置 appKey")
    qs = urllib.parse.urlencode({"appKey": app_key, "prov": prov})
    req = urllib.request.Request(
        f"{API_BASE}/138-49?{qs}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            root = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise ShowApiError(f"网络请求失败: {e}") from e

    if root.get("showapi_res_code") != 0:
        raise ShowApiError(f"ShowAPI 错误: {root.get('showapi_res_error')}")
    body = root.get("showapi_res_body") or {}
    if body.get("ret_code") not in (0, None):
        raise ShowApiError(f"接口业务错误: {body.get('ret_code')}")

    fuels = {}
    for fuel, field in FUEL_FIELDS.items():
        node = body.get(field) or {}
        price = node.get("price")
        if not price:
            continue
        entry = {"price": float(price)}
        for src, dst in (
            ("before_change_price", "before"),
            ("change", "change"),
            ("change_percent", "pct"),
        ):
            try:
                entry[dst] = (
                    float(node.get(src)) if node.get(src) not in ("", None) else None
                )
            except (TypeError, ValueError):
                entry[dst] = None
        fuels[fuel] = entry
    return {
        "fuels": fuels,
        "before_change_time": body.get("before_change_time") or "",
        "next_change_time": body.get("next_change_time") or "",
    }
