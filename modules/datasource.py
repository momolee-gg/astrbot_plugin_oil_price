"""ShowAPI（万维）油价数据源客户端。仅依赖 constants。"""

import http.client
import ipaddress
import json
import socket
import urllib.parse

from .constants import API_TIMEOUT, FUEL_FIELDS, USER_AGENT

# SSRF 守卫：ShowAPI 是唯一允许的数据源主机。先解析并校验 IP 全部为公网，
# 再按校验后的 IP 直连（Host 头带原主机名），同时阻断 DNS rebinding。
_API_HOST = "route.showapi.com"


class ShowApiError(RuntimeError):
    """接口调用失败（网络/平台错误/业务错误）。"""


class _SNIHTTPSConnection(http.client.HTTPSConnection):
    """按校验后的 IP 拨号、按原主机名做 SNI 与证书校验（防 DNS rebinding）。

    裸 HTTPSConnection(IP) 会把 IP 当 SNI/证书主机名，与 CDN 证书不匹配；
    connect() 覆写后 TCP 连 IP、TLS 校验 route.showapi.com。
    """

    def __init__(self, host: str, sni: str, **kw):
        super().__init__(host, **kw)
        self._sni = sni

    def connect(self):
        self.sock = self._create_connection((self.host, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._sni)


def fetch_province(prov: str, app_key: str) -> dict:
    """查询单省行情，返回规整数据：

    {"fuels": {"92": {"price", "before", "change", "pct"}, ...},
     "before_change_time": str, "next_change_time": "YYYYMMDD"（新价生效日，
     窗口时刻为其前一日 24 时，如 20261030 → 10-29 24 时调整）
    }
    """
    if not app_key:
        raise ShowApiError("未配置 appKey")
    try:
        infos = socket.getaddrinfo(_API_HOST, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ShowApiError(f"数据源解析失败: {e}") from e
    dial_ip = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ShowApiError(f"数据源解析到非公网地址: {info[4][0]}")
        if dial_ip is None:
            dial_ip = info[4][0]
    if dial_ip is None:
        raise ShowApiError("数据源无可用的公网解析结果")
    qs = urllib.parse.urlencode({"appKey": app_key, "prov": prov})
    conn = _SNIHTTPSConnection(dial_ip, sni=_API_HOST, timeout=API_TIMEOUT)
    try:
        conn.request(
            "GET",
            f"/138-49?{qs}",
            headers={"Host": _API_HOST, "User-Agent": USER_AGENT},
        )
        resp = conn.getresponse()
        root = json.loads(resp.read().decode("utf-8"))
    except ShowApiError:
        raise
    except Exception as e:
        raise ShowApiError(f"网络请求失败: {e}") from e
    finally:
        conn.close()

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
