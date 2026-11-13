"""配置访问与校验（Prefs）：插件配置的唯一读写入口。

依赖 modules.constants 与 modules.pushtarget（通用目标串识别）；
main.py 的指令与 Web API 均经由本模块取值/写入。
"""

import logging
import re

from .modules import pushtarget
from .modules.constants import (
    DATASOURCE,
    DEFAULT_FUELS,
    DEFAULT_PROVINCE,
    FUEL_ORDER,
    PROVINCES,
    REGISTER_URL,
)

# 行内调度覆盖格式：weekly:N（N=0~6）与 HH:MM
_ROW_WEEKLY_RE = re.compile(r"^weekly:([0-6])$")
_ROW_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_log = logging.getLogger(__name__)
_on_change_warned = False


class Prefs:
    def __init__(self, config: dict):
        self.config = config

    # ---------- 底层 ----------

    def _get(self, section: str, key: str, default=None):
        try:
            v = self.config.get(section, {}).get(key, default)
            return default if v is None and default is not None else v
        except Exception:
            return default

    def _section(self, section: str) -> dict:
        return self.config.setdefault(section, {})

    # ---------- 读取 ----------

    def app_key(self) -> str:
        return str(self._get("api", "app_key", "") or "").strip()

    def app_key_ready(self) -> bool:
        return bool(self.app_key())

    def gen_model(self) -> str:
        """生图模型名（跑在 bot 已配置模型平台的生图端点上）。"""
        return str(self._get("api", "gen_model", "") or "").strip() or "qwen-image-3.0"

    def gen_enable(self) -> bool:
        """图片消息开关：开且生图端点可用才发图片，否则只发文字。"""
        return bool(self._get("api", "gen_enable", True))

    def gen_cache(self) -> bool:
        """当日生图缓存（oil_1002）：同文本海报当天只生成一次，重复请求直接复用。"""
        return bool(self._get("api", "gen_cache", True))

    def push_frequency(self) -> str:
        """推送频率：daily=每日 / weekly=每周 / off=关闭常规推送（调价提醒独立生效）。

        旧值 on_change（有变动时）自 v1.1 起并入调价提醒（oil_1001 D2），
        读取时自动按 off 迁移，行为等价：平时静默 + 调价落地推送。
        """
        global _on_change_warned
        v = str(self._get("basic", "push_frequency", "daily") or "daily").strip()
        if v == "on_change":
            if not _on_change_warned:
                _on_change_warned = True
                _log.info(
                    "[油价插件] 检测到旧配置 push_frequency=on_change，"
                    "已自动迁移为 off（关闭常规推送）；调价提醒仍按 change_remind_days 生效"
                )
            return "off"
        return v if v in ("daily", "weekly", "off") else "daily"

    def push_weekday(self) -> int:
        """每周推送日（0=周一 … 6=周日），weekly 频率时使用。"""
        try:
            v = int(self._get("basic", "push_weekday", 0))
        except (TypeError, ValueError):
            return 0
        return v if 0 <= v <= 6 else 0

    def change_remind_days(self) -> int:
        """调价提醒天数（0=关闭整个调价提醒），任意推送频率下生效。

        语义（oil_1008）：剩余 1~N 天每日一条推算口径预告；窗口日只在官方
        公告落地后推一次官方口径（若无公告不以估算替代）；调价落地后推送最新油价。
        """
        try:
            v = int(self._get("basic", "change_remind_days", 1))
        except (TypeError, ValueError):
            return 1
        return v if 0 <= v <= 3 else 1

    def auto_provinces(self) -> bool:
        """推送省份自适应开关（oil_1001 D10）：开=推送与缺省查询按目标近期查询偏好选省。"""
        return bool(self._get("basic", "auto_provinces", False))

    def provinces(self) -> list:
        """已配置省份（校验+去重），空/非法时回退默认省。"""
        raw = self._get("basic", "provinces", [])
        items = raw if isinstance(raw, list) else [raw]
        out = []
        for p in items:
            p = str(p).strip()
            if p in PROVINCES and p not in out:
                out.append(p)
        return out or [DEFAULT_PROVINCE]

    def main_province(self) -> str:
        """单省指令（看看油价/我要加油/调价窗口）的缺省省份。"""
        return self.provinces()[0]

    def default_fuels(self) -> list:
        """已配置油号（校验+去重），空/非法时回退默认。"""
        raw = self._get("basic", "default_fuels", [])
        items = raw if isinstance(raw, list) else [raw]
        out = []
        for f in items:
            f = str(f).strip()
            if f in FUEL_ORDER and f not in out:
                out.append(f)
        return out or list(DEFAULT_FUELS)

    def main_fuel(self) -> str:
        """单油号指令（看看油价/我要加油）的缺省油号。"""
        return self.default_fuels()[0]

    def push_time(self) -> tuple:
        def _int(key, default, lo, hi):
            try:
                v = int(self._get("basic", key, default))
            except (TypeError, ValueError):
                return default
            return v if lo <= v <= hi else default

        return _int("push_hour", 9, 0, 23), _int("push_minute", 0, 0, 59)

    def monthly_push_enabled(self) -> bool:
        return bool(self._get("basic", "monthly_push", True))

    # ---------- 通用推送目标行（oil_1010：不区分平台，格式识别 + 调度覆盖） ----------

    def feishu_app_id(self) -> str:
        return str(self._get("api", "feishu_app_id", "") or "").strip()

    def feishu_app_secret(self) -> str:
        return str(self._get("api", "feishu_app_secret", "") or "").strip()

    def feishu_app_ready(self) -> bool:
        """自建应用凭证是否成对就绪（feishu_app 类目标可收海报图片的前提）。"""
        return bool(self.feishu_app_id() and self.feishu_app_secret())

    def push_rows(self) -> list:
        """解析并校验后的推送目标行（群聊在前、个人在后）。

        行字段：raw/kind/identity/umo/feishu + frequency/weekday/hour/minute/limit；
        行内 frequency/time 留空跟随全局默认调度，非法行记日志跳过。
        """
        rows = []
        for key, group in (
            ("push_group_targets", True),
            ("push_private_targets", False),
        ):
            raw = self._get("basic", key, []) or []
            for item in raw if isinstance(raw, list) else []:
                row = self._parse_push_row(item, group)
                if row:
                    rows.append(row)
        return rows

    def _parse_push_row(self, item, group: bool):
        """单行配置 → 解析后的目标行；非法行返回 None 并记日志。"""
        if not isinstance(item, dict):
            _log.warning("[油价插件] 推送目标行格式不正确，已跳过")
            return None
        t, err = pushtarget.resolve_target(str(item.get("id") or ""), group=group)
        if not t:
            _log.warning(
                f"[油价插件] 推送目标行已跳过：{err}（{str(item.get('id'))[:24]}）"
            )
            return None
        freq_raw = str(item.get("frequency") or "").strip()
        freq, weekday = self._resolve_row_freq(freq_raw)
        time_raw = str(item.get("time") or "").strip()
        hour, minute = self._resolve_row_time(time_raw)
        limit = None
        if item.get("limit") not in (None, ""):
            try:
                limit = max(0, int(item.get("limit")))
            except (TypeError, ValueError):
                limit = None
        return {
            "raw": t["raw"],
            "kind": t["kind"],
            "identity": t["identity"],
            "umo": t["umo"],
            "feishu": t["feishu"],
            "frequency": freq,
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
            "limit": limit,
            "sched_override": bool(freq_raw or time_raw),
        }

    def _resolve_row_freq(self, freq_raw: str):
        """行内频率：''=跟随全局默认；daily/off；weekly:N（N=0~6）；非法回退默认。"""
        if not freq_raw:
            return self.push_frequency(), self.push_weekday()
        if freq_raw in ("daily", "off"):
            return freq_raw, 0
        m = _ROW_WEEKLY_RE.fullmatch(freq_raw)
        if m:
            return "weekly", int(m.group(1))
        _log.warning(f"[油价插件] 推送目标行频率非法（{freq_raw}），回退全局默认")
        return self.push_frequency(), self.push_weekday()

    def _resolve_row_time(self, time_raw: str):
        """行内时刻：''=跟随全局默认；HH:MM；非法回退默认。"""
        if not time_raw:
            return self.push_time()
        m = _ROW_TIME_RE.fullmatch(time_raw)
        if not m:
            _log.warning(f"[油价插件] 推送目标行时刻非法（{time_raw}），回退全局默认")
            return self.push_time()
        return int(m.group(1)), int(m.group(2))

    def push_row_identities(self) -> list:
        """全部目标行的标识串（去重状态清理与展示排序用）。"""
        return [r["identity"] for r in self.push_rows()]

    def quota_limit_for(self, umo: str) -> int:
        """查询配额解析：该会话对应的推送行单独限额优先，否则全局默认。"""
        for row in self.push_rows():
            if row["kind"] == "umo" and row["umo"] == umo and row["limit"] is not None:
                return row["limit"]
        return self.user_daily_limit()

    def user_daily_limit(self) -> int:
        """全局默认每人每日查询上限（目标未单独限额时使用）。"""
        try:
            return max(0, int(self._get("basic", "user_daily_limit", 5)))
        except (TypeError, ValueError):
            return 5

    # ---------- Web 载荷 ----------

    def _stored_push_rows(self, key: str) -> list:
        """读取已存储的目标行（规整为 {id, frequency, time, limit} 展示形状）。"""
        raw = self._get("basic", key, []) or []
        out = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            limit = item.get("limit")
            if limit in ("", None):
                limit = None
            out.append(
                {
                    "id": str(item.get("id") or ""),
                    "frequency": str(item.get("frequency") or ""),
                    "time": str(item.get("time") or ""),
                    "limit": limit,
                }
            )
        return out

    def web_payload(self) -> dict:
        hour, minute = self.push_time()
        return {
            "provinces": self.provinces(),
            "push_hour": hour,
            "push_minute": minute,
            "push_frequency": self.push_frequency(),
            "push_weekday": self.push_weekday(),
            "change_remind_days": self.change_remind_days(),
            "auto_provinces": self.auto_provinces(),
            "monthly_push": self.monthly_push_enabled(),
            "gen_enable": self.gen_enable(),
            "gen_cache": self.gen_cache(),
            "gen_model": self.gen_model(),
            "push_group_targets": self._stored_push_rows("push_group_targets"),
            "push_private_targets": self._stored_push_rows("push_private_targets"),
            "user_daily_limit": self.user_daily_limit(),
            "default_fuels": self.default_fuels(),
            "app_key": self.app_key(),
            "app_key_ready": self.app_key_ready(),
            "feishu_app_id": self.feishu_app_id(),
            "feishu_app_secret": self.feishu_app_secret(),
            "feishu_app_ready": self.feishu_app_ready(),
            "datasource": DATASOURCE,
            "register_url": REGISTER_URL,
            "province_options": PROVINCES,
            "fuel_options": FUEL_ORDER,
        }

    def apply_web_update(self, payload: dict) -> list:
        """校验并写入页面提交的配置。返回错误列表，空列表表示全部有效。"""
        errors = []
        if not isinstance(payload, dict):
            return ["无效的请求数据"]

        # 省份（多选）
        provs = payload.get("provinces")
        cleaned_prov = []
        if not isinstance(provs, list) or not provs:
            errors.append("推送省份至少选择一个")
        else:
            cleaned_prov = list(
                dict.fromkeys(str(p).strip() for p in provs if str(p).strip())
            )
            invalid = [p for p in cleaned_prov if p not in PROVINCES]
            if invalid:
                errors.append(f"非法省份：{'、'.join(invalid)}")
            if not cleaned_prov:
                errors.append("推送省份至少选择一个")

        # 推送时间
        def _int_in(key, lo, hi, label, required=True):
            if not required and payload.get(key) in ("", None):
                return None
            try:
                v = int(payload.get(key))
            except (TypeError, ValueError):
                errors.append(f"{label}必须是整数")
                return None
            if not lo <= v <= hi:
                errors.append(f"{label}需在 {lo}~{hi} 之间")
                return None
            return v

        hour = _int_in("push_hour", 0, 23, "推送小时")
        minute = _int_in("push_minute", 0, 59, "推送分钟")

        # 月度推送开关
        monthly = payload.get("monthly_push", True)
        monthly = (
            bool(monthly)
            if isinstance(monthly, bool)
            else str(monthly).lower() in ("1", "true", "on")
        )

        # 推送频率 / 每周推送日 / 调价提醒 / 省份自适应
        freq = str(payload.get("push_frequency", "daily") or "daily").strip()
        if freq == "on_change":  # 旧页面缓存可能仍提交旧值，按迁移规则处理
            freq = "off"
        if freq not in ("daily", "weekly", "off"):
            errors.append("推送频率必须是 daily / weekly / off")
            freq = None
        weekday = _int_in("push_weekday", 0, 6, "每周推送日", required=False)
        remind = _int_in("change_remind_days", 0, 3, "调价提醒天数", required=False)
        auto_prov = payload.get("auto_provinces", False)
        auto_prov = (
            bool(auto_prov)
            if isinstance(auto_prov, bool)
            else str(auto_prov).lower() in ("1", "true", "on")
        )

        # 图片消息开关与生图模型
        gen_enable = payload.get("gen_enable", True)
        gen_enable = (
            bool(gen_enable)
            if isinstance(gen_enable, bool)
            else str(gen_enable).lower() in ("1", "true", "on")
        )
        gen_cache = payload.get("gen_cache", True)
        gen_cache = (
            bool(gen_cache)
            if isinstance(gen_cache, bool)
            else str(gen_cache).lower() in ("1", "true", "on")
        )
        gen_model = str(payload.get("gen_model", "") or "").strip()

        # 通用推送目标行（oil_1010）：{id, frequency, time, limit}，
        # id 格式自动识别平台；frequency/time 留空跟随全局默认调度
        def _push_row_list(key, group, label):
            rows = payload.get(key)
            rows = rows if isinstance(rows, list) else []
            out, seen = [], set()
            for row in rows:
                if not isinstance(row, dict):
                    errors.append(f"{label}格式不正确")
                    continue
                tid = str(row.get("id") or "").strip()
                if not tid:
                    continue
                t, err = pushtarget.resolve_target(tid, group=group)
                if not t:
                    errors.append(f"{label}「{tid[:24]}」{err}")
                    continue
                freq = str(row.get("frequency") or "").strip()
                if freq not in ("", "daily", "off") and not _ROW_WEEKLY_RE.fullmatch(freq):
                    errors.append(f"{label}「{tid[:16]}」频率需为 留空/每日/每周N/关闭")
                    continue
                time_s = str(row.get("time") or "").strip()
                if time_s and not _ROW_TIME_RE.fullmatch(time_s):
                    errors.append(f"{label}「{tid[:16]}」时刻格式需为 HH:MM 或留空")
                    continue
                lim = row.get("limit")
                lim_i = None
                if lim not in (None, ""):
                    try:
                        lim_i = int(lim)
                    except (TypeError, ValueError):
                        errors.append(f"{label}「{tid[:16]}」的限额必须是整数")
                        continue
                    if lim_i < 0:
                        errors.append(f"{label}「{tid[:16]}」的限额不能为负数")
                        continue
                if t["identity"] in seen:
                    continue
                seen.add(t["identity"])
                out.append(
                    {"id": t["raw"], "frequency": freq, "time": time_s, "limit": lim_i}
                )
            return out

        group_rows = _push_row_list("push_group_targets", True, "群聊推送目标")
        priv_rows = _push_row_list("push_private_targets", False, "个人推送目标")

        # 每日上限（全局默认）
        try:
            limit = max(0, int(payload.get("user_daily_limit", 5)))
        except (TypeError, ValueError):
            errors.append("每人每日上限必须是整数")
            limit = None

        # 默认油号（多选）
        fuels = payload.get("default_fuels")
        cleaned_fuel = []
        if not isinstance(fuels, list) or not fuels:
            errors.append("默认油号至少选择一个")
        else:
            cleaned_fuel = list(
                dict.fromkeys(str(f).strip() for f in fuels if str(f).strip())
            )
            invalid = [f for f in cleaned_fuel if f not in FUEL_ORDER]
            if invalid:
                errors.append(f"非法油号：{'、'.join(invalid)}")
            if not cleaned_fuel:
                errors.append("默认油号至少选择一个")

        # appKey
        app_key = str(payload.get("app_key", "") or "").strip()

        # 飞书自建应用凭证（成对校验）
        f_app_id = str(payload.get("feishu_app_id", "") or "").strip()
        f_app_secret = str(payload.get("feishu_app_secret", "") or "").strip()
        if bool(f_app_id) != bool(f_app_secret):
            errors.append("飞书 app_id 与 app_secret 需成对填写")

        if errors:
            return errors

        basic = self._section("basic")
        basic["provinces"] = cleaned_prov
        basic["push_hour"], basic["push_minute"] = hour, minute
        basic["push_frequency"] = freq or "daily"
        if weekday is not None:
            basic["push_weekday"] = weekday
        if remind is not None:
            basic["change_remind_days"] = remind
        basic["auto_provinces"] = auto_prov
        basic["monthly_push"] = monthly
        basic["user_daily_limit"] = limit
        basic["default_fuels"] = cleaned_fuel
        # 通用推送目标行（oil_1010：平台由目标串格式识别，调度行内可覆盖）
        basic["push_group_targets"] = group_rows
        basic["push_private_targets"] = priv_rows
        api = self._section("api")
        api["app_key"] = app_key
        api["gen_enable"] = gen_enable
        api["gen_cache"] = gen_cache
        api["gen_model"] = gen_model
        api["feishu_app_id"] = f_app_id
        api["feishu_app_secret"] = f_app_secret
        return []

    def save(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save(indent=4)
