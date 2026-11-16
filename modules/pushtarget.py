"""通用推送目标识别器（oil_1010）：配置不再区分平台，目标串自动识别派发。

kind 两类：
  umo            AstrBot 统一会话源（QQ 号/群号自动补 default: 前缀；任意平台完整
                 UMO 原样通过，如 猫饼:GroupMessage:oc_xxx——新平台零配置扩展点）。
                 文字与海报图片均由 AstrBot 平台适配器发送（适配器用平台自带的
                 凭证处理飞书图片上传等）。
  feishu_webhook 飞书群自定义机器人（webhook 地址，可选 ##加签密钥；官方限制仅文字）。

群/私由调用方以 kind_hint 限定（纯数字的 QQ 号与群号无法从格式区分）。
飞书 ou_/oc_/手机号/邮箱不再是推送目标形态——个人/群消息统一填完整 UMO
（可从设置页会话选择器点选或 /油价订阅 获取）。仅依赖 feishu/constants。
"""

import re

from . import feishu
from .constants import GROUP_TARGET_PREFIX, PRIVATE_TARGET_PREFIX

# 中国大陆手机号：与 QQ 号（≤11 位纯数字）无法完全互斥，不再作为目标形态
_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")


def resolve_target(raw: str, group: bool = True):
    """通用目标串 → (目标 dict 或 None, 错误说明)。

    group=True 群聊列表：QQ 群号 / 完整 UMO / 飞书 webhook；
    group=False 个人列表：QQ 号 / 完整 UMO。

    返回 dict 字段：
      kind     umo / feishu_webhook
      raw      用户填写的目标串（原样）
      identity 去重状态键的行标识（umo 串或 webhook 串）
      umo      kind=umo 时的会话源
      feishu   feishu_webhook 时的目标结构（url/secret）
    """
    t = str(raw or "").strip()
    if not t:
        return None, "目标为空"
    # 1) URL：仅认飞书官方 webhook（feishu.parse_target 校验域名白名单与路径）
    if t.startswith(("http://", "https://")):
        f, err = feishu.parse_target(t)
        if not f or f["kind"] != "webhook":
            return None, err or "webhook 仅支持飞书官方自定义机器人地址"
        return _pack("feishu_webhook", t, feishu=f), ""
    # 2) 飞书 ID 形态（ou_/oc_/on_/手机号/邮箱）：不再是目标，提示改用完整 UMO
    if t.startswith(("ou_", "oc_", "on_")) or "@" in t or _MOBILE_RE.match(t):
        return None, "飞书个人/群消息请填完整 UMO（平台:类型:ID），可从会话选择器点选"
    # 3) 完整 UMO：含冒号的非 URL 串原样通过（任意 AstrBot 平台通用）
    if ":" in t:
        return _pack("umo", t, umo=t), ""
    # 4) 纯数字：QQ 号/群号（按列表语义补前缀）；其余无法识别
    if t.isdigit():
        umo = (GROUP_TARGET_PREFIX if group else PRIVATE_TARGET_PREFIX) + t
        return _pack("umo", t, umo=umo), ""
    return None, "无法识别（QQ号/群号、完整UMO、飞书webhook）"


def _pack(kind: str, raw: str, *, umo: str = None, feishu: dict = None) -> dict:
    """统一行结构：identity 用作去重状态键的行标识。"""
    if kind == "umo":
        identity = umo
    else:
        f = feishu or {}
        secret = f.get("secret") or ""
        identity = f["url"] + (f"##{secret}" if secret else "")
    return {
        "kind": kind,
        "raw": raw,
        "identity": identity,
        "umo": umo,
        "feishu": feishu,
    }
