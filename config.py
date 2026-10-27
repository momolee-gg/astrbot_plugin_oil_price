"""配置访问与校验（Prefs）：插件配置的唯一读写入口。

只依赖 modules.constants；main.py 的指令与 Web API 均经由本模块取值/写入。
"""

import logging

from .modules.constants import (
    DATASOURCE,
    DEFAULT_FUELS,
    DEFAULT_PROVINCE,
    FUEL_ORDER,
    GROUP_TARGET_PREFIX,
    PRIVATE_TARGET_PREFIX,
    PROVINCES,
    REGISTER_URL,
)

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
        """调价提醒天数（0=关闭整个调价提醒：预告 + 落地确认），任意推送频率下生效。

        预告语义：距下次调价窗口剩余 ≤ N 天时每日一条（oil_1001 D3）。
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

    def user_daily_limit(self) -> int:
        """全局默认每人每日查询上限（目标未单独限额时使用）。"""
        try:
            return max(0, int(self._get("basic", "user_daily_limit", 5)))
        except (TypeError, ValueError):
            return 5

    # ---------- 推送目标与限额（目标绑定） ----------

    def _to_umo(self, target_id: str, kind: str):
        """QQ 号/群号 → UMO；完整 UMO 原样；无法识别返回 None。"""
        t = str(target_id or "").strip()
        if not t:
            return None
        if ":" in t:
            return t
        if t.isdigit():
            prefix = GROUP_TARGET_PREFIX if kind == "group" else PRIVATE_TARGET_PREFIX
            return prefix + t
        return None

    def _stored_target_limits(self) -> dict:
        """读取已持久化的 {UMO: limit}，过滤非法项。"""
        raw = self._get("basic", "target_limits", {}) or {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for umo, limit in raw.items():
            try:
                value = int(limit)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                out[str(umo)] = value
        return out

    def _structured_targets(self, key: str, kind: str):
        """读取目标列表，并合并持久化单独限额，返回 ([umo...], {umo: limit})。"""
        raw = self._get("basic", key, []) or []
        stored_limits = self._stored_target_limits()
        ids, limits = [], {}
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                umo = self._to_umo(item.get("id"), kind)
                lim = item.get("limit", stored_limits.get(umo) if umo else None)
            else:  # 兼容旧字符串列表
                umo = self._to_umo(item, kind)
                lim = stored_limits.get(umo) if umo else None
            if not umo or umo in ids:
                continue
            ids.append(umo)
            try:
                lim_i = int(lim) if lim is not None and lim != "" else None
            except (TypeError, ValueError):
                lim_i = None
            if lim_i is not None and lim_i >= 0:
                limits[umo] = lim_i
        return ids, limits

    def notify_targets(self) -> list:
        """推送目标（群聊在前）。"""
        ids_g, _ = self._structured_targets("notify_group", "group")
        ids_p, _ = self._structured_targets("notify_private", "private")
        return ids_g + ids_p

    def target_limits(self) -> dict:
        """每个推送目标的单独每日上限 {UMO: limit}。"""
        _, limits = self._structured_targets("notify_group", "group")
        _, limits_p = self._structured_targets("notify_private", "private")
        limits.update(limits_p)
        return limits

    def quota_limit_for(self, umo: str) -> int:
        """查询配额解析：目标单独限额优先，否则全局默认。"""
        limits = self.target_limits()
        if umo and umo in limits:
            return limits[umo]
        return self.user_daily_limit()

    # ---------- Web 载荷 ----------

    def web_payload(self) -> dict:
        hour, minute = self.push_time()
        _, limits_g = self._structured_targets("notify_group", "group")
        _, limits_p = self._structured_targets("notify_private", "private")

        def display(umo, prefix):
            return umo.removeprefix(prefix)

        priv = [
            {"id": display(u, PRIVATE_TARGET_PREFIX), "limit": limits_p.get(u)}
            for u in self._structured_targets("notify_private", "private")[0]
        ]
        group = [
            {"id": display(u, GROUP_TARGET_PREFIX), "limit": limits_g.get(u)}
            for u in self._structured_targets("notify_group", "group")[0]
        ]
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
            "gen_model": self.gen_model(),
            "private_targets": priv,
            "group_targets": group,
            "user_daily_limit": self.user_daily_limit(),
            "default_fuels": self.default_fuels(),
            "app_key": self.app_key(),
            "app_key_ready": self.app_key_ready(),
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
        gen_model = str(payload.get("gen_model", "") or "").strip()

        # 推送目标（结构化：[{id, limit}]，limit 可空=用全局默认）
        def _structured(key, kind, label):
            rows = payload.get(key)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                errors.append(f"{label}格式不正确")
                return [], {}
            ids, limits = [], {}
            for row in rows:
                tid = str((row or {}).get("id", "") or "").strip()
                if not tid:
                    continue
                umo = self._to_umo(tid, kind)
                if not umo:
                    errors.append(
                        f"{label}目标“{tid}”无法识别（应为 QQ 号/群号或完整 UMO）"
                    )
                    continue
                if umo in ids:
                    continue
                ids.append(umo)
                lim = (row or {}).get("limit", None)
                if lim in ("", None):
                    continue
                try:
                    lim_i = int(lim)
                except (TypeError, ValueError):
                    errors.append(f"目标“{tid}”的限额必须是整数")
                    continue
                if lim_i < 0:
                    errors.append(f"目标“{tid}”的限额不能为负数")
                    continue
                limits[umo] = lim_i
            return ids, limits

        priv_ids, priv_limits = _structured("private_targets", "private", "私聊推送目标")
        group_ids, group_limits = _structured("group_targets", "group", "群聊推送目标")

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
        basic["notify_private"], basic["notify_group"] = priv_ids, group_ids
        target_limits = {}
        target_limits.update(group_limits)
        target_limits.update(priv_limits)
        basic["target_limits"] = target_limits
        basic["user_daily_limit"] = limit
        basic["default_fuels"] = cleaned_fuel
        api = self._section("api")
        api["app_key"] = app_key
        api["gen_enable"] = gen_enable
        api["gen_model"] = gen_model
        return []

    def save(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save(indent=4)
