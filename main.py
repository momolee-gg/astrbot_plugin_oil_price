"""astrbot_plugin_oil_price —— 全国油价查询与出图播报（ShowAPI 数据源）。

架构（模块间不耦合，均只依赖 modules/constants；charts 复用 reporting 的文案函数）：
  modules/constants.py   全局常量（零依赖）
  modules/netguard.py    共享出网守卫（公网解析校验 + IP 直连防 rebinding）
  modules/datasource.py  ShowAPI 数据源客户端
  modules/feishu.py      飞书推送客户端（webhook + 自建应用 im/v1）
  modules/pushtarget.py  通用推送目标识别器（QQ/UMO/飞书 webhook/应用目标）
  modules/storage.py     本地存储（查询历史 / 用户配额 / 查询偏好事件）
  modules/reporting.py   文字文案（单省报告 / 多省速览 / 倒计时 / 上涨提醒）
  modules/charts.py      PIL 渲染（单油卡 / 结算单 / 折线图）
  modules/genimage.py    AI 生图（当天文字消息 → 提示词 → bot 已配置模型的生图端点）
  config.py              配置读写与校验（Prefs，唯一配置访问层）
  main.py                插件入口：指令编排、定时推送（每日/每月）、统计与设置页 API

推送体系（oil_1001）：常规推送（每日/每周/关闭）与调价提醒（预告+落地确认，独立开关）
按调价窗口相位组织；auto_provinces 开启时按目标近期查询偏好分省份集推送。
通用推送（oil_1010）：目标行不区分平台（QQ/UMO/飞书 webhook/应用目标格式自动识别），
调度=全局默认+行内可覆盖；主循环按「时:分」分槽批量执行，去重状态键=行标识::省集，
发送按行能力分发（umo 走 UMO 主动消息、feishu 行走 webhook/自建应用）。
"""

import asyncio
import base64
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as BotImage
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:  # 官方 api 面优先，旧版本回退 core 路径
    from astrbot.api.event import EventMessageType
except ImportError:  # pragma: no cover
    from astrbot.core.platform.astr_message_event import EventMessageType

from .config import Prefs
from .modules import (
    charts,
    datasource,
    feishu,
    forecast,
    genimage,
    pushtarget,
    reporting,
)
from .modules.constants import (
    CACHE_MIN_BYTES,
    FUEL_NAMES,
    FUEL_ORDER,
    GROUP_TARGET_PREFIX,
    PRIVATE_TARGET_PREFIX,
    PROVINCES,
    TMP_KEEP_DAYS,
    TMP_KEEP_MONTHLY_DAYS,
)
from .modules.storage import Store, pick_preferred_provinces

PLUGIN_NAME = "astrbot_plugin_oil_price"
PLUGIN_VERSION = "1.5.0"
DATA_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
QUOTA_FILE = os.path.join(DATA_DIR, "user_quota.json")
EVENTS_FILE = os.path.join(DATA_DIR, "query_events.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "seen_sessions.json")
TMP_DIR = os.path.join(DATA_DIR, "tmp")

NO_KEY_HINT = (
    "⚠️ 尚未配置 ShowAPI appKey，插件功能未启用。\n"
    "请在管理面板侧边栏「油价设置」页填写后保存。"
)


@register(
    PLUGIN_NAME,
    "momolee-gg",
    "全国油价查询与出图播报（ShowAPI 数据源）：多省推送、"
    "AI 生图每日海报（5 风格按日轮换，同日同风格）、推送频率（每日/每周/关闭）、"
    "调价提醒（剩余≤N天预告 + 落地确认，AI 图片）、通用推送目标（QQ/飞书/任意 "
    "UMO 平台混配，全局默认调度+目标行覆盖）、推送省份自适应查询偏好、"
    "油价走势折线图、月度走势推送、加油结算单、"
    "目标绑定查询限额、WebUI 设置与统计页",
    PLUGIN_VERSION,
)
class OilPricePlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.prefs = Prefs(config)
        self.store = Store(HISTORY_FILE, QUOTA_FILE, EVENTS_FILE, SESSIONS_FILE)
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        self._cleanup_tmp()
        self._tasks = []
        # 槽分调度守卫（oil_1010）：{「HH:MM」: 已推送日期} 与月度图 {「HH:MM」: 已推送月份}
        self._pushed_slots = {}
        self._pushed_month_slots = {}
        # 见过的会话：每会话限频落盘（高频群不逐条写盘），时间戳进程内保存
        self._seen_flushed = {}
        self._loop_started = False
        self._cleaned_date = None
        self._announce_last_slot = None
        self._state_file = os.path.join(DATA_DIR, "push_state.json")

        context.register_web_api(
            f"/{PLUGIN_NAME}/config/get", self._web_config_get, ["GET"], "config_get"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/config/save",
            self._web_config_save,
            ["POST"],
            "config_save",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/stats/render",
            self._web_stats_render,
            ["POST"],
            "stats_render",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/prefs/list",
            self._web_prefs_list,
            ["GET"],
            "prefs_list",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/prefs/manage",
            self._web_prefs_manage,
            ["POST"],
            "prefs_manage",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/sessions/list",
            self._web_sessions_list,
            ["GET"],
            "sessions_list",
        )

        self._ensure_push_loop()

    async def terminate(self):
        """插件卸载/重载时取消后台任务（AstrBot 生命周期钩子）。"""
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("[油价插件] 已清理后台推送任务")

    # ================= 会话记录（oil_1010：设置页「从记录会话选择」数据源） =================

    _SEEN_FLUSH_INTERVAL = 600  # 每会话限频落盘窗口（秒）：高频群不逐条写盘

    @filter.event_message_type(EventMessageType.ALL)
    async def record_seen_session(self, event: AstrMessageEvent):
        """被动记录见过的会话（只记录不回复）：设置页「从记录会话选择」的数据源。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            now_ts = time.time()
            last = self._seen_flushed.get(umo)
            if last is not None and now_ts - last < self._SEEN_FLUSH_INTERVAL:
                return
            self._seen_flushed[umo] = now_ts
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            label = str(getattr(sender, "nickname", "") or "") if sender else ""
            self.store.record_seen_session(umo, label)
        except Exception as e:
            logger.error(f"[油价插件] 记录会话失败: {e}")

    # ================= 定时推送（每日 + 每月） =================

    def _ensure_push_loop(self):
        """appKey 就绪时保证推送定时器在跑（只启动一次）。"""
        if self._loop_started:
            return
        if not self.prefs.app_key_ready():
            logger.warning(
                "[油价插件] 未配置 appKey，推送功能停用；请在 WebUI「油价设置」页填写并保存"
            )
            return
        try:
            self._tasks.append(asyncio.get_running_loop().create_task(self._daily_loop()))
            self._loop_started = True
            logger.info("[油价插件] 推送定时器已启动")
        except RuntimeError:
            logger.warning("[油价插件] 事件循环未就绪，推送定时器暂缓启动")

    async def _daily_loop(self):
        logger.info("[油价插件] 推送定时器运行中（每 20 秒检查，配置热读取）")
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                month = now.strftime("%Y-%m")
                if self._cleaned_date != today:
                    self._cleaned_date = today
                    self._cleanup_tmp()
                # 槽分调度（oil_1010）：目标行按生效时刻（行覆盖>全局默认）分槽，
                # 到点的槽当天只执行一轮；同槽目标批量执行共享文案与生图缓存
                slots = {}
                for row in self.prefs.push_rows():
                    slots.setdefault((row["hour"], row["minute"]), []).append(row)
                for (h, m), slot_rows in sorted(slots.items()):
                    if now.hour != h or now.minute != m:
                        continue
                    slot = f"{h:02d}:{m:02d}"
                    if self._pushed_slots.get(slot) != today:
                        self._pushed_slots[slot] = today
                        await self._push_round(slot_rows, now)
                    if (
                        self.prefs.monthly_push_enabled()
                        and now.day == 1
                        and self._pushed_month_slots.get(slot) != month
                    ):
                        self._pushed_month_slots[slot] = month
                        await self._monthly_push(slot_rows)
                self._prune_slot_guards(today, month)
                if self._claim_announce_slot(now):
                    try:
                        await self._official_announce_push(now)
                    except Exception as e:
                        logger.error(f"[油价插件] 官方公告轮询异常: {e}")
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[油价插件] 推送循环异常: {e}")
                await asyncio.sleep(60)

    def _prune_slot_guards(self, today: str, month: str):
        """槽守卫只保留当天/当月条目，防目标行调整后无限增长。"""
        for key in list(self._pushed_slots):
            if self._pushed_slots[key] != today:
                self._pushed_slots.pop(key)
        for key in list(self._pushed_month_slots):
            if self._pushed_month_slots[key] != month:
                self._pushed_month_slots.pop(key)

    # ================= 推送状态（风格轮换 / 变动快照 / 提醒去重） =================

    def _load_push_state(self) -> dict:
        try:
            with open(self._state_file, encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            return {}

    def _save_push_state(self, st: dict):
        tmp = self._state_file + ".tmp"
        Path(tmp).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state_file)

    def _daily_style(self, date: str) -> str:
        """当日统一风格（oil_1005）：风格按「天」轮换，与消息类型/渠道无关——
        同一天内查询、推送、调价提醒的所有海报共用同一风格；每天随机换一个
        与前一日不同的风格。"""
        st = self._load_push_state()
        idx = st.get("style_idx")
        if (
            st.get("style_date") == date
            and isinstance(idx, int)
            and 0 <= idx < len(genimage.STYLES)
        ):
            return genimage.STYLES[idx]["text"]
        idx = genimage.pick_style_index(idx if isinstance(idx, int) else None)
        st["style_idx"] = idx
        st["style_date"] = date
        self._save_push_state(st)
        logger.info(f"[油价插件] 今日生图风格：{genimage.STYLES[idx]['name']}")
        return genimage.STYLES[idx]["text"]

    def _image_ready(self) -> bool:
        """图片消息开关：gen_enable 开且 bot 配置的 provider 里有生图端点。"""
        return bool(self.prefs.gen_enable() and self._gen_endpoints())

    # ================= 价格快照（落地确认用） =================

    def _price_snapshot(self, rows) -> dict:
        return {
            prov: {
                k: e["price"]
                for k, e in (data.get("fuels") or {}).items()
                if e.get("price") is not None
            }
            for prov, data in rows
        }

    # ================= 推送轮（oil_1001：常规推送 + 调价提醒，按省份集分组） =================

    def _set_key(self, provs: list) -> str:
        return "、".join(provs)

    @staticmethod
    def _norm_provs(provs: list) -> list:
        """省份列表按省份表固定顺序归一（oil_1002）：多省文案行序、推送分组
        与用户输入顺序解耦——同省集不同顺序共享同一文案与缓存图。"""
        return sorted(provs, key=PROVINCES.index)

    def _target_province_sets(self, targets: list) -> dict:
        """目标行 → 省份集分组 {tuple(provs): [row...]}；auto_provinces 开启时
        各目标可不同，相同省份集共享一次文案与生图（F9）。"""
        sets = {}
        for row in targets:
            provs = tuple(self._resolve_provinces_for(row["identity"]))
            sets.setdefault(provs, []).append(row)
        return sets

    def _resolve_provinces_for(self, key: str) -> list:
        """目标缺省省份（F8）：auto_provinces 开启时按该目标近期查询偏好选取
        （群聊按成员去重聚合、私聊按本人），记录不足回退配置省份。
        key 为行标识（QQ 目标即 UMO；其他平台为各自的目标串）。"""
        if not self.prefs.auto_provinces():
            return self._norm_provs(self.prefs.provinces())
        picked = pick_preferred_provinces(self.store.query_events(key))
        return (
            self._norm_provs(picked) if picked else self._norm_provs(self.prefs.provinces())
        )

    def _record_query(self, event: AstrMessageEvent, provs: list):
        """记录一次成功的主动查询事件（F7），供省份自适应聚合；推送拉取不计入。"""
        if not provs:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            self.store.record_query(umo, event.get_sender_id() or "", list(provs))
        except Exception as e:
            logger.error(f"[油价插件] 记录查询偏好失败: {e}")

    def _row_key(self, row: dict, set_key: str) -> str:
        """去重状态键 = 行标识::省集（oil_1010：目标行级隔离）。"""
        return f"{row['identity']}::{set_key}"

    async def _push_round(self, slot_rows: list, now: datetime):
        """某个「时:分」槽到点的一轮推送（F1~F3）：常规推送按行频率（行覆盖
        全局默认），调价提醒独立生效（剩余≤N天预告 + 落地确认）；按目标省份集
        分组执行，省集内逐行相位决策。"""
        if not self.prefs.app_key_ready():
            logger.warning("[油价插件] 未配置 appKey，本轮推送跳过")
            return
        if not slot_rows:
            return
        set_map = self._target_province_sets(slot_rows)
        for provs, rows in set_map.items():
            try:
                await self._push_round_one(list(provs), rows, now)
            except Exception as e:
                logger.error(f"[油价插件] 省份集 {'、'.join(provs)} 推送异常: {e}")
        self._prune_push_state(set_map)

    async def _push_round_one(self, provs: list, rows: list, now: datetime):
        """单个省份集的行级推送决策：每行独立判断常规（行频率，oil_1010）/
        落地确认（行快照有变动且该行今天未发常规）/预告（行级去重）。
        去重规则见 F3：当天已发常规卡片则落地确认不重发；单省文字卡片自带
        倒计时则预告不重发。状态键 = 行标识::省集。"""
        data = await self._collect_today(provs)
        if not data:
            logger.error(f"[油价插件] 推送取消：{'、'.join(provs)} 全部查询失败")
            return
        set_key = self._set_key(provs)
        remind_n = self.prefs.change_remind_days()
        for row in rows:
            row_key = self._row_key(row, set_key)
            regular_sent = False
            if row["frequency"] == "daily" or (
                row["frequency"] == "weekly" and now.weekday() == row["weekday"]
            ):
                await self._push_report(data, [row])
                regular_sent = True
            if remind_n <= 0:
                continue
            # 相位③ 落地确认：该行快照有变动且该行今天未发常规（首次见仅记基线）
            changed = self._snapshot_changed(row_key, data)
            if changed and not regular_sent:
                logger.info("[油价插件] 调价落地：价格有变动，推送最新油价")
                await self._push_report(data, [row])
            # 相位①② 预告：剩余 ≤ N 天每日一条；单省常规已含倒计时的行不重发
            if not (regular_sent and len(data) == 1):
                await self._advance_remind(data, [row])

    def _snapshot_changed(self, set_key: str, rows: list):
        """按省份集对比价格快照。首次见到该集只记基线返回 None，否则返回是否有变动。"""
        st = self._load_push_state()
        snaps = st.setdefault("price_snaps", {})
        cur = self._price_snapshot(rows)
        old = snaps.get(set_key)
        snaps[set_key] = cur
        self._save_push_state(st)
        return None if old is None else old != cur

    def _prune_push_state(self, set_map: dict):
        """快照与预告去重状态只保留当前目标行的键（行标识::省集），
        防目标调整/换集后无限增长。"""
        st = self._load_push_state()
        keep = {
            self._row_key(row, self._set_key(list(provs)))
            for provs, rows in set_map.items()
            for row in rows
        }
        dirty = False
        for key in ("price_snaps", "remind_done", "official_done"):
            box = st.get(key)
            if not box:
                continue
            stale = [k for k in box if k not in keep]
            for k in stale:
                box.pop(k)
                dirty = True
        if dirty:
            self._save_push_state(st)

    async def _advance_remind(
        self, rows: list, targets: list = None, force: bool = False
    ) -> bool:
        """调价预告（相位①②，F6）：距窗口剩余 1~N 天时发一条**推算口径**提醒，
        每日每目标行至多一条（状态键=行标识::省集，oil_1010）；窗口日（剩余 0 天）
        不发——当天由官方公告推送接管（oil_1008，措辞「预计/已公告」严格分型）。
        文案含每省倒计时与当前油价明细。gen_enable 开且端点可用走 AI 海报
        （单省=文字+图片、多省=仅图片），失败回退纯文字；图片能力按行判定
        （umo 行可收图、feishu_app 行需凭证、webhook 行仅文字）。

        force=True（/油价调整 手动触发）：无视 change_remind_days 开关、≤N 天门控
        与当日去重，窗口日也发送（官方口径优先，build_remind_text 按 fc 分型）；
        且**不写入去重状态**（不影响自动推送节奏）。返回是否发生了推送。
        """
        today = self.store.today()
        row = (targets or [None])[0]
        if not row:
            return False
        days_n = self.prefs.change_remind_days()
        state_key = self._row_key(row, self._set_key([p for p, _ in rows]))
        st = self._load_push_state()
        if not force and (
            days_n <= 0 or (st.get("remind_done") or {}).get(state_key) == today
        ):
            return False
        remind_rows, min_days = [], None
        for prov, data in rows:
            days, _ = reporting.days_until(data.get("next_change_time") or "")
            if days is None:
                continue
            min_days = days if min_days is None else min(min_days, days)
            remind_rows.append((prov, data))
        if min_days is None or not remind_rows:
            return False
        if not force and not 1 <= min_days <= days_n:
            # 窗口日（min_days=0）不在预告范围：官方公告推送当天接管
            return False
        fc = None
        try:
            fc = await asyncio.to_thread(forecast.get_today_forecast, today)
        except Exception as e:
            logger.warning(f"[油价插件] 调价预测获取失败，本条提醒不带预测: {e}")
        text = reporting.build_remind_text(
            remind_rows, self.prefs.default_fuels(), forecast=fc
        )
        if not text:
            return False
        image_ok = self._image_ready() and self._row_image_capable(row)
        try:
            if not image_ok:
                await self._push_text(text, targets)
            elif len(rows) == 1:
                path = await self.get_or_gen_image(
                    text,
                    f"ai_remind_{rows[0][0]}",
                    today,
                    provs=[rows[0][0]],
                )
                await self._push_text(text, targets)
                await self._push_image(path, targets)
            else:
                path = await self.get_or_gen_image(
                    text,
                    "ai_remind_multi",
                    today,
                    provs=[p for p, _ in rows],
                )
                await self._push_image(path, targets)
        except Exception as e:
            logger.error(f"[油价插件] 调价提醒出图失败，回退文字: {e}")
            await self._push_text(text, targets)
        if not force:
            st = self._load_push_state()
            st.setdefault("remind_done", {})[state_key] = today
            self._save_push_state(st)
        logger.info("[油价插件] 已发送调价提醒")
        return True

    # ================= 官方公告推送（窗口日当天，oil_1008） =================

    _ANNOUNCE_END_HOUR = 23
    _ANNOUNCE_END_MINUTE = 50

    @staticmethod
    def _announce_gate(now: datetime) -> bool:
        """窗口日公告轮询闸门：16:30~23:50 每 10 分钟一探；只等官方数，
        不把估算值冒充当天公告推送。"""
        if not (16 <= now.hour <= OilPricePlugin._ANNOUNCE_END_HOUR):
            return False
        if now.hour == 16 and now.minute < 30:
            return False
        if (
            now.hour == OilPricePlugin._ANNOUNCE_END_HOUR
            and now.minute > OilPricePlugin._ANNOUNCE_END_MINUTE
        ):
            return False
        return now.minute % 10 == 0

    def _claim_announce_slot(self, now: datetime) -> bool:
        """每个十分钟时槽只探测一次（主循环每 20 秒执行）。"""
        if not self._announce_gate(now):
            return False
        slot = now.strftime("%Y-%m-%d %H:%M")
        if slot == self._announce_last_slot:
            return False
        self._announce_last_slot = slot
        return True

    async def _official_announce_push(self, now: datetime):
        """窗口日官方公告推送（相位④）：只在东财调价记录确认公告已落地
        （官方=True，生效日=明天）时推送；不以估算值冒充当天公告。
        探测是全局的（每槽一次），落地面通知全部目标行，去重键=行标识::省集。"""
        if self.prefs.change_remind_days() <= 0:
            return
        today = self.store.today()
        fc = None
        try:
            fc = await asyncio.to_thread(forecast.get_today_forecast, today)
        except Exception as e:
            logger.warning(f"[油价插件] 官方公告查询失败: {e}")
        if not (fc and fc.get("official")):
            return  # 公告尚未落地：继续轮询，不发送估算替代公告
        st = self._load_push_state()
        done = st.setdefault("official_done", {})
        eff = str(fc.get("base_eff") or "").replace("-", "")
        if len(eff) != 8:
            logger.warning("[油价插件] 官方公告缺少有效生效日，跳过本次推送")
            return
        set_map = self._target_province_sets(self.prefs.push_rows())
        for provs, rows in set_map.items():
            set_key = self._set_key(list(provs))
            data = await self._collect_today(list(provs))
            if not data:
                continue
            # 官方调价行就是窗口日期的权威来源；即使 ShowAPI 已提前滚到
            # 下一轮窗口，也以公告生效日（明天）构造本次官方提醒倒计时。
            remind_rows = [(prov, {**d, "next_change_time": eff}) for prov, d in data]
            text = reporting.build_remind_text(
                remind_rows, self.prefs.default_fuels(), forecast=fc
            )
            if not text:
                continue
            for row in rows:
                done_key = self._row_key(row, set_key)
                if done.get(done_key) == today:
                    continue
                try:
                    image_ok = self._image_ready() and self._row_image_capable(row)
                    if not image_ok:
                        await self._push_text(text, [row])
                    else:
                        text_sent = False
                        try:
                            if len(data) == 1:
                                path = await self.get_or_gen_image(
                                    text,
                                    f"ai_official_{data[0][0]}",
                                    today,
                                    provs=[data[0][0]],
                                )
                                await self._push_text(text, [row])
                                text_sent = True
                            else:
                                path = await self.get_or_gen_image(
                                    text,
                                    "ai_official_multi",
                                    today,
                                    provs=[p for p, _ in data],
                                )
                            await self._push_image(path, [row])
                        except Exception as e:
                            logger.error(f"[油价插件] 官方公告出图失败，回退文字: {e}")
                            if not text_sent:
                                await self._push_text(text, [row])
                    done[done_key] = today
                    # 逐集逐行落盘：出错/进程重启不重复发送已完成的行。
                    self._save_push_state(st)
                    logger.info(
                        f"[油价插件] 已发送官方公告调价提醒（{row['identity'][:24]}）"
                    )
                except Exception as e:
                    logger.error(f"[油价插件] 官方公告推送异常（{done_key}）: {e}")
        self._save_push_state(st)

    async def _push_report(self, rows: list, targets: list = None):
        """按省份集生成并推送当日油价内容（常规推送与调价落地确认共用）。

        图片消息开关（gen_enable）：开且生图端点可用 → 单省=文字+图片两条，
        多省=仅图片；关或端点不可用 → 只发文字版。出图失败自动回退文字，不断播。
        图片能力按行判定（oil_1010）：umo 行 / 有凭证的 feishu_app 行可收图；
        仅文字行（webhook、无凭证 feishu_app）在多省仅图片时改收速览文字。
        """
        today = self.store.today()
        targets = targets if targets is not None else self.prefs.push_rows()
        img_rows = [r for r in targets if self._row_image_capable(r)]
        send_image = self._image_ready() and bool(img_rows)
        if not send_image:
            logger.info("[油价插件] 图片消息未开启或无可用图片目标，本次仅发文字")
        try:
            if len(rows) == 1:
                prov, data = rows[0]
                text = self._report_text(prov, data)
                if send_image:
                    path = await self.get_or_gen_image(
                        text, f"ai_{prov}", today, provs=[prov]
                    )
                    await self._push_text(text, targets)
                    await self._push_image(path, img_rows)
                else:
                    await self._push_text(text, targets)
            else:
                text = reporting.build_multi_text(rows, self.prefs.default_fuels())
                if send_image:
                    path = await self.get_or_gen_image(
                        text, "ai_multi", today, provs=[p for p, _ in rows]
                    )
                    await self._push_image(path, img_rows)
                    # 仅文字行收不到图片，补发速览文字（图片行保持仅图片）
                    text_only = [r for r in targets if not self._row_image_capable(r)]
                    await self._push_text(text, text_only)
                else:
                    await self._push_text(text, targets)
        except Exception as e:
            logger.error(f"[油价插件] 出图失败，改用文字推送: {e}")
            await self._push_text(
                self._fallback_text(rows) + "\n\n⚠️ 海报生成失败，已回退文字版",
                targets,
            )

    async def _daily_push(self):
        """立即按当前配置执行一次常规内容推送（/油价推送测试 用，不参与去重状态）。"""
        if not self.prefs.app_key_ready():
            logger.warning("[油价插件] 未配置 appKey，跳过推送")
            return
        rows = self.prefs.push_rows()
        if not rows:
            return
        for provs, set_rows in self._target_province_sets(rows).items():
            data = await self._collect_today(list(provs))
            if not data:
                logger.error("[油价插件] 推送取消：所有省份查询均失败")
                continue
            await self._push_report(data, set_rows)

    async def _monthly_push(self, slot_rows: list = None):
        """每月 1 日推送上一月油价走势图（按目标省份集分组生成，F9）。
        走势图为图片消息，仅图片能力行可收；无可用图片行则跳过并记日志。"""
        now = datetime.now()
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        rows = slot_rows if slot_rows is not None else self.prefs.push_rows()
        for provs, set_rows in self._target_province_sets(rows).items():
            img_rows = [r for r in set_rows if self._row_image_capable(r)]
            if not img_rows:
                logger.warning(
                    f"[油价插件] 省份集 {'、'.join(provs)} 无可收图的目标行，月度图跳过"
                )
                continue
            lines = charts.build_lines(
                self.store.history,
                list(provs),
                FUEL_ORDER,
                f"{year:04d}-{month:02d}-01",
                f"{year:04d}-{month:02d}-31",
            )
            if not lines:
                logger.warning(
                    f"[油价插件] {year}-{month:02d} {'、'.join(provs)} 无历史数据，跳过"
                )
                continue
            img = charts.line_chart_image(
                lines,
                f"{year}年{month}月 油价走势",
                "、".join(provs) + " · 每日单价（元/升）",
            )
            path = charts.save_image(img, TMP_DIR, f"monthly_{year}-{month:02d}.png")
            await self._push_image(path, img_rows)

    # ================= 查询（当日缓存优先） =================

    async def _get_today(self, prov: str) -> dict:
        cached = self.store.get_day(prov, self.store.today())
        if cached and cached.get("fuels"):
            return cached
        data = await asyncio.to_thread(
            datasource.fetch_province, prov, self.prefs.app_key()
        )
        self.store.store_prices(prov, data)
        return data

    async def _collect_today(self, provs: list) -> list:
        rows = []
        for prov in provs:
            try:
                rows.append((prov, await self._get_today(prov)))
            except Exception as e:
                logger.error(f"[油价插件] 查询 {prov} 失败: {e}")
        return rows

    def _report_text(self, prov: str, data: dict, alarm: bool = True) -> str:
        return reporting.build_text_report(
            prov,
            data,
            trend_series=self.store.trend_series,
            fuels=self.prefs.default_fuels(),
            alarm=alarm,
        )

    def _fallback_text(self, rows: list, alarm: bool = True) -> str:
        """只发文字时的文案：单省=完整多行报告，多省=紧凑速览。

        出图失败与图片关闭两条路径共用；此前单省出图失败误用多省速览，
        各油号挤成一行（oil_1001 修复）。推送回退保持警报样式（alarm=True），
        查询回退传 alarm=False 与查询中性播报一致（oil_1007）。
        """
        if len(rows) == 1:
            prov, data = rows[0]
            return self._report_text(prov, data, alarm=alarm)
        return reporting.build_multi_text(rows, self.prefs.default_fuels())

    def _gen_endpoints(self) -> list:
        """从 bot 已配置的模型提供者提取生图端点，正在使用的排最前。"""
        provs = []
        try:
            using = self.context.get_using_provider()
            if using is not None:
                provs.append(using)
        except Exception:
            pass
        try:
            for p in self.context.get_all_providers():
                if p is not None and p not in provs:
                    provs.append(p)
        except Exception:
            pass
        eps = []
        for p in provs:
            cfg = getattr(p, "provider_config", None) or {}
            for ep in genimage.endpoints_from_provider_config(cfg):
                if ep not in eps:
                    eps.append(ep)
        return eps

    def _parse_province_args(self, event: AstrMessageEvent, province: str) -> list:
        """解析 /今日油价 的省份参数：支持多省空格分隔（如 /今日油价 广东 江苏）。

        从原始消息里取命令字之后的 token 并按合法省份名单校验去重；
        无有效 token 时回退到单省参数，再回退到该会话的缺省省份
        （auto_provinces 开启时按近期查询偏好，否则用已配置省份）。
        """
        raw = str(getattr(event, "message_str", "") or "")
        after = raw.split("今日油价", 1)[1] if "今日油价" in raw else ""
        tokens = [t for t in after.split() if t in PROVINCES]
        if tokens:
            return self._norm_provs(list(dict.fromkeys(tokens)))
        prov_arg = (province or "").strip()
        if prov_arg in PROVINCES:
            return [prov_arg]
        return self._resolve_provinces_for(
            str(getattr(event, "unified_msg_origin", "") or "")
        )

    # ================= 生图（当日内容寻址缓存，oil_1002） =================

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _cache_lookup(self, text: str, base_name: str, date: str) -> str:
        """当日生图缓存查找：文本 sha1 即缓存键，命中返回图片路径否则空串。

        命中条件 = PNG 存在且 ≥CACHE_MIN_BYTES，且 meta 记录的文本哈希一致——
        配置/数据/文案变化都会体现在文本里，哈希不一致自动失效，不会发陈旧图。
        """
        h = self._text_hash(text)
        path = os.path.join(TMP_DIR, f"{base_name}_{date}_{h[:8]}.png")
        if not (os.path.isfile(path) and os.path.getsize(path) >= CACHE_MIN_BYTES):
            return ""
        try:
            with open(path + ".meta.json", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return ""
        return path if meta.get("text_sha1") == h else ""

    def _cache_store(self, path: str, text: str, style: str, provs: list):
        """生图成功后写 sidecar 元数据（与 PNG 同寿，清理时一并删除）。"""
        meta = {
            "text_sha1": self._text_hash(text),
            "base_name": os.path.splitext(os.path.basename(path))[0],
            "provs": list(provs),
            "style": style,
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            Path(path + ".meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[油价插件] 缓存元数据写入失败（不影响本次发图）: {e}")

    async def get_or_gen_image(
        self, text: str, base_name: str, date: str, provs: list = None
    ) -> str:
        """海报统一入口：当日缓存命中直接复用（秒回、零成本），未命中才真实生图。

        base_name 如 ai_江苏 / ai_multi / ai_remind_江苏 / ai_remind_multi，
        最终文件名 `{base}_{date}_{hash8}.png`——内容寻址使不同省集/文案天然
        各自成键，互不覆盖。
        """
        if not self.prefs.gen_enable():
            raise genimage.GenImageError("图片消息已关闭（gen_enable=false）")
        if self.prefs.gen_cache():
            cached = self._cache_lookup(text, base_name, date)
            if cached:
                logger.info(f"[油价插件] 生图缓存命中: {os.path.basename(cached)}")
                return cached
        style = self._daily_style(date)
        prompt = genimage.prompt_from_text(text, style=style)
        path = await asyncio.to_thread(
            genimage.generate,
            prompt,
            self._gen_endpoints(),
            self.prefs.gen_model(),
            out_dir=TMP_DIR,
            name=f"{base_name}_{date}_{self._text_hash(text)[:8]}.png",
        )
        if self.prefs.gen_cache():
            self._cache_store(path, text, style, provs or [])
        return path

    # ================= 推送通道 =================

    async def _deliver(self, components, umos: list):
        """UMO 目标批量发送（同槽同省集目标共享一条消息链）。"""
        mc = await asyncio.to_thread(self._make_chain, components)
        for umo in umos:
            try:
                await self.context.send_message(umo, mc)
                logger.info(f"[油价插件] 已推送至 {umo}")
            except Exception as e:
                logger.error(f"[油价插件] 推送到 {umo} 失败: {e}")

    @staticmethod
    def _make_chain(components):
        from astrbot.api.event import MessageChain

        mc = MessageChain()
        mc.chain = components
        return mc

    def _row_image_capable(self, row: dict) -> bool:
        """该行能否收图片消息：umo 行可；feishu_app 行需自建应用凭证；
        feishu_webhook 行官方限制不能收图。"""
        if row["kind"] == "umo":
            return True
        if row["kind"] == "feishu_app":
            return self.prefs.feishu_app_ready()
        return False

    def _feishu_api_host(self) -> str:
        """应用接口域名跟随机器人所在云：有国际版 webhook 则同域，否则国内版。"""
        return feishu.api_host(
            [r["raw"] for r in self.prefs.push_rows() if r["kind"] == "feishu_webhook"]
        )

    async def _push_text(self, text: str, rows: list = None):
        """按行发文字：umo 行走 UMO 主动消息（同链批量），feishu 行逐目标走
        webhook/应用；单行失败记日志不断播。"""
        rows = rows if rows is not None else self.prefs.push_rows()
        umos = [r["umo"] for r in rows if r["kind"] == "umo"]
        if umos:
            await self._deliver([Plain(text)], umos)
        app_id, app_secret = self.prefs.feishu_app_id(), self.prefs.feishu_app_secret()
        host = self._feishu_api_host()
        for row in rows:
            try:
                if row["kind"] == "feishu_webhook":
                    await asyncio.to_thread(feishu.send_webhook, row["feishu"], text)
                    logger.info(
                        f"[油价插件] 已推送至飞书 {feishu.mask_target(row['feishu'])}"
                    )
                elif row["kind"] == "feishu_app":
                    if not app_id or not app_secret:
                        raise feishu.FeishuError("未配置飞书自建应用凭证")
                    await asyncio.to_thread(
                        feishu.send_app_text, app_id, app_secret, row["feishu"], text, host
                    )
                    logger.info(
                        f"[油价插件] 已推送至飞书 {feishu.mask_target(row['feishu'])}"
                    )
            except Exception as e:
                logger.error(f"[油价插件] 飞书推送到 {row['identity'][:24]} 失败: {e}")

    async def _push_image(self, path: str, rows: list = None):
        """按行发图片：umo 行走 UMO；feishu_app 行走应用上传（feishu_webhook 行
        官方限制不能收图，调用方按能力矩阵分流不会传入）。

        上传是飞书侧的渠道级失败：整体抛出，由调用方走「出图失败回退文字」语义；
        已传后的单目标发送失败仅记日志（与 UMO 渠道的逐目标容错一致）。
        """
        rows = (
            rows
            if rows is not None
            else [r for r in self.prefs.push_rows() if self._row_image_capable(r)]
        )
        umos = [r["umo"] for r in rows if r["kind"] == "umo"]
        if umos:
            await self._deliver([BotImage.fromFileSystem(path)], umos)
        feishu_rows = [r for r in rows if r["kind"] == "feishu_app"]
        if not feishu_rows:
            return
        app_id, app_secret = self.prefs.feishu_app_id(), self.prefs.feishu_app_secret()
        if not app_id or not app_secret:
            logger.warning("[油价插件] feishu_app 行缺自建应用凭证，跳过图片消息")
            return
        host = self._feishu_api_host()
        try:
            image_key = await asyncio.to_thread(
                feishu.upload_image, app_id, app_secret, path, host
            )
        except Exception as e:
            raise feishu.FeishuError(f"海报上传飞书失败: {e}") from e
        for row in feishu_rows:
            try:
                await asyncio.to_thread(
                    feishu.send_app_image,
                    app_id,
                    app_secret,
                    row["feishu"],
                    path,
                    image_key,
                    host,
                )
                logger.info(f"[油价插件] 已推送至飞书 {feishu.mask_target(row['feishu'])}")
            except Exception as e:
                logger.error(f"[油价插件] 飞书推送到 {row['identity'][:24]} 失败: {e}")

    def _cleanup_tmp(self):
        """临时图片按 mtime TTL 清理（oil_1002）：AI 海报与各类当日产物 3 天、
        月度走势图 60 天；不解析文件名，元数据丢失的孤儿文件也能清走。"""
        now = time.time()
        removed = 0
        try:
            names = os.listdir(TMP_DIR)
        except OSError:
            return
        for name in names:
            path = os.path.join(TMP_DIR, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            keep = (
                TMP_KEEP_MONTHLY_DAYS * 86400
                if name.startswith("monthly_")
                else TMP_KEEP_DAYS * 86400
            )
            if age > keep:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info(f"[油价插件] 已清理过期临时图片 {removed} 个")

    # ================= WebUI：设置页 API =================

    async def _web_config_get(self):
        return json_response(self.prefs.web_payload())

    async def _web_config_save(self):
        payload = await request.json(default={})
        errors = self.prefs.apply_web_update(payload)
        if errors:
            return error_response("；".join(errors), status_code=400)
        try:
            self.prefs.save()
        except Exception as e:
            logger.error(f"[油价插件] 配置保存失败: {e}")
            return error_response("配置写入失败，详见日志", status_code=500)
        self._ensure_push_loop()
        return json_response({"ok": True, "app_key_ready": self.prefs.app_key_ready()})

    # ================= WebUI：查询偏好记忆（oil_1004） =================

    @staticmethod
    def _pref_target_label(umo: str) -> str:
        """UMO → 展示名（群聊/私聊 + ID）。"""
        if GROUP_TARGET_PREFIX and umo.startswith(GROUP_TARGET_PREFIX):
            return f"群聊 {umo.removeprefix(GROUP_TARGET_PREFIX)}"
        if PRIVATE_TARGET_PREFIX and umo.startswith(PRIVATE_TARGET_PREFIX):
            return f"私聊 {umo.removeprefix(PRIVATE_TARGET_PREFIX)}"
        return umo

    async def _web_prefs_list(self):
        """查询省份记忆列表：按目标分组，含每条事件与当前推导偏好。"""
        targets = []
        for umo, events in self.store.all_query_events().items():
            preferred = pick_preferred_provinces(events)
            targets.append(
                {
                    "umo": umo,
                    "label": self._pref_target_label(umo),
                    "events": events,
                    "preferred": preferred or [],
                    "preferred_from_prefs": preferred is None,
                }
            )
        # 推送目标在前、其余按标签排序，展示稳定
        order = {u: i for i, u in enumerate(self.prefs.push_row_identities())}
        targets.sort(key=lambda t: (order.get(t["umo"], 99), t["label"]))
        return json_response(
            {"auto_provinces": self.prefs.auto_provinces(), "targets": targets}
        )

    async def _web_prefs_manage(self):
        """查询省份记忆管理：update=改事件省份集 / delete=删事件 / clear=清空目标。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("无效的请求数据", status_code=400)
        action = str(payload.get("action") or "")
        umo = str(payload.get("umo") or "")
        if not umo:
            return error_response("缺少目标", status_code=400)
        if action == "clear":
            ok = self.store.clear_query_events(umo)
            return json_response({"ok": ok})
        want = payload.get("event") or {}
        if not isinstance(want, dict) or not want:
            return error_response("缺少事件", status_code=400)
        if action == "delete":
            ok = self.store.delete_query_event(umo, want)
        elif action == "update":
            provs = [
                str(p).strip()
                for p in (payload.get("provinces") or [])
                if str(p).strip() in PROVINCES
            ]
            provs = list(dict.fromkeys(provs))
            if not provs:
                return error_response("省份集不能为空且需为合法省份", status_code=400)
            ok = self.store.update_query_event(umo, want, provs)
        else:
            return error_response("未知操作", status_code=400)
        if not ok:
            return error_response("未找到匹配的记录（可能刚被删除）", status_code=404)
        return json_response({"ok": True})

    async def _web_sessions_list(self):
        """见过的会话列表（设置页「从记录会话选择」数据源，含机器人层）。"""
        return json_response({"sessions": self.store.all_seen_sessions()})

    # ================= WebUI：统计页 API =================

    async def _web_stats_render(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("无效的请求数据", status_code=400)
        provs = [
            p for p in (payload.get("provinces") or []) if str(p).strip()
        ] or self.prefs.provinces()
        fuels = [f for f in (payload.get("fuels") or []) if f in FUEL_ORDER] or [
            "92",
            "95",
            "98",
        ]
        try:
            days = min(180, max(7, int(payload.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        if not self.prefs.app_key_ready():
            return error_response(NO_KEY_HINT.replace("\n", ""), status_code=400)

        end = datetime.now().date()
        start_d = end.fromordinal(end.toordinal() - days + 1)
        lines = charts.build_lines(
            self.store.history, provs, fuels, start_d.isoformat(), end.isoformat()
        )
        if not lines:
            return json_response(
                {
                    "image": "",
                    "summary": [],
                    "message": "所选范围内暂无历史数据（数据从插件开始查询后逐日积累）",
                }
            )
        img = charts.line_chart_image(
            lines,
            "油价走势",
            f"近 {days} 天 · 每日单价（元/升）",
            theme=str(payload.get("theme") or "dark"),
            ui=True,  # WebUI 页面模式：字号/配色与页面正文对齐（oil_1001 F10）
        )
        b64 = base64.b64encode(charts.png_bytes(img)).decode()
        return json_response(
            {
                "image": "data:image/png;base64," + b64,
                "summary": charts.summarize_lines(lines),
            }
        )

    # ================= 指令 =================

    def _quota_guard(self, event: AstrMessageEvent):
        # 查询配额与推送目标绑定——优先用该目标(群/私聊)的单独限额，否则用全局默认
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        allowed, used, limit = self.store.quota_consume(
            f"{umo}|{event.get_sender_id() or ''}", self.prefs.quota_limit_for(umo)
        )
        if not allowed:
            return (
                False,
                f"今日查询次数已达上限（{used}/{limit}）啦，明天再来问猫饼吧 (=^･ω･^=)",
            )
        remain = max(0, limit - used) if limit > 0 else -1
        return True, (
            f"\n\n（今日剩余查询次数：{remain}）" if limit > 0 and remain >= 0 else ""
        )

    def _key_hint(self):
        """appKey 未就绪时返回提示文案；就绪时返回空串。"""
        return "" if self.prefs.app_key_ready() else NO_KEY_HINT

    @filter.command("油价", alias={"油价查询", "查油价"})
    async def oil_price_cmd(self, event: AstrMessageEvent, province: str = ""):
        """文字版快速查询。/油价 [省份]；不带省份时播报全部已选省份"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        ok, extra = self._quota_guard(event)
        if not ok:
            yield event.plain_result(extra)
            return
        prov_arg = (province or "").strip()
        if prov_arg:
            provs = [prov_arg]
        else:
            provs = self._resolve_provinces_for(
                str(getattr(event, "unified_msg_origin", "") or "")
            )
        rows = await self._collect_today(provs)
        if not rows:
            yield event.plain_result("油价查询失败喵，请稍后再试")
            return
        self._record_query(event, provs)
        if prov_arg:
            text = self._report_text(prov_arg, rows[0][1], alarm=False)
        else:
            text = reporting.build_multi_text(rows, self.prefs.default_fuels())
        yield event.plain_result(text + extra)

    @filter.command("今日油价")
    async def today_cmd(self, event: AstrMessageEvent, province: str = ""):
        """出图版播报。/今日油价 [省份...]；多省空格分隔出合一图，缺省用已配置省份"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        ok, extra = self._quota_guard(event)
        if not ok:
            yield event.plain_result(extra)
            return
        provs = self._parse_province_args(event, province)
        rows = await self._collect_today(provs)
        if not rows:
            yield event.plain_result("油价查询失败喵，请稍后再试")
            return
        self._record_query(event, provs)
        today = self.store.today()
        if not self._image_ready():
            hint = (
                "（图片消息未开启或未配置生图端点，已发文字版。"
                "请到 WebUI「油价设置」开启图片消息并确认 bot 已配置模型）"
            )
            yield event.plain_result(self._fallback_text(rows, alarm=False) + hint)
            return
        if len(provs) == 1 and rows:
            prov, data = rows[0]
            # 查询走中性播报（oil_1007）：不带警报标题，文案哈希与推送不同，
            # 不会命中落地确认推送生成的同一张海报
            poster_text = self._report_text(prov, data, alarm=False)
            base_name, poster_provs = f"ai_{prov}", [prov]
        else:
            poster_text = reporting.build_multi_text(rows, self.prefs.default_fuels())
            base_name, poster_provs = "ai_multi", [p for p, _ in rows]
        # 当日缓存命中（oil_1002）：跳过等待提示语直接发图
        if self.prefs.gen_cache():
            cached = self._cache_lookup(poster_text, base_name, today)
            if cached:
                logger.info(f"[油价插件] 生图缓存命中: {os.path.basename(cached)}")
                yield event.chain_result([BotImage.fromFileSystem(cached)])
                return
        yield event.plain_result(
            f"⏳ 收到喵，正在查询{'、'.join(provs)}油价并生成海报（约需几十秒），请稍等～"
        )
        try:
            path = await self.get_or_gen_image(
                poster_text, base_name, today, provs=poster_provs
            )
            yield event.chain_result([BotImage.fromFileSystem(path)])
        except Exception as e:
            logger.error(f"[油价插件] 出图失败: {e}")
            yield event.plain_result(
                self._fallback_text(rows, alarm=False) + "\n\n⚠️ 海报生成失败，已回退文字版"
            )

    @filter.command("看看油价")
    async def look_cmd(self, event: AstrMessageEvent, province: str = "", fuel: str = ""):
        """单油种大字卡片。/看看油价 [省份] [油号]"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        ok, extra = self._quota_guard(event)
        if not ok:
            yield event.plain_result(extra)
            return
        prov = (province or "").strip() or self.prefs.main_province()
        fuel = (fuel or "").strip() or self.prefs.main_fuel()
        try:
            data = await self._get_today(prov)
        except Exception as e:
            yield event.plain_result(f"油价查询失败喵：{e}")
            return
        entry = (data.get("fuels") or {}).get(fuel)
        if not entry:
            yield event.plain_result(f"没有查到 {prov} {fuel} 号油的价格喵")
            return
        self._record_query(event, [prov])
        price, ch, pct = entry["price"], entry.get("change"), entry.get("pct")
        today = self.store.today()
        try:
            path = await asyncio.to_thread(
                charts.render_price_card,
                prov,
                FUEL_NAMES.get(fuel, fuel + "号"),
                price,
                ch,
                pct,
                today,
                TMP_DIR,
            )
            yield event.chain_result([BotImage.fromFileSystem(path)])
        except Exception as e:
            logger.error(f"[油价插件] 出图失败: {e}")
            tail = (
                f"较上次调价 {'↑' if (ch or 0) > 0 else '↓'} {abs(ch):.2f} 元"
                if ch is not None
                else ""
            )
            yield event.plain_result(f"{prov} {fuel}号油：{price:g} 元/升 {tail}")

    @filter.command("我要加油")
    async def refuel_cmd(
        self,
        event: AstrMessageEvent,
        liters: str = "50",
        province: str = "",
        fuel: str = "",
    ):
        """加油结算单。/我要加油 30 [省份] [油号]"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        ok, extra = self._quota_guard(event)
        if not ok:
            yield event.plain_result(extra)
            return
        try:
            liters_f = abs(float(liters))
        except (TypeError, ValueError):
            yield event.plain_result("升数要写数字喵，比如：/我要加油 30")
            return
        prov = (province or "").strip() or self.prefs.main_province()
        fuel = (fuel or "").strip() or self.prefs.main_fuel()
        fuel_name = FUEL_NAMES.get(fuel, fuel + "号")
        try:
            data = await self._get_today(prov)
        except Exception as e:
            yield event.plain_result(f"油价查询失败喵：{e}")
            return
        entry = (data.get("fuels") or {}).get(fuel)
        if not entry or not entry.get("price"):
            yield event.plain_result(f"没有查到 {prov} {fuel_name} 的价格喵")
            return
        self._record_query(event, [prov])
        price, ch = entry["price"], entry.get("change")
        total = round(price * liters_f, 2)
        change_total = round(ch * liters_f, 2) if ch is not None else None
        today = self.store.today()
        try:
            path = await asyncio.to_thread(
                charts.render_receipt,
                liters_f,
                price,
                total,
                prov,
                fuel_name,
                change_total,
                today,
                TMP_DIR,
            )
            yield event.chain_result([BotImage.fromFileSystem(path)])
        except Exception as e:
            logger.error(f"[油价插件] 出图失败: {e}")
            yield event.plain_result(
                f"{fuel_name} {liters_f:g} 升 = {total:.2f} 元（单价 {price:g} 元/升）"
            )

    @filter.command("油价设置")
    async def settings_cmd(
        self, event: AstrMessageEvent, province: str = "", fuel: str = ""
    ):
        """记住常用省份与油号。/油价设置 广东 95（多省请在设置页勾选）"""
        if not province and not fuel:
            hour, minute = self.prefs.push_time()
            yield event.plain_result(
                "用法：/油价设置 广东 95\n当前："
                f"{'、'.join(self.prefs.provinces())} · {'/'.join(self.prefs.default_fuels())}号 · "
                f"推送时间 {hour:02d}:{minute:02d}"
            )
            return
        payload = self.prefs.web_payload()
        if province:
            payload["provinces"] = [province.strip()]
        if fuel:
            payload["default_fuels"] = [fuel.strip()]
        errors = self.prefs.apply_web_update(payload)
        if errors:
            yield event.plain_result("保存失败：" + "；".join(errors))
            return
        try:
            self.prefs.save()
        except Exception as e:
            logger.error(f"[油价插件] 保存设置失败: {e}")
            yield event.plain_result(f"保存失败：{e}")
            return
        yield event.plain_result(
            f"✅ 已记住：省份 {'、'.join(self.prefs.provinces())}，"
            f"默认油号 {'/'.join(self.prefs.default_fuels())}号（多省推送请到设置页勾选）"
        )

    @filter.command("调价窗口")
    async def adjust_window_cmd(self, event: AstrMessageEvent):
        """查看下次调价窗口倒计时"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        try:
            data = await self._get_today(self.prefs.main_province())
        except Exception as e:
            yield event.plain_result(f"查询失败喵：{e}")
            return
        nxt = data.get("next_change_time") or ""
        fc = None
        try:
            fc = await asyncio.to_thread(forecast.get_today_forecast, self.store.today())
        except Exception as e:
            logger.warning(f"[油价插件] 调价预测获取失败: {e}")
        direction = (fc or {}).get("dir")
        official = bool((fc or {}).get("official"))
        line = reporting.adjust_countdown_text(nxt, direction, official)
        if fc and direction in ("rise", "drop"):
            word = "上调" if direction == "rise" else "下调"
            d92 = abs((fc.get("delta") or {}).get("92") or 0)
            per_ton = abs(int(fc.get("per_ton") or 0))
            if fc.get("official"):
                extra = (
                    f"\n⚡ 官方公告：汽油每吨{word} {per_ton} 元（92号约 {d92:.2f} 元/升）"
                )
                cy = fc.get("per_ton_diesel")
                d0 = (fc.get("delta") or {}).get("0")
                if cy is not None:
                    extra += f"，柴油每吨{word} {abs(int(cy))} 元"
                    if d0 is not None:
                        extra += f"（0号约 {abs(d0):.2f} 元/升）"
                extra += "，今晚 24 时起执行"
            else:
                extra = (
                    f"\n⚡ 预计每吨{word}约 {per_ton} 元"
                    f"（92号约 {d92:.2f} 元/升），以发改委公告为准"
                )
        elif fc and direction == "flat":
            extra = "\n⚡ 预计本轮调价幅度不足 50 元/吨，大概率搁浅（以发改委公告为准）"
        else:
            extra = ""
        if line:
            yield event.plain_result(f"📅 {line}{extra}")
        elif nxt:
            yield event.plain_result(f"下次调价窗口：{nxt}{extra}")
        else:
            yield event.plain_result("数据源暂无下次调价日期喵")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("油价订阅")
    async def push_subscribe_cmd(self, event: AstrMessageEvent):
        """把当前会话加入推送目标（管理员）：群聊/私聊自动归类，调度跟随默认"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            yield event.plain_result("无法获取当前会话标识，订阅失败")
            return
        if any(r["identity"] == umo for r in self.prefs.push_rows()):
            yield event.plain_result("✅ 本会话已在推送目标中，无需重复订阅")
            return
        group = ":GroupMessage:" in umo
        key = "push_group_targets" if group else "push_private_targets"
        payload = self.prefs.web_payload()
        payload.setdefault(key, []).append(
            {"id": umo, "frequency": "", "time": "", "limit": None}
        )
        errors = self.prefs.apply_web_update(payload)
        if errors:
            yield event.plain_result("订阅失败：" + "；".join(errors))
            return
        try:
            self.prefs.save()
        except Exception as e:
            yield event.plain_result(f"保存失败：{e}")
            return
        where = "群聊" if group else "私聊"
        yield event.plain_result(
            f"✅ 已订阅：本{where}将跟随默认调度接收油价推送"
            "（可在设置页目标行单独调整频率/时刻）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("油价退订")
    async def push_unsubscribe_cmd(self, event: AstrMessageEvent):
        """把当前会话移出推送目标（管理员）"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        removed = False
        for key, group in (
            ("push_group_targets", True),
            ("push_private_targets", False),
        ):
            rows = self.prefs.config["basic"].get(key) or []
            kept = []
            for r in rows if isinstance(rows, list) else []:
                t, _err = pushtarget.resolve_target(
                    str((r or {}).get("id") or ""), group=group
                )
                if t and t["identity"] == umo:
                    removed = True
                    continue
                kept.append(r)
            if removed:
                self.prefs.config["basic"][key] = kept
        if not removed:
            yield event.plain_result("本会话不在推送目标中，无需退订")
            return
        try:
            self.prefs.save()
        except Exception as e:
            yield event.plain_result(f"保存失败：{e}")
            return
        yield event.plain_result("✅ 已退订：本会话不再接收油价推送")

    @filter.command("油价调整")
    async def price_adjust_push_cmd(self, event: AstrMessageEvent):
        """立即向全部推送目标发送一条调价提醒（官方口径优先，其次推算口径）"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        rows = self.prefs.push_rows()
        if not rows:
            yield event.plain_result(
                "未配置推送目标（请在设置页添加目标行，或在本会话发 /油价订阅）"
            )
            return
        sent_any = False
        set_map = self._target_province_sets(rows)
        for provs, set_rows in set_map.items():
            data = await self._collect_today(list(provs))
            if not data:
                continue
            if await self._advance_remind(data, set_rows, force=True):
                sent_any = True
        if sent_any:
            yield event.plain_result(
                f"✅ 调价提醒已推送至 {len(rows)} 个目标"
                "（内容含下次调价窗口倒计时与预测/公告口径）"
            )
        else:
            yield event.plain_result("未能推送：数据源暂无下次调价日期，或全部省份查询失败")

    @filter.command("油价推送测试")
    async def push_test_cmd(self, event: AstrMessageEvent):
        """立即向配置的推送目标执行一次每日推送（QQ/飞书目标混合）"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        await self._daily_push()
        rows = self.prefs.push_rows()
        yield event.plain_result(
            "✅ 测试推送已执行："
            + (
                "、".join(r["identity"] for r in rows)
                if rows
                else "未配置推送目标（请在设置页添加目标行）"
            )
        )

    @filter.command("飞书群列表")
    async def feishu_chats_cmd(self, event: AstrMessageEvent):
        """列出飞书应用机器人已加入的群（chat_id），供配置群推送目标"""
        if not self.prefs.feishu_app_ready():
            yield event.plain_result(
                "⚠️ 请先在 WebUI「油价设置 → 接口配置」填写飞书自建应用 "
                "app_id 与 app_secret，并把应用机器人拉进群"
            )
            return
        try:
            chats = await asyncio.to_thread(
                feishu.list_chats,
                self.prefs.feishu_app_id(),
                self.prefs.feishu_app_secret(),
                self._feishu_api_host(),
            )
        except Exception as e:
            yield event.plain_result(f"查询失败：{e}")
            return
        if not chats:
            yield event.plain_result(
                "机器人尚未加入任何飞书群（请先在群设置→群机器人→添加应用机器人）"
            )
            return
        lines = [f"· {name}：{cid}" for cid, name in chats]
        yield event.plain_result(
            "📋 机器人已加入的群（chat_id 可填入群推送目标）：\n" + "\n".join(lines)
        )
