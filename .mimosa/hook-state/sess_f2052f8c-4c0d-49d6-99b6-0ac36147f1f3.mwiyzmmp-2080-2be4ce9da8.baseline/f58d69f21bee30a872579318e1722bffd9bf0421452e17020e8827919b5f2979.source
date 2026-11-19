"""共享出网守卫（v1.4 安全加固口径的通用化）。

所有插件主动外联必须经此模块：先解析主机并校验全部解析结果均为公网地址，
再按校验后的 IP 直连、按原主机名做 SNI 与证书校验——同时阻断 SSRF 内网探测
与 DNS rebinding。ShowAPI 数据源（datasource）与飞书推送（feishu）共用；
主机白名单由调用方限定（数据源/飞书各自只认官方域）。
"""

import http.client
import ipaddress
import socket


class NetGuardError(RuntimeError):
    """出网守卫拒绝（解析失败/解析到非公网地址/无可用公网结果）。"""


class SNISafeConnection(http.client.HTTPSConnection):
    """按校验后的 IP 拨号、按原主机名做 SNI 与证书校验（防 DNS rebinding）。

    裸 HTTPSConnection(IP) 会把 IP 当 SNI/证书主机名，与 CDN 证书不匹配；
    connect() 覆写后 TCP 连 IP、TLS 校验原主机名。
    """

    def __init__(self, host: str, sni: str, **kw):
        super().__init__(host, **kw)
        self._sni = sni

    def connect(self):
        self.sock = self._create_connection((self.host, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._sni)


def resolve_public_ip(host: str, port: int = 443) -> str:
    """解析主机并校验全部解析结果均为公网地址，返回首个公网 IP 用于直连。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise NetGuardError(f"{host} 解析失败: {e}") from e
    dial_ip = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise NetGuardError(f"{host} 解析到非公网地址: {info[4][0]}")
        if dial_ip is None:
            dial_ip = info[4][0]
    if dial_ip is None:
        raise NetGuardError(f"{host} 无可用的公网解析结果")
    return dial_ip


def guarded_request(
    host: str,
    method: str,
    path: str,
    *,
    port: int = 443,
    timeout: float,
    body: bytes = None,
    headers: dict = None,
) -> tuple:
    """经守卫发起一次 HTTPS 请求，返回 (HTTP 状态码, 响应体 bytes)。

    先解析校验（拒绝内网/非公网解析），再按校验后的 IP 直连（SNI/证书仍校验
    原主机名，阻断 DNS rebinding）；网络层异常原样抛出，由调用方包装。
    """
    dial_ip = resolve_public_ip(host, port)
    conn = SNISafeConnection(dial_ip, sni=host, port=port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()
