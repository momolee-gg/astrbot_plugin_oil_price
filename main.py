"""astrbot_plugin_oil_price —— 全国油价查询与出图播报（ShowAPI 数据源）。

架构（模块间不耦合，均只依赖 modules/constants；charts 复用 reporting 的文案函数）：
  modules/constants.py   全局常量（零依赖）
  modules/datasource.py  ShowAPI 数据源客户端
  modules/storage.py     本地存储（查询历史 / 用户配额 / 月度序列）
  modules/reporting.py   文字文案（单省报告 / 多省速览 / 倒计时 / 上涨提醒）
  modules/charts.py      PIL 渲染（单油卡 / 结算单 / 折线图）
  modules/genimage.py    AI 生图（当天文字消息 → 提示词 → bot 已配置模型的生图端点）
  config.py              配置读写与校验（Prefs，唯一配置访问层）
  main.py                插件入口：指令编排、定时推送（每日/每月）、统计与设置页 API
"""

import asyncio
import base64
import json
import os
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as BotImage
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .config import Prefs
from .modules import charts, datasource, genimage, reporting
from .modules.constants import FUEL_NAMES, FUEL_ORDER, PROVINCES
from .modules.storage import Store

PLUGIN_NAME = "astrbot_plugin_oil_price"
PLUGIN_VERSION = "1.0.0"
DATA_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
QUOTA_FILE = os.path.join(DATA_DIR, "user_quota.json")
TMP_DIR = os.path.join(DATA_DIR, "tmp")

NO_KEY_HINT = (
    "⚠️ 尚未配置 ShowAPI appKey，插件功能未启用。\n"
    "请在管理面板侧边栏「油价设置」页填写后保存。"
)


@register(
    PLUGIN_NAME,
    "momolee-gg",
    "全国油价查询与出图播报（ShowAPI 数据源）：多省推送、"
    "AI 生图每日海报（5 风格随机轮换）、推送频率（每日/每周/有变动时）、"
    "调价提前提醒、油价走势折线图、月度走势推送、加油结算单、"
    "目标绑定查询限额、WebUI 设置与统计页",
    PLUGIN_VERSION,
)
class OilPricePlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.prefs = Prefs(config)
        self.store = Store(HISTORY_FILE, QUOTA_FILE)
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        self._tasks = []
        self._pushed_date = None
        self._pushed_month = None
        self._loop_started = False
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

        self._ensure_push_loop()

    async def terminate(self):
        """插件卸载/重载时取消后台任务（AstrBot 生命周期钩子）。"""
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("[油价插件] 已清理后台推送任务")

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
                hour, minute = self.prefs.push_time()
                today = now.strftime("%Y-%m-%d")
                month = now.strftime("%Y-%m")
                if now.hour == hour and now.minute == minute:
                    if self._pushed_date != today:
                        self._pushed_date = today
                        freq = self.prefs.push_frequency()
                        if freq == "weekly":
                            if now.weekday() == self.prefs.push_weekday():
                                await self._daily_push()
                            else:
                                logger.info("[油价插件] 今日非每周推送日，跳过")
                        elif freq == "on_change":
                            await self._on_change_push(today)
                        else:
                            await self._daily_push()
                        if freq in ("weekly", "on_change"):
                            await self._advance_remind(today)
                    if (
                        self.prefs.monthly_push_enabled()
                        and now.day == 1
                        and self._pushed_month != month
                    ):
                        self._pushed_month = month
                        await self._monthly_push()
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[油价插件] 推送循环异常: {e}")
                await asyncio.sleep(60)

    # ================= 推送状态（风格轮换 / 变动快照 / 提醒去重） =================

    def _load_push_state(self) -> dict:
        try:
            with open(self._state_file, encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            return {}

    def _save_push_state(self, st: dict):
        tmp = self._state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, self._state_file)

    def _pick_style(self) -> str:
        """随机挑一个与上次不同的风格，并记录到推送状态。"""
        st = self._load_push_state()
        idx = genimage.pick_style_index(st.get("last_style"))
        st["last_style"] = idx
        self._save_push_state(st)
        name = genimage.STYLES[idx]["name"]
        logger.info(f"[油价插件] 本次生图风格：{name}")
        return genimage.STYLES[idx]["text"]

    def _image_ready(self) -> bool:
        """图片消息开关：gen_enable 开且 bot 配置的 provider 里有生图端点。"""
        return bool(self.prefs.gen_enable() and self._gen_endpoints())

    # ================= 变动推送与提前提醒 =================

    def _price_snapshot(self, rows) -> dict:
        return {
            prov: {
                k: e["price"]
                for k, e in (data.get("fuels") or {}).items()
                if e.get("price") is not None
            }
            for prov, data in rows
        }

    async def _on_change_push(self, today: str):
        """变动推送：到达推送时间时抓取价格，与上次快照对比，有变动才推。"""
        rows = await self._collect_today(self.prefs.provinces())
        if not rows:
            logger.error("[油价插件] 变动推送取消：所有省份查询均失败")
            return
        st = self._load_push_state()
        snap = st.get("last_prices") or {}
        cur = self._price_snapshot(rows)
        if not snap:
            st["last_prices"] = cur
            self._save_push_state(st)
            logger.info("[油价插件] 变动推送：首次运行，仅记录基线价格，不推送")
            return
        if snap == cur:
            logger.info("[油价插件] 变动推送：价格无变动，不推送")
            return
        await self._daily_push()
        st = self._load_push_state()
        st["last_prices"] = cur
        self._save_push_state(st)

    async def _advance_remind(self, today: str):
        """调价提前提醒：距下次调价窗口还剩 N 天时发一条文字提醒（每日至多一条）。"""
        days_n = self.prefs.change_remind_days()
        if days_n <= 0:
            return
        st = self._load_push_state()
        if st.get("last_remind") == today:
            return
        rows = await self._collect_today(self.prefs.provinces())
        lines = []
        for prov, data in rows:
            line = reporting.adjust_countdown_text(data.get("next_change_time") or "")
            if line:
                lines.append(f"⛽ {prov}：{line}")
        if not lines:
            return
        text = "🚨🚨 调价提醒 🚨🚨\n" + "\n".join(lines)
        await self._push_text(text)
        st = self._load_push_state()
        st["last_remind"] = today
        self._save_push_state(st)
        logger.info("[油价插件] 已发送调价提前提醒")

    async def _daily_push(self):
        """每日推送。

        图片消息开关（gen_enable）：开且生图端点可用 → 单省=文字+图片两条，
        多省=仅图片；关或端点不可用 → 只发文字版（单省=单省报告，多省=速览）。
        出图失败自动回退文字，不会断播。
        """
        if not self.prefs.app_key_ready():
            logger.warning("[油价插件] 未配置 appKey，跳过推送")
            return
        rows = await self._collect_today(self.prefs.provinces())
        if not rows:
            logger.error("[油价插件] 推送取消：所有省份查询均失败")
            return
        today = self.store.today()
        send_image = self._image_ready()
        if not send_image:
            logger.info("[油价插件] 图片消息未开启或未配置生图端点，本次仅发文字")
        try:
            if len(rows) == 1:
                prov, data = rows[0]
                text = self._report_text(prov, data)
                if send_image:
                    path = await self._gen_image_from_text(
                        text, f"ai_{prov}_{today}.png", style=self._pick_style()
                    )
                    await self._push_text(text)
                    await self._push_image(path)
                else:
                    await self._push_text(text)
            else:
                text = reporting.build_multi_text(rows, self.prefs.default_fuels())
                if send_image:
                    path = await self._gen_image_from_text(
                        text, f"ai_multi_{today}.png", style=self._pick_style()
                    )
                    await self._push_image(path)
                else:
                    await self._push_text(text)
        except Exception as e:
            logger.error(f"[油价插件] 出图失败，改用文字推送: {e}")
            await self._push_text(
                reporting.build_multi_text(rows, self.prefs.default_fuels())
            )

    async def _monthly_push(self):
        """每月 1 日推送上一月油价走势图（取推送配置的目标）。"""
        now = datetime.now()
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        lines = charts.build_lines(
            self.store.history,
            self.prefs.provinces(),
            FUEL_ORDER,
            f"{year:04d}-{month:02d}-01",
            f"{year:04d}-{month:02d}-31",
        )
        if not lines:
            logger.warning(f"[油价插件] {year}-{month:02d} 无历史数据，月度推送跳过")
            return
        img = charts.line_chart_image(
            lines,
            f"{year}年{month}月 油价走势",
            "、".join(self.prefs.provinces()) + " · 每日单价（元/升）",
        )
        path = charts.save_image(img, TMP_DIR, f"monthly_{year}-{month:02d}.png")
        await self._push_image(path)

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

    def _report_text(self, prov: str, data: dict) -> str:
        return reporting.build_text_report(
            prov,
            data,
            trend_series=self.store.trend_series,
            fuels=self.prefs.default_fuels(),
        )

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
        无有效 token 时回退到单省参数，再回退到已配置省份。
        """
        raw = str(getattr(event, "message_str", "") or "")
        after = raw.split("今日油价", 1)[1] if "今日油价" in raw else ""
        tokens = [t for t in after.split() if t in PROVINCES]
        if tokens:
            return list(dict.fromkeys(tokens))
        prov_arg = (province or "").strip()
        if prov_arg in PROVINCES:
            return [prov_arg]
        return self.prefs.provinces()

    async def _gen_image_from_text(self, text: str, name: str, style: str = "") -> str:
        """把当天文字消息发给 bot 已配置模型平台的生图模型，返回本地 PNG 路径。"""
        if not self.prefs.gen_enable():
            raise genimage.GenImageError("图片消息已关闭（gen_enable=false）")
        prompt = genimage.prompt_from_text(text, style=style)
        return await asyncio.to_thread(
            genimage.generate,
            prompt,
            self._gen_endpoints(),
            self.prefs.gen_model(),
            out_dir=TMP_DIR,
            name=name,
        )

    # ================= 推送通道 =================

    async def _deliver(self, components):
        mc = await asyncio.to_thread(self._make_chain, components)
        for umo in self.prefs.notify_targets():
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

    async def _push_image(self, path: str):
        await self._deliver([BotImage.fromFileSystem(path)])

    async def _push_text(self, text: str):
        await self._deliver([Plain(text)])

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
        provs = [prov_arg] if prov_arg else self.prefs.provinces()
        rows = await self._collect_today(provs)
        if not rows:
            yield event.plain_result("油价查询失败喵，请稍后再试")
            return
        if prov_arg:
            text = self._report_text(prov_arg, rows[0][1])
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
        today = self.store.today()
        if not self._image_ready():
            hint = (
                "（图片消息未开启或未配置生图端点，已发文字版。"
                "请到 WebUI「油价设置」开启图片消息并确认 bot 已配置模型）"
            )
            if len(provs) == 1 and rows:
                prov, data = rows[0]
                yield event.plain_result(self._report_text(prov, data) + hint)
            else:
                yield event.plain_result(
                    reporting.build_multi_text(rows, self.prefs.default_fuels()) + hint
                )
            return
        try:
            if len(provs) == 1 and rows:
                prov, data = rows[0]
                path = await self._gen_image_from_text(
                    self._report_text(prov, data),
                    f"ai_{prov}_{today}.png",
                    style=self._pick_style(),
                )
            else:
                path = await self._gen_image_from_text(
                    reporting.build_multi_text(rows, self.prefs.default_fuels()),
                    f"ai_multi_{today}.png",
                    style=self._pick_style(),
                )
            yield event.chain_result([BotImage.fromFileSystem(path)])
        except Exception as e:
            logger.error(f"[油价插件] 出图失败: {e}")
            yield event.plain_result(
                reporting.build_multi_text(rows, self.prefs.default_fuels())
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
        line = reporting.adjust_countdown_text(nxt)
        yield event.plain_result(
            f"📅 {line}"
            if line
            else (f"下次调价窗口：{nxt}" if nxt else "数据源暂无下次调价日期喵")
        )

    @filter.command("油价推送测试")
    async def push_test_cmd(self, event: AstrMessageEvent):
        """立即向配置的推送目标执行一次每日推送"""
        key_hint = self._key_hint()
        if key_hint:
            yield event.plain_result(key_hint)
            return
        await self._daily_push()
        targets = self.prefs.notify_targets()
        yield event.plain_result(
            "✅ 测试推送已执行：" + ("、".join(targets) if targets else "未配置推送目标")
        )
