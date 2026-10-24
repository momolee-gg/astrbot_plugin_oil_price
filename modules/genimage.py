"""AI 生图：当天文字消息 → 提示词 → bot 已配置模型的生图端点 → 下载 PNG。

不再自建 API Key / 模型配置：密钥与 api_base 全部取自 AstrBot 已配置的
provider（provider_config），插件只负责把文字消息变成海报提示词并调端点。
仅依赖标准库。生成失败抛 GenImageError，由调用方回退纯文字推送。
"""

import base64
import json
import os
import re
import urllib.parse
import urllib.request

DEFAULT_MODEL = "qwen-image-3.0"
TIMEOUT = 180
SIZE = "864x1152"

# 阿里云百炼 qwen-image 走原生多模态接口（非 OpenAI images 协议）
_DASHSCOPE_NATIVE = "/api/v1/services/aigc/multimodal-generation/generation"

# 单油号数据行尾部涨幅，如「92号汽油：8.58 元/升（↑涨 0.32）」→ (主体, ↑涨 0.32)
_ROW_CHANGE_RE = re.compile(r"^(?P<main>.*元/升)\s*[（(](?P<chg>[↑↓][^（）()]*)[)）]\s*$")


class GenImageError(RuntimeError):
    """生图调用失败（无可用端点/网络/鉴权/无图返回）。"""


# 内置风格库：随机轮换用（同一风格不连续出现）
STYLES = [
    {
        "name": "红色促销",
        "text": (
            "高饱和度红色促销电商海报风：顶部是深红色斜纹质感通栏横幅，横幅左侧一个巨大的红色扩音喇叭插画"
            "加红色警灯图标，右侧一个红色警灯图标，横幅内是超大号加粗描边标题字（重点词用亮黄色、其余白色）；"
            "标题下方是白色大圆角主面板：面板内第一行是红底白字的加油机图标胶囊行，"
            "数据行每行的油号名称分别装在绿色（92号）、蓝色（95号）、紫色（98号）的实色圆角胶囊标签里（白字加粗），"
            "价格用特大号红色加粗数字，单位灰色小字，行尾涨幅装在浅粉色圆角小胶囊里（红色箭头+红色数字）；"
            "面板下方是亮黄色大圆角横幅区：左侧一个大的黄色闪电插画图标，文字为黑色粗体、关键词用红色笔刷横条"
            "内白字突出，右下角配一个卡通加油站插画（红白条纹雨棚、红色加油机、蓝天草地背景）；"
            "最底部是深蓝色圆角通栏横条：白色日历图标 + 白色文字 + 特大号亮黄色加粗数字；"
            "整体配色为红黄蓝绿紫的高饱和撞色，圆角卡片层次分明，喜庆热闹的促销氛围"
        ),
    },
    {
        "name": "清新卡通",
        "text": (
            "扁平卡通插画风格，天蓝色渐变天空配白云，底部绿色草地和卡通加油站建筑、可爱小汽车，"
            "白色圆角信息面板，配色明快清爽，圆角图标可爱友好"
        ),
    },
    {
        "name": "深色高级",
        "text": (
            "深色高级感设计风：深蓝黑渐变背景带细腻光晕，半透明玻璃拟态圆角卡片承载信息，"
            "红金撞色点缀（涨幅与重点文字用亮红色+金色描边强调），图标为精致的渐变立体小图标，"
            "整体像专业金融资讯海报"
        ),
    },
    {
        "name": "手绘水彩",
        "text": (
            "温暖的手绘水彩插画风格：奶油色纸纹背景，柔和的橙红暖色调，手绘描边贴纸感图标"
            "（加油机、小汽车、日历均为水彩贴纸风），信息区为略带纸张阴影的白色圆角卡片，"
            "像精致的手账日报"
        ),
    },
    {
        "name": "简约杂志",
        "text": (
            "简约杂志资讯风：大面积留白的米白背景，黑色超大号标题字配一条红色强调色带，"
            "数据区为细线分隔的极简表格式排版，价格数字用衬线感大字，图标为细线条单色图标，"
            "整体像财经杂志的信息版面，克制专业"
        ),
    },
]


def pick_style_index(last: int = None) -> int:
    """随机挑一个风格索引，保证与上一次不同。"""
    import random

    choices = [i for i in range(len(STYLES)) if i != last]
    return random.choice(choices)


def _strip_emoji(line: str) -> str:
    """去掉行首的 emoji 修饰符（生图时由插画图标代替）。"""
    return re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+", "", line).strip()


def prompt_from_text(text: str, style: str = "") -> str:
    """把当天的文字消息按行拆进海报生成提示词（图文同源的唯一来源）。

    每一行原文加引号并指定其画在海报哪个区块，让模型逐块照抄，
    避免「最后再对照长文」造成的数字错漏与版式跑偏。
    style: 追加在版式规则后的风格描述（如「深色玻璃拟态高级风」）；
    为空时用默认的扁平卡通插画风格。
    """
    body = (text or "").strip()
    if not body:
        raise GenImageError("文字消息为空，无法生成图片")

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    title = _strip_emoji(lines[0])
    fuels = list(dict.fromkeys(re.findall(r"(\d{1,2})号", body)))

    style_txt = style.strip() or "扁平卡通插画风格，配色明快"
    spec = [
        f"根据下面的逐行内容，画一张竖版油价信息海报（QQ群每日播报用）。"
        f"{style_txt}，适合手机竖屏查看。"
    ]

    info = ""
    rows, notice, countdown = [], [], []
    rest = lines[1:]
    if "油价（" in title or "速览" in title:
        # 下跌/持平日没有醒目标题行，首行即省份信息行：补一个通用标题
        info = title
        title = "今日油价"
    spec.append(f"1. 顶部标题区：左右各一个红色警灯插画图标，中间大字标题「{title}」")

    for ln in rest:
        if "距下次调价窗口" in ln:
            countdown.append(ln)
        elif "元/升" in ln:
            rows.append(ln)
        elif "油价（" in ln or "速览" in ln:
            info = ln
        else:
            notice.append(ln)
    if info:
        spec.append(
            f"2. 标题下方信息行：加油机插画图标 + 「{_strip_emoji(info)}」。"
            f"这一行里的日期在整个海报中只允许出现这一次，严禁重复"
        )
    else:
        spec.append("2. （无省份信息行，跳过）")

    spec.append(
        f"3. 中部白色圆角数据面板：{len(rows)} 行油价数据，每行一个条目，"
        f"多行严格左对齐——油号名称、冒号、价格、单位逐列上下对齐："
    )
    for i, row in enumerate(rows, 1):
        m = _ROW_CHANGE_RE.match(row)
        if m:
            chg = m.group("chg").strip()
            pill = "红色" if chg.startswith("↑") else "绿色"
            spec.append(
                f"   第{i}行「{m.group('main').strip()}」，"
                f"行尾画一个醒目的{pill}圆角小胶囊，胶囊内白字「{chg}」"
            )
        else:
            spec.append(f"   第{i}行「{_strip_emoji(row)}」")
    if rows:
        spec.append(
            "   胶囊只用一个统一的粗箭头符号（上涨用红色向上箭头、下跌用绿色向下箭头），"
            "严禁混用闪电、菱形、火苗等其他符号；胶囊之外不得再画任何涨幅符号"
        )

    if notice:
        notice_txt = "；".join(_strip_emoji(ln) for ln in notice)
        spec.append(
            f"4. 提示区：闪电插画小图标 + 「{notice_txt}」。"
            f"若内容含「上调」，这两个字用红色加粗放大突出；"
            f"若含「下调」，用绿色加粗放大突出；持平类提示语正常排版即可"
        )
    else:
        spec.append("4. （无提示区，跳过）")

    if countdown:
        cd = _strip_emoji(countdown[0])
        spec.append(
            f"5. 底部倒计时区：红色日历插画图标 + 「{cd}」，天数数字用特大号红色突出"
        )
    else:
        spec.append("5. （无倒计时区，跳过）")

    rule_fuels = "、".join(fuels) if fuels else "原文出现的"
    spec.append(
        "【硬性规则】\n"
        f"- 引号「」内的每个字、每个数字都必须原样画进海报，一个不能少、一个不能改；"
        f"油号只能是 {rule_fuels}，严禁写成 99、97 等其他数字；\n"
        "- 引号外的任何文字（包括本提示词的其他内容）都不允许出现在海报上；\n"
        "- 播报文字里的emoji不要画成字符，改画成对应插画图标"
        "（🚨=警灯、⛽=加油机、⚡=闪电、📅=日历、🎉=彩带庆祝、💡=灯泡），"
        "其余emoji同理画成贴合含义的插画图标；\n"
        "- 数据行只有引号里的内容，不要自行补画箭头、火苗、钻石等装饰符号。"
    )

    return "\n".join(spec) + "\n【播报原文（核对用）】\n" + body


def endpoints_from_provider_config(cfg: dict) -> list:
    """从 provider 配置提取 (api_key, 生图端点) 列表。

    cfg 即 AstrBot provider 的 provider_config（含 key 列表与 api_base）。
    百炼（dashscope）映射到原生多模态生图接口，其余按 OpenAI 兼容协议拼
    images/generations。
    """
    keys = (cfg or {}).get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    base = str((cfg or {}).get("api_base") or "").strip().rstrip("/")
    if not base:
        return []
    if "dashscope" in base:
        parts = urllib.parse.urlsplit(base)
        url = f"{parts.scheme}://{parts.netloc}{_DASHSCOPE_NATIVE}"
    else:
        url = base + "/images/generations"
    out = []
    for k in keys:
        k = str(k or "").strip()
        if k:
            out.append((k, url))
    return out


def _gen_once(prompt: str, api_key: str, url: str, model: str, size: str):
    """调一次生图端点，返回图片 URL（b64 响应转成 data 未处理，统一走 URL/字节）。

    返回 ("url", url) 或 ("b64", b64_str)；失败抛异常。
    """
    if "multimodal-generation" in url:
        payload = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        }
        if size:
            payload["parameters"] = {"size": size.replace("x", "*")}
    else:
        payload = {"model": model, "prompt": prompt, "size": size}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        root = json.loads(resp.read().decode("utf-8"))

    if "multimodal-generation" in url:
        content = (
            ((root.get("output") or {}).get("choices") or [{}])[0].get("message") or {}
        ).get("content") or []
        for item in content:
            if item.get("image"):
                return "url", item["image"]
        raise RuntimeError(f"生图无返回图片: {root.get('error') or root}")

    data = (root.get("data") or [{}])[0]
    if data.get("b64_json"):
        return "b64", data["b64_json"]
    if data.get("url"):
        return "url", data["url"]
    raise RuntimeError(f"生图无返回图片: {root.get('error') or root}")


def generate(
    prompt: str,
    endpoints: list,
    model: str = DEFAULT_MODEL,
    size: str = SIZE,
    out_dir: str = "/tmp",
    name: str = "gen_poster.png",
) -> str:
    """逐个尝试端点调生图并下载，返回本地 PNG 路径；失败抛 GenImageError。

    endpoints: [(api_key, url), ...]，顺序即优先级（正在使用的 provider 在前）。
    """
    if not endpoints:
        raise GenImageError("未从 bot 配置的模型提供者取到生图端点")
    last_err = None
    for api_key, url in endpoints:
        try:
            kind, img = _gen_once(prompt, api_key, url, model or DEFAULT_MODEL, size)
        except Exception as e:
            last_err = GenImageError(f"生图请求失败[{url}]: {e}")
            continue
        try:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, name)
            if kind == "b64":
                with open(path, "wb") as f:
                    f.write(base64.b64decode(img))
            else:
                urllib.request.urlretrieve(img, path)
            if os.path.getsize(path) < 1024:
                raise GenImageError("下载图片过小，疑似失败")
            return path
        except GenImageError:
            raise
        except Exception as e:
            raise GenImageError(f"生图下载失败: {e}") from e
    raise last_err or GenImageError("生图失败：无可用端点")
