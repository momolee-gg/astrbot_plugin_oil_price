"""飞书群自定义机器人 webhook 客户端。

仅依赖 netguard/constants；外联经 netguard 出网守卫（官方域名白名单 +
公网解析校验 + IP 直连防 rebinding）。零第三方依赖（http.client 同步实现，
调用方用 asyncio.to_thread 包装）。

说明（v1.5.2）：飞书个人/群消息推送统一走 AstrBot 平台适配器
（context.send_message + UMO，图片由适配器用平台配置的凭证自行上传），
插件内不再实现自建应用开放接口、不再要求填写 app_id/app_secret。
本模块仅保留「群自定义机器人 webhook」这条与适配器无关的轻量通道。
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

from .constants import API_TIMEOUT, USER_AGENT
from .netguard import NetGuardError, guarded_request

# 官方机器人 webhook 仅有的两个域（国内版/国际版），其余一律拒绝
HOOK_HOSTS = ("open.feishu.cn", "open.larksuite.com")
_HOOK_PATH = "/open-apis/bot/v2/hook/"


class FeishuError(RuntimeError):
    """飞书推送失败（网络/平台错误/业务错误）。"""


# ---------- 目标解析 ----------


def parse_target(raw: str):
    """单条目标串 → (目标 dict 或 None, 错误说明)。仅识别 webhook。

    「地址##加签密钥」后缀写法；飞书个人/群消息请使用完整 UMO
    （经 AstrBot 适配器发送），ou_/oc_/手机号/邮箱不再作为推送目标。
    """
    t = str(raw or "").strip()
    if not t:
        return None, "目标为空"
    if "##" in t:
        t, _, secret = t.partition("##")
        t = t.strip()
    else:
        secret = ""
    if t.startswith(("http://", "https://")):
        u = urllib.parse.urlsplit(t)
        if u.scheme != "https" or u.hostname not in HOOK_HOSTS:
            return None, "webhook 须为 open.feishu.cn / open.larksuite.com 官方机器人地址"
        if not u.path.startswith(_HOOK_PATH) or len(u.path) <= len(_HOOK_PATH):
            return None, "webhook 路径不合法（应为 /open-apis/bot/v2/hook/xxx）"
        return {"kind": "webhook", "url": t, "secret": secret}, ""
    if t.startswith(("ou_", "oc_", "on_")) or "@" in t or t.isdigit():
        return None, "飞书个人/群消息请填完整 UMO（平台:类型:ID），可从会话选择器点选"
    return None, "无法识别（webhook 地址或完整 UMO）"


def mask_target(target: dict) -> str:
    """目标脱敏展示：日志不落 webhook 地址等敏感信息。"""
    if not isinstance(target, dict):
        return "目标"
    if target.get("kind") == "webhook":
        return f"webhook(…{target['url'][-4:]})"
    return f"{target.get('kind')}({str(target.get('id'))[:6]}…)"


def sign_secret(secret: str, timestamp: int) -> str:
    """官方加签算法：HMAC-SHA256，密钥 f"{timestamp}\\n{secret}"、消息为空，再 base64。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


# ---------- 请求封装 ----------


def _request(host, method, path, *, body=None, headers=None) -> dict:
    """白名单域请求 + JSON 解析；网络层异常与平台错误统一抛 FeishuError。

    webhook 成功响应为 code/StatusCode=0（旧版 webhook 无该字段时视为成功）。
    """
    if host not in HOOK_HOSTS:
        raise FeishuError(f"非飞书官方域名: {host}")
    try:
        status, raw = guarded_request(
            host, method, path, timeout=API_TIMEOUT, body=body, headers=headers
        )
    except NetGuardError as e:
        raise FeishuError(f"出网守卫拒绝: {e}") from e
    except Exception as e:
        raise FeishuError(f"网络请求失败: {e}") from e
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise FeishuError(f"响应解析失败(HTTP {status}): {e}") from e
    code = data.get("code", data.get("StatusCode"))
    if code not in (0, None):
        raise FeishuError(
            f"飞书返回 {code}: {data.get('msg') or data.get('msgZh') or '未知错误'}"
        )
    return data


# ---------- 群自定义机器人（webhook） ----------


def send_webhook(target: dict, text: str):
    """群自定义机器人发文字消息；有加签密钥时按官方算法签名（timestamp+sign）。"""
    u = urllib.parse.urlsplit(target["url"])
    payload = {"msg_type": "text", "content": {"text": text}}
    if target.get("secret"):
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = sign_secret(target["secret"], ts)
    _request(
        u.hostname,
        "POST",
        u.path,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
