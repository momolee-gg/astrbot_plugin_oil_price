"""飞书推送客户端：群自定义机器人 webhook + 自建应用开放接口（im/v1）。

仅依赖 netguard/constants；所有外联经 netguard 出网守卫（官方域名白名单 +
公网解析校验 + IP 直连防 rebinding）。零第三方依赖（http.client 同步实现，
调用方用 asyncio.to_thread 包装）。

- webhook（群）：群里加「自定义机器人」得到 hook 地址，可选加签密钥，只能发
  文字（官方限制：webhook 无法上传图片、无法私发个人）——推送策略上等价于
  「图片关闭」的推送形态。
- 自建应用（人/群）：app_id + app_secret 换取 tenant_access_token 后经
  im/v1/messages 发文字与图片（图片先传 im/v1/images 换 image_key）。
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse

from .constants import API_TIMEOUT, USER_AGENT
from .netguard import NetGuardError, guarded_request

# 官方机器人 webhook 与开放接口仅有的两个域（国内版/国际版），其余一律拒绝
HOOK_HOSTS = ("open.feishu.cn", "open.larksuite.com")
_API_HOST = "open.feishu.cn"
_HOOK_PATH = "/open-apis/bot/v2/hook/"
_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_PATH = "/open-apis/im/v1/messages"
_IMAGE_PATH = "/open-apis/im/v1/images"
_CHATS_PATH = "/open-apis/im/v1/chats"

# tenant_access_token 到期前提前刷新的余量（秒）
_TOKEN_REFRESH_MARGIN = 300

# tenant_access_token 进程内缓存（key=域名|app_id；读写均加锁，兼容 to_thread 并发）
_token_cache = {"key": "", "token": "", "expire_at": 0.0}
_token_lock = threading.Lock()

# 应用消息 receive_id_type 与目标 kind 的对应
_RECEIVE_TYPES = {
    "open_id": "open_id",
    "chat_id": "chat_id",
    "mobile": "mobile",
    "email": "email",
}


class FeishuError(RuntimeError):
    """飞书推送失败（网络/平台错误/业务错误）。"""


# ---------- 目标解析 ----------


def parse_target(raw: str):
    """单条目标串 → (目标 dict 或 None, 错误说明)。

    kind：webhook（url+secret）/ open_id（ou_ 开头）/ chat_id（oc_ 开头）/
    mobile（纯数字）/ email（含 @ 与域名点号）；webhook 允许「地址##加签密钥」
    后缀写法。
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
    if t.startswith("ou_"):
        return {"kind": "open_id", "id": t}, ""
    if t.startswith("oc_"):
        return {"kind": "chat_id", "id": t}, ""
    if "@" in t and "." in t.rsplit("@", 1)[1]:
        return {"kind": "email", "id": t}, ""
    if t.isdigit() and 5 <= len(t) <= 20:
        return {"kind": "mobile", "id": t}, ""
    return None, "无法识别（应为 webhook 地址、ou_、oc_、手机号或邮箱）"


def mask_target(target: dict) -> str:
    """目标脱敏展示：日志不落 webhook 地址与手机号等敏感信息。"""
    if not isinstance(target, dict):
        return "目标"
    if target.get("kind") == "webhook":
        return f"webhook(…{target['url'][-4:]})"
    if target.get("kind") == "mobile":
        mid = str(target["id"])
        return f"mobile({mid[:3]}****{mid[-4:]})"
    return f"{target.get('kind')}({str(target.get('id'))[:6]}…)"


def sign_secret(secret: str, timestamp: int) -> str:
    """官方加签算法：HMAC-SHA256，密钥 f"{timestamp}\\n{secret}"、消息为空，再 base64。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


def api_host(webhook_urls=()) -> str:
    """应用开放接口域名跟随机器人所在云：任一 webhook 为国际版域名则同域，
    否则默认国内版 open.feishu.cn。"""
    for u in webhook_urls or ():
        try:
            if urllib.parse.urlsplit(str(u)).hostname == "open.larksuite.com":
                return "open.larksuite.com"
        except Exception:
            continue
    return _API_HOST


# ---------- 请求封装 ----------


def _request(host, method, path, *, body=None, headers=None) -> dict:
    """白名单域请求 + JSON 解析；网络层异常与平台错误统一抛 FeishuError。

    webhook 与开放平台的成功响应均为 code/StatusCode=0（旧版 webhook 无该字段
    时视为成功）。
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


def _api_json(host, method, path, body=None, token="", headers=None) -> dict:
    """开放平台 JSON 请求的公共头（UA / Bearer token / Content-Type）。"""
    hs = {"User-Agent": USER_AGENT}
    if token:
        hs["Authorization"] = f"Bearer {token}"
    if headers:
        hs.update(headers)
    if body is not None and "Content-Type" not in hs:
        hs["Content-Type"] = "application/json; charset=utf-8"
    return _request(host, method, path, body=body, headers=hs)


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


# ---------- 自建应用（im/v1 开放接口） ----------


def _reset_token_cache():
    """清空 token 缓存（测试用）。"""
    with _token_lock:
        _token_cache.update(key="", token="", expire_at=0.0)


def tenant_access_token(app_id: str, app_secret: str, host: str = _API_HOST) -> str:
    """获取 tenant_access_token（进程内缓存至到期前 300 秒，失败抛 FeishuError）。"""
    key = f"{host}|{app_id}"
    now = time.time()
    with _token_lock:
        if (
            _token_cache["key"] == key
            and _token_cache["token"]
            and now < _token_cache["expire_at"]
        ):
            return _token_cache["token"]
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    data = _api_json(host, "POST", _TENANT_TOKEN_PATH, body)
    token = str(data.get("tenant_access_token") or "")
    if not token:
        raise FeishuError(
            f"获取 tenant_access_token 失败: {data.get('msg') or '响应缺少令牌'}"
        )
    try:
        expire = float(data.get("expire"))
    except (TypeError, ValueError):
        expire = 7200.0
    with _token_lock:
        _token_cache.update(
            key=key,
            token=token,
            expire_at=now + max(60.0, expire - _TOKEN_REFRESH_MARGIN),
        )
    return token


def upload_image(app_id, app_secret, png_path: str, host=_API_HOST) -> str:
    """上传本地 PNG 换 image_key（im/v1/images，multipart/form-data 手工构造）。"""
    token = tenant_access_token(app_id, app_secret, host)
    try:
        with open(png_path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise FeishuError(f"读取图片失败: {e}") from e
    boundary = "----astrbot_oil_" + os.urandom(8).hex()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n',
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="image"; filename="poster.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    resp = _api_json(
        host,
        "POST",
        _IMAGE_PATH,
        body,
        token,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    key = (resp.get("data") or {}).get("image_key")
    if not key:
        raise FeishuError("图片上传成功但响应缺少 image_key")
    return key


def send_app_text(app_id, app_secret, target: dict, text: str, host=_API_HOST):
    """经自建应用发文字消息：receive_id_type 按目标 kind 路由
    （open_id/chat_id/mobile/email）。"""
    token = tenant_access_token(app_id, app_secret, host)
    body = json.dumps(
        {
            "receive_id": target["id"],
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    _api_json(
        host,
        "POST",
        f"{_MESSAGE_PATH}?receive_id_type={_RECEIVE_TYPES[target['kind']]}",
        body,
        token,
    )


def send_app_image(
    app_id, app_secret, target: dict, png_path: str, image_key=None, host=_API_HOST
) -> str:
    """经自建应用发图片消息；image_key 未传时先上传（返回最终 image_key）。"""
    if not image_key:
        image_key = upload_image(app_id, app_secret, png_path, host)
    token = tenant_access_token(app_id, app_secret, host)
    body = json.dumps(
        {
            "receive_id": target["id"],
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    _api_json(
        host,
        "POST",
        f"{_MESSAGE_PATH}?receive_id_type={_RECEIVE_TYPES[target['kind']]}",
        body,
        token,
    )
    return image_key


def list_chats(app_id, app_secret, host=_API_HOST) -> list:
    """列出应用机器人已加入的群 [(chat_id, 群名)]（最多 50 个），供配置群目标。"""
    token = tenant_access_token(app_id, app_secret, host)
    data = _api_json(host, "GET", f"{_CHATS_PATH}?page_size=50", token=token)
    items = (data.get("data") or {}).get("items") or []
    return [
        (str(i.get("chat_id") or ""), str(i.get("name") or "未命名群"))
        for i in items
        if i.get("chat_id")
    ]
