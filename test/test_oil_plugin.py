"""油价插件自动化测试（astrbot_plugin_oil_price v1.2.0）。

运行方式（容器内）：
    cd /AstrBot/data/plugins && python3 -m unittest astrbot_plugin_oil_price.tests.test_oil_plugin -v

本地（无容器，Windows 开发机）：python test/run_local.py
（用 test/_local 的最小 astrbot 桩满足 import 面，逻辑为真实实现）

隔离策略：
    - 全部读写走测试临时目录，不触碰真实配置/历史/配额；
    - ShowAPI 请求默认全部 mock（OIL_LIVE=1 时额外执行真实冒烟，消耗 1 次配额）；
    - 推送目标使用 MockContext，不发送真实 QQ 消息。
"""

import asyncio
import base64
import io
import json
import os
import shutil
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

PLUGINS_DIR = "/AstrBot/data/plugins"
if PLUGINS_DIR not in sys.path:
    sys.path.insert(0, PLUGINS_DIR)

TEST_ROOT = "/tmp/oil_plugin_test"

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot_plugin_oil_price import main as plugin_main
from astrbot_plugin_oil_price.config import Prefs
from astrbot_plugin_oil_price.modules import charts, datasource, reporting
from astrbot_plugin_oil_price.modules.constants import PROVINCES
from astrbot_plugin_oil_price.modules.storage import Store

# ======================= 测试基础设施 =======================


class MockProvider:
    """模拟 bot 已配置的模型提供者（生图端点来源）。"""

    def __init__(self, api_base="https://example.invalid/api/v4"):
        self.provider_config = {"key": ["test-img-key"], "api_base": api_base}


class MockContext:
    def __init__(self):
        self.apis = []
        self.sent_messages = []

    def register_web_api(self, route, handler, methods, desc):
        self.apis.append((route, tuple(methods), desc))

    async def send_message(self, umo, mc):
        self.sent_messages.append((umo, mc))

    def get_using_provider(self):
        return MockProvider()

    def get_all_providers(self):
        return [MockProvider()]


class MockEvent:
    def __init__(self, sender="tester", umo="default:GroupMessage:10001", message_str=""):
        self.sender = sender
        self.unified_msg_origin = umo
        self.message_str = message_str
        self.sent = []

    def get_sender_id(self):
        return self.sender

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def chain_result(self, parts):
        return {"type": "chain", "parts": parts}

    async def send(self, msg):
        self.sent.append(msg)


class FakeConfig(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saved = 0

    def save_config(self, indent=None):
        self.saved += 1


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self, default=None):
        return self._payload


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def base_config(appkey="test-key-123"):
    return {
        "basic": {
            "provinces": ["江苏", "上海"],
            "push_hour": 9,
            "push_minute": 0,
            "monthly_push": True,
            "notify_private": [],
            "notify_group": ["default:GroupMessage:10001"],
            "target_limits": {},
            "user_daily_limit": 5,
            "default_fuels": ["92", "95"],
        },
        "api": {"app_key": appkey},
    }


def fake_price_data(p92=8.09, ch92=0.10):
    return {
        "fuels": {
            "92": {"price": p92, "before": p92 - ch92, "change": ch92, "pct": 1.2},
            "95": {"price": 8.62, "before": 8.67, "change": -0.05, "pct": -0.6},
            "98": {"price": 9.98, "before": 9.98, "change": 0.0, "pct": 0.0},
            "0": {"price": 7.71, "before": 7.71, "change": None, "pct": None},
        },
        "before_change_time": "20261023",
        "next_change_time": "20261030",
    }


def _fake_genimage_generate(
    prompt, endpoints, model=None, size=None, out_dir="/tmp", name="gen.png"
):
    """生图假实现：不联网，直接落一张最小 PNG。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 2000)
    return path


def make_plugin(name="t", config=None, appkey="test-key-123"):
    sub = os.path.join(TEST_ROOT, name.replace(".", "_"))
    shutil.rmtree(sub, ignore_errors=True)
    plugin_main.DATA_DIR = os.path.join(sub, "data")
    plugin_main.HISTORY_FILE = os.path.join(sub, "history.json")
    plugin_main.QUOTA_FILE = os.path.join(sub, "quota.json")
    plugin_main.EVENTS_FILE = os.path.join(sub, "events.json")
    plugin_main.TMP_DIR = os.path.join(sub, "tmp")
    cfg = config if config is not None else base_config(appkey=appkey)
    ctx = MockContext()
    pl = plugin_main.OilPricePlugin(ctx, cfg)
    return pl, ctx, cfg


def fetch_with_window(days_ahead: int, p92: float = 8.09):
    """构造返回指定调价窗口天数的 fetch 假实现（矩阵/提醒用例用）。"""
    target = (datetime.now().date() + timedelta(days=days_ahead)).strftime("%Y%m%d")

    def fake(prov, key):
        d = fake_price_data(p92 if prov != "上海" else 8.05)
        d["next_change_time"] = target
        return d

    return fake


async def run_cmd(gen):
    out = []
    async for r in gen:
        out.append(r)
    return out


def png_ok(path_or_bytes):
    data = (
        path_or_bytes
        if isinstance(path_or_bytes, bytes)
        else open(path_or_bytes, "rb").read()
    )
    return data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 1000


def seed_history(store: Store, prov: str, days=5, base=8.0):
    today = datetime.now().date()
    for i in range(days):
        d = today - timedelta(days=i)
        key = d.isoformat()
        price = round(base + i * 0.01, 2)
        data = {
            "fuels": {
                "92": {
                    "price": price,
                    "change": 0.01,
                    "pct": None,
                    "before": price - 0.01,
                },
                "95": {
                    "price": price + 0.5,
                    "change": 0.0,
                    "pct": None,
                    "before": price + 0.5,
                },
            },
            "before_change_time": "",
            "next_change_time": "20261030",
        }
        store.store_prices(prov, data)
        h = store.history()
        entry = h[prov].pop(store.today(), None)
        if entry is not None:
            h[prov][key] = entry
            store._save(store.history_file, h)


# ======================= Prefs 配置校验（TC-C01~C15） =======================


class TestPrefsValidation(unittest.TestCase):
    def setUp(self):
        self.prefs = Prefs(base_config())

    def valid_payload(self):
        return {
            "provinces": ["江苏", "上海", "江苏"],
            "push_hour": 8,
            "push_minute": 30,
            "monthly_push": False,
            "private_targets": [{"id": "123456001", "limit": 10}],
            "group_targets": [
                {"id": "10001", "limit": None},
                {"id": "default:GroupMessage:20002", "limit": 0},
            ],
            "user_daily_limit": 3,
            "default_fuels": ["92", "95", "92"],
            "app_key": "  abc123  ",
        }

    def test_c01_valid_full_payload(self):
        errs = self.prefs.apply_web_update(self.valid_payload())
        self.assertEqual(errs, [])
        b = self.prefs.config["basic"]
        self.assertEqual(b["provinces"], ["江苏", "上海"])
        self.assertEqual((b["push_hour"], b["push_minute"]), (8, 30))
        self.assertFalse(b["monthly_push"])
        self.assertEqual(b["notify_private"], ["default:FriendMessage:123456001"])
        self.assertEqual(
            b["notify_group"],
            ["default:GroupMessage:10001", "default:GroupMessage:20002"],
        )
        self.assertEqual(
            b["target_limits"],
            {
                "default:FriendMessage:123456001": 10,
                "default:GroupMessage:20002": 0,
            },
        )
        self.assertEqual(b["user_daily_limit"], 3)
        self.assertEqual(b["default_fuels"], ["92", "95"])
        self.assertEqual(self.prefs.config["api"]["app_key"], "abc123")

    def test_c03_empty_provinces(self):
        p = self.valid_payload()
        p["provinces"] = []
        self.assertTrue(any("省份" in e for e in self.prefs.apply_web_update(p)))

    def test_c04_invalid_province(self):
        p = self.valid_payload()
        p["provinces"] = ["江苏", "火星"]
        self.assertTrue(any("火星" in e for e in self.prefs.apply_web_update(p)))

    def test_c05_time_boundaries_ok(self):
        for h, m in ((0, 0), (23, 59)):
            p = self.valid_payload()
            p["push_hour"], p["push_minute"] = h, m
            self.assertEqual(self.prefs.apply_web_update(p), [])

    def test_c06_time_invalid(self):
        for h, m in ((24, 0), (0, 60), (-1, 0), ("abc", 0)):
            p = self.valid_payload()
            p["push_hour"], p["push_minute"] = h, m
            self.assertNotEqual(self.prefs.apply_web_update(p), [])

    def test_c08_targets_mixed_and_dedup(self):
        p = self.valid_payload()
        p["group_targets"] = [
            {"id": "10001", "limit": 4},
            {"id": "10001", "limit": 9},
            {"id": "default:GroupMessage:30003", "limit": None},
        ]
        self.assertEqual(self.prefs.apply_web_update(p), [])
        self.assertEqual(
            self.prefs.config["basic"]["notify_group"],
            ["default:GroupMessage:10001", "default:GroupMessage:30003"],
        )
        self.assertEqual(
            self.prefs.config["basic"]["target_limits"],
            {"default:GroupMessage:10001": 4, "default:FriendMessage:123456001": 10},
        )

    def test_c10_limit_zero_ok(self):
        p = self.valid_payload()
        p["user_daily_limit"] = 0
        self.assertEqual(self.prefs.apply_web_update(p), [])
        self.assertEqual(self.prefs.config["basic"]["user_daily_limit"], 0)

    def test_c11_limit_not_int(self):
        p = self.valid_payload()
        p["user_daily_limit"] = "很多"
        self.assertTrue(any("上限" in e for e in self.prefs.apply_web_update(p)))

    def test_c13_fuels_invalid_or_empty(self):
        p = self.valid_payload()
        p["default_fuels"] = ["92", "93"]
        self.assertTrue(any("非法油号" in e for e in self.prefs.apply_web_update(p)))
        p["default_fuels"] = []
        self.assertTrue(any("油号" in e for e in self.prefs.apply_web_update(p)))

    def test_c14_empty_payload(self):
        self.assertGreaterEqual(len(self.prefs.apply_web_update({})), 3)

    def test_c15_save_called(self):
        cfg = FakeConfig(base_config())
        prefs = Prefs(cfg)
        self.assertEqual(prefs.apply_web_update(self.valid_payload()), [])
        prefs.save()
        self.assertEqual(cfg.saved, 1)

    def test_c18_c19_structured_limits_and_fallback(self):
        self.assertEqual(self.prefs.apply_web_update(self.valid_payload()), [])
        self.assertEqual(self.prefs.quota_limit_for("default:FriendMessage:123456001"), 10)
        self.assertEqual(self.prefs.quota_limit_for("default:GroupMessage:10001"), 3)
        self.assertEqual(self.prefs.quota_limit_for("default:GroupMessage:20002"), 0)

    def test_c21_invalid_target_limit_and_id(self):
        p = self.valid_payload()
        p["private_targets"] = [{"id": "123456001", "limit": -1}]
        self.assertTrue(any("负数" in e for e in self.prefs.apply_web_update(p)))
        p = self.valid_payload()
        p["private_targets"] = [{"id": "abc", "limit": 1}]
        self.assertTrue(any("无法识别" in e for e in self.prefs.apply_web_update(p)))
        p = self.valid_payload()
        p["private_targets"] = [{"id": "123456001", "limit": "很多"}]
        self.assertTrue(any("整数" in e for e in self.prefs.apply_web_update(p)))

    def test_c22_limits_survive_prefs_rebuild(self):
        self.assertEqual(self.prefs.apply_web_update(self.valid_payload()), [])
        rebuilt = Prefs(self.prefs.config)
        self.assertEqual(rebuilt.quota_limit_for("default:FriendMessage:123456001"), 10)
        payload = rebuilt.web_payload()
        self.assertEqual(payload["private_targets"], [{"id": "123456001", "limit": 10}])

    def test_c23_unknown_target_uses_global_default(self):
        self.assertEqual(self.prefs.apply_web_update(self.valid_payload()), [])
        self.assertEqual(self.prefs.quota_limit_for("default:GroupMessage:99999"), 3)

    def test_c24_real_schema_persists_dynamic_target_limits(self):
        plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(plugin_root, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        d = os.path.join(TEST_ROOT, "schema_roundtrip")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "config.json")
        cfg = AstrBotConfig(config_path=path, schema=schema)
        prefs = Prefs(cfg)
        self.assertEqual(prefs.apply_web_update(self.valid_payload()), [])
        prefs.save()
        rebuilt = AstrBotConfig(config_path=path, schema=schema)
        self.assertEqual(
            rebuilt["basic"]["target_limits"],
            {
                "default:FriendMessage:123456001": 10,
                "default:GroupMessage:20002": 0,
            },
        )
        self.assertEqual(
            Prefs(rebuilt).quota_limit_for("default:FriendMessage:123456001"), 10
        )


# ======================= 存储层（TC-E01~E08） =======================


class TestStorage(unittest.TestCase):
    def setUp(self):
        d = os.path.join(TEST_ROOT, "storage", self.id().split(".")[-1])
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        self.store = Store(os.path.join(d, "history.json"), os.path.join(d, "quota.json"))

    @staticmethod
    def _data(price=8.0):
        return {
            "fuels": {"92": {"price": price, "change": 0.0, "pct": None, "before": price}},
            "before_change_time": "",
            "next_change_time": "",
        }

    def test_e01_store_and_get(self):
        self.store.store_prices("江苏", self._data())
        day = self.store.today()
        got = self.store.get_day("江苏", day)
        self.assertIsNotNone(got)
        self.assertEqual(got["fuels"]["92"]["price"], 8.0)

    def test_e02_get_prev_skips_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.store.store_prices("江苏", self._data())
        h = self.store.history()
        h["江苏"][yesterday] = h["江苏"].pop(today)
        self.store._save(self.store.history_file, h)
        self.assertIsNotNone(self.store.get_prev("江苏"))

    def test_e03_trend_series_window(self):
        seed_history(self.store, "江苏", days=10)
        series = self.store.trend_series("江苏", 7)
        self.assertLessEqual(len(series), 7)
        self.assertTrue(all(len(x) == 2 for x in series))

    def test_e04_month_series(self):
        seed_history(self.store, "江苏", days=40)
        now = datetime.now()
        series = self.store.month_series("江苏", now.year, now.month)
        self.assertTrue(all(k.startswith(now.strftime("%Y-%m")) for k, _ in series))

    def test_e05_quota_unlimited(self):
        for _ in range(10):
            ok, _, _ = self.store.quota_consume("u1", 0)
            self.assertTrue(ok)

    def test_e06_quota_counting(self):
        ok, used, _ = self.store.quota_consume("u1", 2)
        self.assertTrue(ok and used == 1)
        ok, used, _ = self.store.quota_consume("u1", 2)
        self.assertTrue(ok and used == 2)

    def test_e07_quota_exceeded(self):
        self.store.quota_consume("u1", 1)
        ok, used, limit = self.store.quota_consume("u1", 1)
        self.assertFalse(ok)
        self.assertEqual((used, limit), (1, 1))

    def test_e08_quota_per_user(self):
        self.store.quota_consume("u1", 1)
        self.store.quota_consume("u1", 1)
        ok, _, _ = self.store.quota_consume("u2", 1)
        self.assertTrue(ok)


# ======================= 查询偏好事件与省份聚合（oil_1001 F7/F8） =======================


class TestQueryEvents(unittest.TestCase):
    def setUp(self):
        d = os.path.join(TEST_ROOT, "events", self.id().split(".")[-1])
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        self.store = Store(
            os.path.join(d, "history.json"),
            os.path.join(d, "quota.json"),
            os.path.join(d, "events.json"),
        )

    def test_f07_record_and_rolling_50(self):
        for i in range(55):
            self.store.record_query("umo1", f"u{i % 3}", [f"省{i}"])
        events = self.store.query_events("umo1")
        self.assertEqual(len(events), 50)
        self.assertEqual(events[0]["p"], ["省5"])  # 最旧的 5 条被挤出
        self.assertEqual(events[-1]["p"], ["省54"])
        self.assertEqual(self.store.query_events("umo2"), [])

    def test_f07_no_events_file_noop(self):
        store = Store("a.json", "b.json")  # 未启用事件文件的旧构造方式
        store.record_query("umo", "u", ["江苏"])
        self.assertEqual(store.query_events("umo"), [])

    def test_oil1004_manage_update_delete_clear(self):
        """记忆管理（oil_1004）：按 (u,p,t) 精确匹配改/删，清空目标。"""
        self.store.record_query("umo1", "u1", ["江苏"])
        self.store.record_query("umo1", "u2", ["上海"])
        ev1 = self.store.query_events("umo1")[0]

        # 改：匹配事件省份集更新，其余不动
        self.assertTrue(self.store.update_query_event("umo1", ev1, ["广东", "四川"]))
        events = self.store.query_events("umo1")
        self.assertEqual(events[0]["p"], ["广东", "四川"])
        self.assertEqual(events[1]["p"], ["上海"])
        # 不匹配/空省份 → False 且不落盘
        self.assertFalse(
            self.store.update_query_event("umo1", {"u": "x", "p": [], "t": ""}, ["江苏"])
        )
        self.assertFalse(self.store.update_query_event("umo1", ev1, []))

        # 删：匹配删除；目标删空后键移除
        ev2 = self.store.query_events("umo1")[1]
        self.assertTrue(self.store.delete_query_event("umo1", ev2))
        self.assertEqual(len(self.store.query_events("umo1")), 1)
        self.assertTrue(
            self.store.delete_query_event("umo1", self.store.query_events("umo1")[0])
        )
        self.assertEqual(self.store.query_events("umo1"), [])
        self.assertNotIn("umo1", self.store.all_query_events())

        # 清空：无键时返回 False
        self.assertFalse(self.store.clear_query_events("umo1"))
        self.store.record_query("umo1", "u1", ["江苏"])
        self.assertTrue(self.store.clear_query_events("umo1"))
        self.assertEqual(self.store.query_events("umo1"), [])

    def test_f08_picker_fallback_and_top(self):
        from astrbot_plugin_oil_price.modules.storage import (
            pick_preferred_provinces as pick,
        )

        self.assertIsNone(pick([]))
        self.assertIsNone(pick([{"u": "a", "p": ["江苏"]}, {"u": "a", "p": ["上海"]}]))
        # 4 个近期省份只取前 3
        events = [
            {"u": "a", "p": ["江苏"]},
            {"u": "a", "p": ["上海"]},
            {"u": "a", "p": ["浙江"]},
            {"u": "a", "p": ["安徽"]},
        ]
        self.assertEqual(pick(events), ["安徽", "浙江", "上海"])

    def test_f08_picker_private_recency(self):
        """私聊：按本人每省最近一次，最新查询的省排前。"""
        from astrbot_plugin_oil_price.modules.storage import (
            pick_preferred_provinces as pick,
        )

        events = [
            {"u": "me", "p": ["江苏"]},
            {"u": "me", "p": ["广东"]},
            {"u": "me", "p": ["江苏"]},  # 江苏更近
        ]
        self.assertEqual(pick(events), ["江苏", "广东"])

    def test_f08_picker_group_member_dedup(self):
        """群聊：单人高频刷屏不能淹没其他成员的省份（按成员×省份去重 + 近期优先）。"""
        from astrbot_plugin_oil_price.modules.storage import (
            pick_preferred_provinces as pick,
        )

        events = [{"u": "刷子", "p": ["广东"]}] * 15
        events.insert(5, {"u": "路人甲", "p": ["广东"]})
        events.insert(6, {"u": "路人乙", "p": ["上海"]})  # 仅查 1 次
        picked = pick(events)
        # 上海仅 1 次仍入选（不被 15 次刷屏挤出）——去重后频次不再起作用
        self.assertEqual(picked, ["广东", "上海"])

    def test_f08_picker_window_20(self):
        """只看最近 20 条：更早查过的省不再参与。"""
        from astrbot_plugin_oil_price.modules.storage import (
            pick_preferred_provinces as pick,
        )

        events = [{"u": "a", "p": ["四川"]}] * 5 + [{"u": "a", "p": ["广东"]}] * 20
        self.assertEqual(pick(events), ["广东"])


# ======================= 数据源（TC-G01~G05） =======================


class TestDatasource(unittest.TestCase):
    def test_g01_no_appkey(self):
        with self.assertRaises(datasource.ShowApiError):
            datasource.fetch_province("江苏", "")

    def _run_with_resp(self, body: bytes):
        with mock.patch.object(
            datasource.urllib.request, "urlopen", return_value=FakeResp(body)
        ):
            return datasource.fetch_province("江苏", "k")

    def test_g02_normal_response(self):
        body = json.dumps(
            {
                "showapi_res_code": 0,
                "showapi_res_body": {
                    "ret_code": 0,
                    "p92": {
                        "price": "8.09",
                        "change": "0.10",
                        "change_percent": "1.2",
                        "before_change_price": "7.99",
                    },
                    "p95": {"price": "8.62", "change": "", "change_percent": ""},
                },
            }
        ).encode()
        data = self._run_with_resp(body)
        self.assertEqual(data["fuels"]["92"]["price"], 8.09)
        self.assertEqual(data["fuels"]["92"]["change"], 0.10)
        self.assertIsNone(data["fuels"]["95"]["change"])
        self.assertEqual(data["next_change_time"], "")

    def test_g03_platform_error(self):
        body = json.dumps(
            {"showapi_res_code": -1, "showapi_res_error": "key 无效"}
        ).encode()
        with self.assertRaises(datasource.ShowApiError):
            self._run_with_resp(body)

    def test_g04_business_error(self):
        body = json.dumps(
            {"showapi_res_code": 0, "showapi_res_body": {"ret_code": 1}}
        ).encode()
        with self.assertRaises(datasource.ShowApiError):
            self._run_with_resp(body)

    def test_g05_network_error(self):
        with mock.patch.object(
            datasource.urllib.request, "urlopen", side_effect=OSError("超时")
        ):
            with self.assertRaises(datasource.ShowApiError):
                datasource.fetch_province("江苏", "k")


# ======================= 文案 =======================


class TestReporting(unittest.TestCase):
    def test_fuel_line_branches(self):
        self.assertIn("↑涨", reporting.fuel_line("92", {"price": 8.09, "change": 0.1}))
        self.assertIn("↓降", reporting.fuel_line("92", {"price": 8.09, "change": -0.1}))
        self.assertIn("持平", reporting.fuel_line("92", {"price": 8.09, "change": 0.0}))
        self.assertNotIn("（", reporting.fuel_line("92", {"price": 8.09, "change": None}))

    def test_countdown_branches(self):
        today = datetime.now().date()
        self.assertIn(
            "7 天",
            reporting.adjust_countdown_text((today + timedelta(days=7)).strftime("%Y%m%d")),
        )
        self.assertIn(
            "明晚",
            reporting.adjust_countdown_text((today + timedelta(days=1)).strftime("%Y%m%d")),
        )
        self.assertIn("就是今晚", reporting.adjust_countdown_text(today.strftime("%Y%m%d")))
        self.assertEqual(reporting.adjust_countdown_text(""), "")

    def test_text_report_rise_and_fuels_filter(self):
        data = {
            "fuels": {
                "92": {"price": 8.09, "change": 0.1, "pct": None},
                "95": {"price": 8.62, "change": -0.05, "pct": None},
                "98": {"price": 9.98, "change": 0.0, "pct": None},
            },
            "next_change_time": "",
        }
        text = reporting.build_text_report("江苏", data, fuels=["92"])
        self.assertIn("92号", text)
        self.assertNotIn("95号", text)
        self.assertIn("油价上涨提醒", text)

    def test_text_report_drop_and_flat(self):
        drop = {
            "fuels": {
                "92": {"price": 8.26, "change": -0.32, "pct": None},
                "95": {"price": 8.79, "change": -0.33, "pct": None},
                "98": {"price": 10.86, "change": -0.33, "pct": None},
            },
            "next_change_time": "20261107",
        }
        text = reporting.build_text_report("江苏", drop, fuels=["92", "95", "98"])
        self.assertIn("油价下调提醒", text)
        self.assertIn("下调！", text)
        self.assertIn("更省钱", text)

        flat = {
            "fuels": {
                "92": {"price": 8.58, "change": 0.0, "pct": None},
                "95": {"price": 9.12, "change": 0.0, "pct": None},
                "98": {"price": 11.19, "change": 0.0, "pct": None},
            },
            "next_change_time": "20261107",
        }
        text2 = reporting.build_text_report("江苏", flat, fuels=["92", "95", "98"])
        self.assertNotIn("上涨提醒", text2)
        self.assertNotIn("下调提醒", text2)
        self.assertIn("💡", text2)

    def test_multi_text_format_and_filter(self):
        rows = [("江苏", fake_price_data()), ("上海", fake_price_data(8.05))]
        text = reporting.build_multi_text(rows, fuels=["92", "95"])
        self.assertIn("【江苏】", text)
        self.assertIn("【上海】", text)
        self.assertNotIn("98#", text)

    def test_oil1003_remind_text_price_details(self):
        """调价提醒全文：单省=倒计时+无前缀价格行；多省=价格行带省份前缀；
        无窗口省份整行跳过；全无倒计时返回空串。"""
        rows = [("江苏", fake_price_data(8.09))]
        text = reporting.build_remind_text(rows, fuels=["92", "95"])
        self.assertIn("调价提醒", text)
        self.assertIn("距下次调价窗口", text)
        self.assertIn("92号汽油：8.09 元/升", text)
        self.assertIn("95号汽油", text)
        self.assertNotIn("98号", text)  # fuels 过滤
        self.assertNotIn("江苏 92号", text)  # 单省不加前缀

        multi = reporting.build_remind_text(
            [("江苏", fake_price_data(8.09)), ("上海", fake_price_data(8.05))],
            fuels=["92"],
        )
        self.assertIn("江苏 92号汽油：", multi)
        self.assertIn("上海 92号汽油：", multi)
        self.assertGreaterEqual(multi.count("距下次调价窗口"), 2)

        no_window = fake_price_data(8.09)
        no_window["next_change_time"] = ""
        self.assertEqual(
            reporting.build_remind_text([("江苏", no_window)], fuels=["92"]), ""
        )


# ======================= 渲染（TC-F01~F09） =======================


class TestCharts(unittest.TestCase):
    def setUp(self):
        self.out = os.path.join(TEST_ROOT, "charts", self.id().split(".")[-1])
        os.makedirs(self.out, exist_ok=True)

    @staticmethod
    def png_ok(path):
        with open(path, "rb") as f:
            head = f.read(8)
        return head == b"\x89PNG\r\n\x1a\n" and os.path.getsize(path) > 1000

    @staticmethod
    def png_height(path):
        from PIL import Image

        with Image.open(path) as im:
            return im.size[1]

    def _data(self):
        return fake_price_data()

    def test_f01_report_card_full(self):
        path = charts.render_report_card("江苏", self._data(), None, "2026-10-23", self.out)
        self.assertTrue(self.png_ok(path))

    def test_f02_report_card_fuels_filter_fewer_rows(self):
        full = charts.render_report_card("江苏", self._data(), None, "2026-10-23", self.out)
        h_full = self.png_height(full)
        part = charts.render_report_card(
            "江苏", self._data(), None, "2026-10-23", self.out, ["92", "95"]
        )
        h_part = self.png_height(part)
        self.assertLess(h_part, h_full)  # 两行 < 四行（两图同名，需先测 full）

    def test_f03_multi_card(self):
        rows = [("江苏", self._data()), ("上海", self._data())]
        self.assertTrue(self.png_ok(charts.render_multi_card(rows, "2026-10-23", self.out)))

    def test_f04_multi_card_missing_prov_data(self):
        rows = [("江苏", self._data()), ("上海", {"fuels": {}, "next_change_time": ""})]
        self.assertTrue(self.png_ok(charts.render_multi_card(rows, "2026-10-23", self.out)))

    def test_f05_price_card_and_receipt(self):
        p1 = charts.render_price_card(
            "江苏", "92号汽油", 8.09, 0.1, 1.2, "2026-10-23", self.out
        )
        p2 = charts.render_receipt(
            30, 8.09, 242.7, "江苏", "92号汽油", 3.0, "2026-10-23", self.out
        )
        self.assertTrue(self.png_ok(p1))
        self.assertTrue(self.png_ok(p2))

    def test_f06_line_chart_multi_lines(self):
        store = Store(os.path.join(self.out, "h.json"), os.path.join(self.out, "q.json"))
        seed_history(store, "江苏", days=10)
        seed_history(store, "上海", days=10)
        end = datetime.now().date().isoformat()
        start = (datetime.now().date() - timedelta(days=9)).isoformat()
        lines = charts.build_lines(
            store.history, ["江苏", "上海"], ["92", "95"], start, end
        )
        self.assertEqual(len(lines), 4)
        buf = charts.png_bytes(charts.line_chart_image(lines, "油价走势", "近 10 天"))
        self.assertTrue(buf[:8] == b"\x89PNG\r\n\x1a\n")

    def test_f10_line_chart_ui_mode(self):
        """F10：WebUI 页面模式窄画布（760），QQ 推送默认版式（1040）不变。"""
        lines = [
            {
                "label": "江苏 92#",
                "points": [("2026-10-01", 8.0), ("2026-10-02", 8.2), ("2026-10-03", 8.1)],
            },
            {
                "label": "上海 92#",
                "points": [("2026-10-01", 8.1), ("2026-10-02", 8.0), ("2026-10-03", 8.3)],
            },
        ]
        for theme in ("light", "dark"):
            img = charts.line_chart_image(
                lines, "油价走势", "近 3 天", theme=theme, ui=True
            )
            self.assertEqual(img.size, (760, 600))
            self.assertTrue(charts.png_bytes(img)[:8] == b"\x89PNG\r\n\x1a\n")
        img_qq = charts.line_chart_image(lines, "油价走势", "近 3 天")
        self.assertEqual(img_qq.size, (1040, 620))

    def test_f07_line_chart_empty(self):
        buf = charts.png_bytes(charts.line_chart_image([], "油价走势", ""))
        self.assertTrue(buf[:8] == b"\x89PNG\r\n\x1a\n")

    def test_f08_build_lines_filters(self):
        hist = {
            "江苏": {
                "2026-10-01": {"fuels": {"92": {"price": 8.0}}},
                "2026-10-02": {"fuels": {}},
                "2026-10-03": {"fuels": {"92": {"price": 8.1}}},
                "2026-09-30": {"fuels": {"92": {"price": 7.9}}},
            }
        }
        lines = charts.build_lines(
            lambda: hist, ["江苏"], ["92"], "2026-10-01", "2026-10-31"
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual([p for p, _ in lines[0]["points"]], ["2026-10-01", "2026-10-03"])
        self.assertEqual(
            charts.build_lines(lambda: hist, ["江苏"], ["95"], "2026-10-01", "2026-10-31"),
            [],
        )

    def test_f09_summarize(self):
        lines = [
            {
                "label": "江苏 92#",
                "points": [
                    ("2026-10-01", 8.0),
                    ("2026-10-02", 8.2),
                    ("2026-10-03", 8.1),
                ],
            }
        ]
        s = charts.summarize_lines(lines)[0]
        self.assertEqual((s["first"], s["last"]), (8.0, 8.1))
        self.assertAlmostEqual(s["diff"], 0.1)
        self.assertEqual((s["min"], s["max"]), (8.0, 8.2))
        self.assertAlmostEqual(s["avg"], 8.1)


# ======================= AI 生图（图文同源） =======================


class TestGenImage(unittest.TestCase):
    def test_prompt_keeps_text_verbatim(self):
        from astrbot_plugin_oil_price.modules import genimage

        text = (
            "🚨🚨 油价上涨提醒 🚨🚨\n⛽ 江苏油价（10-24）\n92号汽油：8.58 元/升（↑涨 0.32）"
        )
        prompt = genimage.prompt_from_text(text)
        self.assertIn("92号汽油：8.58 元/升（↑涨 0.32）", prompt)
        self.assertIn("江苏油价（10-24）", prompt)
        self.assertIn("油价上涨提醒", prompt)
        with self.assertRaises(genimage.GenImageError):
            genimage.prompt_from_text("   ")

    def test_oil1001_prompt_multi_countdown_lines(self):
        """F6：多省调价提醒的每一条倒计时行都进提示词（修复只取第一行的缺陷）。"""
        from astrbot_plugin_oil_price.modules import genimage

        text = (
            "🚨🚨 调价提醒 🚨🚨\n"
            "⛽ 江苏：距下次调价窗口（10月30日 24时）还有 1 天\n"
            "⛽ 广东：明晚 24 时调价窗口开启！要加油的今晚抓紧"
        )
        prompt = genimage.prompt_from_text(text)
        self.assertIn("江苏：距下次调价窗口（10月30日 24时）还有 1 天", prompt)
        self.assertIn("广东：明晚 24 时调价窗口开启！要加油的今晚抓紧", prompt)
        self.assertIn("第1行", prompt)
        self.assertIn("第2行", prompt)
        self.assertIn("无油价数据区", prompt)  # 无价格行时不画空面板

    def test_endpoints_from_provider_config(self):
        from astrbot_plugin_oil_price.modules import genimage

        eps = genimage.endpoints_from_provider_config(
            {"key": ["k1", "k2"], "api_base": "https://open.bigmodel.cn/api/paas/v4/"}
        )
        self.assertEqual(
            eps,
            [
                ("k1", "https://open.bigmodel.cn/api/paas/v4/images/generations", ""),
                ("k2", "https://open.bigmodel.cn/api/paas/v4/images/generations", ""),
            ],
        )
        eps2 = genimage.endpoints_from_provider_config(
            {
                "key": "solo",
                "api_base": "https://api.deepseek.com/v1",
                "model": "gpt-image-2.5",
            }
        )
        self.assertEqual(
            eps2,
            [("solo", "https://api.deepseek.com/v1/images/generations", "gpt-image-2.5")],
        )
        eps3 = genimage.endpoints_from_provider_config(
            {
                "key": "k",
                "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-image-3.0",
            }
        )
        self.assertEqual(
            eps3,
            [
                (
                    "k",
                    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
                    "multimodal-generation/generation",
                    "qwen-image-3.0",
                )
            ],
        )
        self.assertEqual(genimage.endpoints_from_provider_config({"key": ["k"]}), [])
        self.assertEqual(genimage.endpoints_from_provider_config(None), [])

    def test_generate_no_endpoints_raises(self):
        from astrbot_plugin_oil_price.modules import genimage

        with self.assertRaises(genimage.GenImageError):
            genimage.generate("p", [])

    def test_generate_per_endpoint_model_and_failover(self):
        """端点自带模型名优先于全局 gen_model；上一端点失败继续尝试下一端点。

        对应线上事故：全局模型名 gpt-image-2.5 被发给百炼端点导致
        Model not exist，可用的百炼兜底被废掉（oil_1001 修复）。
        """
        import tempfile

        from astrbot_plugin_oil_price.modules import genimage

        calls = []

        def fake_once(prompt, api_key, url, model, size):
            calls.append((url, model))
            if len(calls) == 1:
                raise RuntimeError("HTTP Error 403")
            return "b64", base64.b64encode(b"\x89PNG" + b"0" * 2000).decode()

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(genimage, "_gen_once", fake_once),
        ):
            path = genimage.generate(
                "p",
                [
                    ("k1", "https://x.example/v1/images/generations", "gpt-image-2.5"),
                    ("k2", "https://dashscope.aliyuncs.com/api/v1/x", ""),
                ],
                model="qwen-image-3.0",
                out_dir=td,
                name="t.png",
            )
            size = os.path.getsize(path)
        self.assertEqual(
            calls,
            [
                ("https://x.example/v1/images/generations", "gpt-image-2.5"),
                ("https://dashscope.aliyuncs.com/api/v1/x", "qwen-image-3.0"),
            ],
        )
        self.assertGreater(size, 1024)

    def test_styles_and_random_no_repeat(self):
        from astrbot_plugin_oil_price.modules import genimage

        self.assertEqual(len(genimage.STYLES), 5)
        for s in genimage.STYLES:
            self.assertTrue(s.get("name") and s.get("text"))
        for last in (None, 0, 1, 2, 3, 4):
            for _ in range(30):
                idx = genimage.pick_style_index(last)
                self.assertTrue(0 <= idx < len(genimage.STYLES))
                self.assertNotEqual(idx, last)

    def test_prefs_push_fields(self):
        from astrbot_plugin_oil_price.config import Prefs

        p = Prefs(
            {
                "basic": {
                    "push_frequency": "weekly",
                    "push_weekday": 5,
                    "change_remind_days": 2,
                }
            }
        )
        self.assertEqual(p.push_frequency(), "weekly")
        self.assertEqual(p.push_weekday(), 5)
        self.assertEqual(p.change_remind_days(), 2)
        self.assertFalse(p.auto_provinces())
        p2 = Prefs(
            {
                "basic": {
                    "push_frequency": "bad",
                    "push_weekday": 9,
                    "change_remind_days": 7,
                }
            }
        )
        self.assertEqual(p2.push_frequency(), "daily")
        self.assertEqual(p2.push_weekday(), 0)
        self.assertEqual(p2.change_remind_days(), 1)

    def test_oil1001_on_change_migration(self):
        """F5：旧值 on_change 读取与 Web 提交均自动迁移为 off。"""
        from astrbot_plugin_oil_price.config import Prefs

        self.assertEqual(
            Prefs({"basic": {"push_frequency": "on_change"}}).push_frequency(), "off"
        )
        self.assertEqual(
            Prefs({"basic": {"push_frequency": "off"}}).push_frequency(), "off"
        )
        prefs = Prefs(base_config())
        payload = self._valid_push_payload()
        payload["push_frequency"] = "on_change"
        self.assertEqual(prefs.apply_web_update(payload), [])
        self.assertEqual(prefs.config["basic"]["push_frequency"], "off")
        payload["push_frequency"] = "sometimes"
        self.assertTrue(any("推送频率" in e for e in prefs.apply_web_update(payload)))

    def test_oil1001_auto_provinces_web_roundtrip(self):
        from astrbot_plugin_oil_price.config import Prefs

        prefs = Prefs(base_config())
        payload = self._valid_push_payload()
        payload["auto_provinces"] = True
        self.assertEqual(prefs.apply_web_update(payload), [])
        self.assertTrue(prefs.config["basic"]["auto_provinces"])
        rebuilt = Prefs(prefs.config)
        self.assertTrue(rebuilt.auto_provinces())
        self.assertTrue(rebuilt.web_payload()["auto_provinces"])

    @staticmethod
    def _valid_push_payload():
        return {
            "provinces": ["江苏"],
            "push_hour": 9,
            "push_minute": 0,
            "push_frequency": "off",
            "change_remind_days": 1,
            "monthly_push": True,
            "private_targets": [],
            "group_targets": [{"id": "10001", "limit": None}],
            "user_daily_limit": 5,
            "default_fuels": ["92"],
            "app_key": "k",
        }

    def test_oil1002_norm_provs_canonical_order(self):
        """多省行序按省份表归一：与输入顺序解耦（缓存共享的前提）。"""
        norm = plugin_main.OilPricePlugin._norm_provs
        self.assertEqual(norm(["上海", "广东", "江苏"]), ["广东", "江苏", "上海"])
        self.assertEqual(norm(["江苏"]), ["江苏"])
        self.assertEqual(norm(["广东", "江苏"]), norm(["江苏", "广东"]))

    def test_oil1002_flat_notice_deterministic(self):
        """持平日提示语按（日期, 省份）确定选取：当天内文本稳定可作缓存键。"""
        flat = {
            "fuels": {
                "92": {"price": 8.0, "before": 8.0, "change": 0.0, "pct": 0.0},
                "95": {"price": 8.6, "before": 8.6, "change": 0.0, "pct": 0.0},
                "98": {"price": 9.9, "before": 9.9, "change": 0.0, "pct": 0.0},
                "0": {"price": 7.7, "before": 7.7, "change": 0.0, "pct": 0.0},
            },
            "next_change_time": "",
        }
        t1 = reporting.build_text_report("江苏", flat)
        t2 = reporting.build_text_report("江苏", flat)
        self.assertEqual(t1, t2)
        notice_line = [ln for ln in t1.splitlines() if "💡" in ln][0]
        self.assertTrue(any(n in notice_line for n in reporting.FLAT_NOTICES))


# ======================= 指令 + 推送 + WebAPI（异步） =======================


class AsyncPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        name = self.id().split(".")[-1]
        self.plugin, self.ctx, self.cfg = make_plugin(name=name)
        self.fetch_patcher = mock.patch.object(
            plugin_main.datasource,
            "fetch_province",
            side_effect=lambda prov, key: fake_price_data(8.05 if prov == "上海" else 8.09),
        )
        self.gen_patcher = mock.patch.object(
            plugin_main.genimage, "generate", _fake_genimage_generate
        )
        self.fetch_patcher.start()
        self.gen_patcher.start()

    async def asyncTearDown(self):
        self.fetch_patcher.stop()
        self.gen_patcher.stop()
        for t in self.plugin._tasks:
            t.cancel()

    # ---------- 指令（TC-A） ----------
    async def test_a01_oil_price_with_province(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.oil_price_cmd(ev, "广东"))
        self.assertIn("广东", out[0]["text"])
        self.assertIn("92号", out[0]["text"])

    async def test_a02_oil_price_default_multi(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.oil_price_cmd(ev, ""))
        self.assertIn("【江苏】", out[0]["text"])
        self.assertIn("【上海】", out[0]["text"])

    async def test_a03_oil_price_fetch_error(self):
        ev = MockEvent()
        with mock.patch.object(
            plugin_main.datasource,
            "fetch_province",
            side_effect=RuntimeError("接口炸了"),
        ):
            out = await run_cmd(self.plugin.oil_price_cmd(ev, "广东"))
        self.assertIn("失败", out[0]["text"])

    async def test_a04_today_single_province_image(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.today_cmd(ev, "广东"))
        self.assertEqual(out[-1]["type"], "chain")

    async def test_a05_today_default_multi_image(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.today_cmd(ev, ""))
        self.assertEqual(out[-1]["type"], "chain")

    async def test_a05b_today_multi_province_args(self):
        seen = []
        with mock.patch.object(
            plugin_main.datasource,
            "fetch_province",
            side_effect=lambda prov, key: (seen.append(prov), fake_price_data(8.09))[1],
        ):
            ev = MockEvent(message_str="今日油价 广东 上海")
            out = await run_cmd(self.plugin.today_cmd(ev, ""))
        self.assertEqual(out[-1]["type"], "chain")
        self.assertIn("广东", seen)
        self.assertIn("上海", seen)
        self.assertNotIn("江苏", seen)

    async def test_a05c_today_single_arg_still_works(self):
        seen = []
        with mock.patch.object(
            plugin_main.datasource,
            "fetch_province",
            side_effect=lambda prov, key: (seen.append(prov), fake_price_data(8.09))[1],
        ):
            ev = MockEvent(message_str="今日油价 广东")
            out = await run_cmd(self.plugin.today_cmd(ev, ""))
        self.assertEqual(out[-1]["type"], "chain")
        self.assertEqual(seen, ["广东"])

    async def test_a06_today_render_fail_text_fallback(self):
        """单省出图失败：回退完整多行报告（不再是速览挤一行）+ 失败提示。"""
        ev = MockEvent()
        with mock.patch.object(
            plugin_main.genimage, "generate", side_effect=RuntimeError("生图平台不可用")
        ):
            out = await run_cmd(self.plugin.today_cmd(ev, "广东"))
        text = out[-1]["text"]
        self.assertIn("广东油价", text)
        self.assertIn("92号", text)
        self.assertIn("\n", text)  # 完整报告按油号分行
        self.assertNotIn("今日油价速览", text)
        self.assertIn("海报生成失败", text)

    async def test_a06b_today_render_fail_multi_keeps_summary(self):
        """多省出图失败：仍用紧凑速览 + 失败提示。"""
        ev = MockEvent()
        with mock.patch.object(
            plugin_main.genimage, "generate", side_effect=RuntimeError("生图平台不可用")
        ):
            out = await run_cmd(self.plugin.today_cmd(ev, ""))
        text = out[-1]["text"]
        self.assertIn("今日油价速览", text)
        self.assertIn("【江苏】", text)
        self.assertIn("海报生成失败", text)

    async def test_a06c_push_report_fail_single_prov_full_text(self):
        """单省推送出图失败：回退完整报告而非速览（oil_1001 修复）。"""
        with mock.patch.object(
            plugin_main.genimage, "generate", side_effect=RuntimeError("403")
        ):
            await self.plugin._push_report(
                [("江苏", fake_price_data(8.09))], ["default:GroupMessage:10001"]
            )
        parts = self.ctx.sent_messages[0][1].chain
        text = parts[0].text
        self.assertIn("江苏油价", text)
        self.assertNotIn("今日油价速览", text)
        self.assertIn("海报生成失败", text)

    # ---------- oil_1002：当日生图缓存 ----------

    def _counting_gen(self, calls: list):
        """包一层计数假的生图实现，返回可 patch 的函数。"""

        def fake(prompt, endpoints, model=None, size=None, out_dir=None, name="g.png"):
            calls.append(name)
            return _fake_genimage_generate(prompt, endpoints, model, size, out_dir, name)

        return fake

    async def test_oil1002_hit_skips_gen_and_wait(self):
        """同日同省两次查询：只真实生图 1 次；第二次无等待语、直接发图。"""
        calls = []
        with mock.patch.object(plugin_main.genimage, "generate", self._counting_gen(calls)):
            out1 = await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
            out2 = await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
        self.assertEqual(len(calls), 1)
        self.assertEqual([r["type"] for r in out1], ["plain", "chain"])  # 首次有等待语
        self.assertEqual([r["type"] for r in out2], ["chain"])  # 命中：无等待语直接发图
        meta = calls[0] + ".meta.json"
        self.assertTrue(os.path.isfile(os.path.join(plugin_main.TMP_DIR, meta)))

    async def test_oil1002_push_and_query_share_cache(self):
        """单省推送后查询（反之亦然）同文本命中：合计只生图 1 次。"""
        calls = []
        with mock.patch.object(plugin_main.genimage, "generate", self._counting_gen(calls)):
            await self.plugin._push_report(
                [("江苏", fake_price_data(8.09))], ["default:GroupMessage:10001"]
            )
            await run_cmd(self.plugin.today_cmd(MockEvent(), "江苏"))
        self.assertEqual(len(calls), 1)

    async def test_oil1002_multi_order_normalized_shared(self):
        """`广东 江苏` 与 `江苏 广东` 归一为同一文本：命中同一张缓存图。"""
        calls = []
        with mock.patch.object(plugin_main.genimage, "generate", self._counting_gen(calls)):
            out1 = await run_cmd(
                self.plugin.today_cmd(MockEvent(message_str="今日油价 广东 江苏"), "")
            )
            out2 = await run_cmd(
                self.plugin.today_cmd(MockEvent(message_str="今日油价 江苏 广东"), "")
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(out1[-1]["type"], "chain")
        self.assertEqual([r["type"] for r in out2], ["chain"])
        self.assertEqual(
            os.path.basename(out1[-1]["parts"][0].path),
            os.path.basename(out2[-1]["parts"][0].path),
        )

    async def test_oil1002_fuels_change_invalidates(self):
        """改默认油号后文本变化：哈希不一致 → 重生成。"""
        calls = []
        with mock.patch.object(plugin_main.genimage, "generate", self._counting_gen(calls)):
            await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
            self.cfg["basic"]["default_fuels"] = ["92"]
            await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
        self.assertEqual(len(calls), 2)

    async def test_oil1002_cache_off_keeps_legacy(self):
        """gen_cache 关闭：每次真实生图、保留等待语（行为同 v1.1.1）。"""
        self.cfg["api"]["gen_cache"] = False
        calls = []
        with mock.patch.object(plugin_main.genimage, "generate", self._counting_gen(calls)):
            out1 = await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
            out2 = await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
        self.assertEqual(len(calls), 2)
        self.assertEqual([r["type"] for r in out1], ["plain", "chain"])
        self.assertEqual([r["type"] for r in out2], ["plain", "chain"])

    async def test_oil1002_metaless_png_regenerates(self):
        """只有 PNG 没有 meta 的历史文件：视为未命中，重生成并补写 meta。"""
        calls = []
        text = self.plugin._report_text("广东", fake_price_data(8.09))
        h = self.plugin._text_hash(text)
        path = os.path.join(
            plugin_main.TMP_DIR,
            f"ai_广东_{self.plugin.store.today()}_{h[:8]}.png",
        )
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)
        with mock.patch.object(plugin_main.genimage, "generate", self._counting_gen(calls)):
            await run_cmd(self.plugin.today_cmd(MockEvent(), "广东"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(os.path.isfile(path + ".meta.json"))

    async def test_oil1002_tmp_cleanup(self):
        """mtime TTL 清理：AI 图/meta/单油卡 3 天、月度图 60 天，孤儿元数据一并清走。"""
        tmp = plugin_main.TMP_DIR
        now = time.time()
        expect_kept = {
            "ai_江苏_2026-10-20_ab12cd34.png": False,  # 5 天前 → 过期
            "ai_江苏_2026-10-20_ab12cd34.png.meta.json": False,
            "ai_江苏_today_99ff8811.png": True,  # 刚生成 → 保留
            "receipt_50_2026-10-20.png": False,  # 4 天前 → 过期
            "monthly_2026-08.png": False,  # 61 天前 → 过期
            "monthly_2026-10.png": True,  # 刚生成 → 保留
        }
        ages = {
            "ai_江苏_2026-10-20_ab12cd34.png": 5 * 86400,
            "ai_江苏_2026-10-20_ab12cd34.png.meta.json": 5 * 86400,
            "ai_江苏_today_99ff8811.png": 60,
            "receipt_50_2026-10-20.png": 4 * 86400,
            "monthly_2026-08.png": 61 * 86400,
            "monthly_2026-10.png": 60,
        }
        for name, age in ages.items():
            p = os.path.join(tmp, name)
            with open(p, "wb") as f:
                f.write(b"0" * 10)
            os.utime(p, (now - age, now - age))
        self.plugin._cleanup_tmp()
        for name, kept in expect_kept.items():
            self.assertEqual(os.path.isfile(os.path.join(tmp, name)), kept, name)

    # ---------- oil_1003：调价提醒带油价明细 ----------

    async def test_oil1003_remind_single_text_includes_prices(self):
        """单省调价提醒的文字消息：倒计时 + 当前油价明细。"""
        self.cfg["basic"]["change_remind_days"] = 1
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(1)
        ):
            rows = await self.plugin._collect_today(["江苏"])
            await self.plugin._advance_remind(rows)
        plains = [
            p.text
            for _, mc in self.ctx.sent_messages
            for p in mc.chain
            if type(p).__name__.endswith("Plain")
        ]
        text = "\n".join(plains)
        self.assertIn("调价提醒", text)
        self.assertIn("调价窗口", text)
        self.assertIn("92号汽油：8.09 元/升", text)

    async def test_oil1003_remind_multi_prompt_includes_prices(self):
        """多省调价提醒（仅图片）：海报提示词含带省份前缀的价格行。"""
        self.cfg["basic"]["change_remind_days"] = 1
        prompts = []

        def fake(prompt, endpoints, model=None, size=None, out_dir=None, name="g.png"):
            prompts.append(prompt)
            return _fake_genimage_generate(prompt, endpoints, model, size, out_dir, name)

        with (
            mock.patch.object(
                plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(1)
            ),
            mock.patch.object(plugin_main.genimage, "generate", fake),
        ):
            rows = await self.plugin._collect_today(["江苏", "上海"])
            await self.plugin._advance_remind(rows)
        self.assertEqual(len(prompts), 1)
        self.assertIn("调价提醒", prompts[0])
        self.assertIn("江苏 92号汽油：", prompts[0])
        self.assertIn("上海 92号汽油：", prompts[0])
        # 文字消息与海报同源：多省预告只发图，原文进「播报原文（核对用）」段
        self.assertIn("【播报原文（核对用）】", prompts[0])

    async def test_a07_look_hit(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.look_cmd(ev, "广东", "92"))
        self.assertEqual(out[0]["type"], "chain")

    async def test_a08_look_miss(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.look_cmd(ev, "广东", "101"))
        self.assertIn("没有查到", out[0]["text"])

    async def test_a09_refuel_ok(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.refuel_cmd(ev, "30", "广东", "95"))
        self.assertEqual(out[0]["type"], "chain")

    async def test_a10_refuel_bad_liters(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.refuel_cmd(ev, "abc"))
        self.assertIn("数字", out[0]["text"])

    async def test_a11_settings_cmd_updates_config(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.settings_cmd(ev, "广东", "95"))
        self.assertEqual(self.cfg["basic"]["provinces"], ["广东"])
        self.assertEqual(self.cfg["basic"]["default_fuels"], ["95"])
        self.assertIn("已记住", out[0]["text"])

    async def test_a12_settings_usage(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.settings_cmd(ev, "", ""))
        self.assertIn("用法", out[0]["text"])

    async def test_a13_adjust_window_hit(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.adjust_window_cmd(ev))
        self.assertTrue(out and ("调价窗口" in out[0]["text"] or "下次" in out[0]["text"]))

    async def test_a14_adjust_window_no_data(self):
        ev = MockEvent()
        with mock.patch.object(
            plugin_main.datasource,
            "fetch_province",
            return_value={"fuels": {}, "next_change_time": ""},
        ):
            out = await run_cmd(self.plugin.adjust_window_cmd(ev))
        self.assertIn("暂无", out[0]["text"])

    async def test_a15_push_test(self):
        ev = MockEvent()
        out = await run_cmd(self.plugin.push_test_cmd(ev))
        self.assertIn("测试推送已执行", out[0]["text"])
        self.assertGreaterEqual(len(self.ctx.sent_messages), 1)

    async def test_quota_exceeded_hint(self):
        self.cfg["basic"]["user_daily_limit"] = 1
        ev = MockEvent()
        await run_cmd(self.plugin.oil_price_cmd(ev, "广东"))
        ev2 = MockEvent()
        out = await run_cmd(self.plugin.oil_price_cmd(ev2, "广东"))
        self.assertIn("上限", out[0]["text"])

    async def test_c20_quota_isolated_by_target_and_user(self):
        self.cfg["basic"]["notify_group"] = [
            "default:GroupMessage:10001",
            "default:GroupMessage:20002",
        ]
        self.cfg["basic"]["target_limits"] = {
            "default:GroupMessage:10001": 1,
            "default:GroupMessage:20002": 2,
        }
        ev_g1 = MockEvent(sender="u1", umo="default:GroupMessage:10001")
        self.assertTrue(self.plugin._quota_guard(ev_g1)[0])
        self.assertFalse(self.plugin._quota_guard(ev_g1)[0])
        ev_g2 = MockEvent(sender="u1", umo="default:GroupMessage:20002")
        self.assertTrue(self.plugin._quota_guard(ev_g2)[0])
        self.assertTrue(self.plugin._quota_guard(ev_g2)[0])
        self.assertFalse(self.plugin._quota_guard(ev_g2)[0])
        ev_other_user = MockEvent(sender="u2", umo="default:GroupMessage:10001")
        self.assertTrue(self.plugin._quota_guard(ev_other_user)[0])

    async def test_fuel_filter_affects_commands(self):
        self.cfg["basic"]["default_fuels"] = ["92"]
        self.assertEqual(self.plugin.prefs.main_fuel(), "92")
        ev = MockEvent()
        out = await run_cmd(self.plugin.look_cmd(ev, "", ""))  # 缺省 → 92 号
        self.assertEqual(out[0]["type"], "chain")

    # ---------- 门控（TC-H） ----------
    async def test_h01_no_appkey_blocks_commands(self):
        pl, ctx, _ = make_plugin(name=self.id().split(".")[-1] + "nokey", appkey="")
        ev = MockEvent()
        for cmd, args in (
            (pl.oil_price_cmd, ("",)),
            (pl.today_cmd, ("",)),
            (pl.look_cmd, ("", "")),
            (pl.refuel_cmd, ("30",)),
            (pl.adjust_window_cmd, ()),
            (pl.push_test_cmd, ()),
        ):
            ev = MockEvent()
            out = await run_cmd(cmd(ev, *args))
            self.assertEqual(len(out), 1, str(cmd))
            self.assertTrue("appKey" in out[0]["text"], str(cmd))

    async def test_h02_no_appkey_push_skipped(self):
        pl, ctx, _ = make_plugin(name=self.id().split(".")[-1] + "nk", appkey="")
        await pl._daily_push()
        self.assertEqual(len(ctx.sent_messages), 0)

    # ---------- 推送（TC-B） ----------
    async def test_b01_daily_push_single_two_parts(self):
        self.cfg["basic"]["provinces"] = ["江苏"]
        await self.plugin._daily_push()
        self.assertEqual(len(self.ctx.sent_messages), 2)
        first = self.ctx.sent_messages[0][1].chain
        second = self.ctx.sent_messages[1][1].chain
        self.assertTrue(any(type(p).__name__.endswith("Plain") for p in first))
        self.assertTrue(any(type(p).__name__.endswith("Image") for p in second))

    async def test_b02_daily_push_multi_image_only(self):
        await self.plugin._daily_push()
        self.assertEqual(len(self.ctx.sent_messages), 1)
        parts = self.ctx.sent_messages[0][1].chain
        self.assertTrue(any(type(p).__name__.endswith("Image") for p in parts))
        self.assertFalse(any(type(p).__name__.endswith("Plain") for p in parts))

    async def test_b03_push_render_fail_falls_back_to_text(self):
        self.cfg["basic"]["provinces"] = ["江苏"]
        with mock.patch.object(
            plugin_main.genimage, "generate", side_effect=RuntimeError("boom")
        ):
            await self.plugin._daily_push()
        self.assertEqual(len(self.ctx.sent_messages), 1)
        parts = self.ctx.sent_messages[0][1].chain
        self.assertTrue(any(type(p).__name__.endswith("Plain") for p in parts))

    async def test_b04_all_fetch_fail_no_push(self):
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=RuntimeError("down")
        ):
            await self.plugin._daily_push()
        self.assertEqual(len(self.ctx.sent_messages), 0)

    async def test_b05_monthly_push_with_data(self):
        now = datetime.now()
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        d = datetime(year, month, 1).date()
        while d.month == month:
            self.plugin.store.store_prices(
                "江苏",
                {
                    "fuels": {
                        "92": {
                            "price": 8.0 + d.day * 0.005,
                            "change": 0.0,
                            "pct": None,
                            "before": 8.0,
                        }
                    },
                    "before_change_time": "",
                    "next_change_time": "",
                },
            )
            h = self.plugin.store.history()
            h["江苏"][d.isoformat()] = h["江苏"].pop(self.plugin.store.today())
            self.plugin.store._save(self.plugin.store.history_file, h)
            d += timedelta(days=1)
        await self.plugin._monthly_push()
        self.assertEqual(len(self.ctx.sent_messages), 1)
        parts = self.ctx.sent_messages[0][1].chain
        self.assertTrue(any(type(p).__name__.endswith("Image") for p in parts))

    async def test_b06_monthly_push_without_data(self):
        await self.plugin._monthly_push()
        self.assertEqual(len(self.ctx.sent_messages), 0)

    async def test_b07_partial_target_failure(self):
        orig = self.ctx.send_message

        async def flaky(umo, mc):
            if "10001" in umo:
                raise RuntimeError("群机器人掉线")
            await orig(umo, mc)

        self.ctx.send_message = flaky
        self.cfg["basic"]["provinces"] = ["江苏"]
        self.cfg["basic"]["notify_private"] = ["default:FriendMessage:123456001"]
        await self.plugin._daily_push()
        self.assertGreaterEqual(len(self.ctx.sent_messages), 2)

    async def test_b10_image_disabled_text_only(self):
        self.cfg["api"]["gen_enable"] = False
        self.cfg["basic"]["provinces"] = ["江苏"]
        await self.plugin._daily_push()
        self.assertEqual(len(self.ctx.sent_messages), 1)
        parts = self.ctx.sent_messages[0][1].chain
        self.assertTrue(any(type(p).__name__.endswith("Plain") for p in parts))
        self.assertFalse(any(type(p).__name__.endswith("Image") for p in parts))

    async def test_b11_off_mode_baseline_then_change_push(self):
        """F1/F2：off + 调价提醒 = 原 on_change 行为——首次仅记基线，价格变动推送。"""
        self.cfg["basic"]["push_frequency"] = "off"
        self.cfg["basic"]["change_remind_days"] = 1
        self.cfg["basic"]["provinces"] = ["江苏"]
        price = {"v": 8.09}
        far_target = (datetime.now().date() + timedelta(days=9)).strftime("%Y%m%d")

        def fake(prov, key):
            d = fake_price_data(price["v"])
            d["next_change_time"] = far_target
            return d

        with mock.patch.object(plugin_main.datasource, "fetch_province", side_effect=fake):
            await self.plugin._push_round(datetime.now())  # 首次：仅记录基线
            self.assertEqual(len(self.ctx.sent_messages), 0)
            price["v"] = 8.30  # 价格变动（调价落地）
            h = self.plugin.store.history()  # 清当日缓存强制重新抓取
            h["江苏"].pop(self.plugin.store.today(), None)
            self.plugin.store._save(self.plugin.store.history_file, h)
            await self.plugin._push_round(datetime.now())  # 有变动：推送
            self.assertGreaterEqual(len(self.ctx.sent_messages), 1)
            n = len(self.ctx.sent_messages)
            await self.plugin._push_round(datetime.now())  # 价格未再变：不重复
            self.assertEqual(len(self.ctx.sent_messages), n)

    async def test_b12_advance_remind_gating_and_daily_dedup(self):
        """F1/F6：预告按「剩余 ≤ N 天」门控（修复旧版任意天数都发），且每日一条。"""
        self.cfg["basic"]["change_remind_days"] = 1
        self.cfg["basic"]["provinces"] = ["江苏"]
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(4)
        ):
            rows = await self.plugin._collect_today(["江苏"])
            await self.plugin._advance_remind(rows)  # 剩余 4 天 > N=1：不发
            self.assertEqual(len(self.ctx.sent_messages), 0)
        self.cfg["basic"]["change_remind_days"] = 3
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(4)
        ):
            await self.plugin._advance_remind(rows)  # 剩余 4 天 > N=3：仍不发
            self.assertEqual(len(self.ctx.sent_messages), 0)
        self.cfg["basic"]["change_remind_days"] = 1
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(1)
        ):
            h = self.plugin.store.history()  # 清当日缓存，拿到新窗口日期
            h["江苏"].pop(self.plugin.store.today(), None)
            self.plugin.store._save(self.plugin.store.history_file, h)
            rows = await self.plugin._collect_today(["江苏"])
            await self.plugin._advance_remind(rows)  # 剩余 1 天 ≤ N：单省=文字+图片
            self.assertGreaterEqual(len(self.ctx.sent_messages), 2)
            n = len(self.ctx.sent_messages)
            await self.plugin._advance_remind(rows)  # 同日去重
            self.assertEqual(len(self.ctx.sent_messages), n)

    async def test_b09_daily_loop_dedup(self):
        calls = []
        now = datetime.now()
        self.cfg["basic"]["push_hour"] = now.hour
        self.cfg["basic"]["push_minute"] = now.minute
        self.plugin._pushed_date = None

        async def fake_round(now_dt):
            calls.append(1)

        async def boom(seconds):
            raise asyncio.CancelledError()

        with (
            mock.patch.object(self.plugin, "_push_round", fake_round),
            mock.patch.object(plugin_main.asyncio, "sleep", boom),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._daily_loop()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.plugin._pushed_date, now.strftime("%Y-%m-%d"))
        calls.clear()

        with (
            mock.patch.object(self.plugin, "_push_round", fake_round),
            mock.patch.object(plugin_main.asyncio, "sleep", boom),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._daily_loop()
        self.assertEqual(len(calls), 0)  # 同日去重

    # ---------- oil_1001：推送轮矩阵（频率 × 调价提醒） ----------

    async def test_oil1001_matrix_freq_x_remind(self):
        """多省场景消息数矩阵：daily/weekly/off × 提醒开关 × 窗口距离。

        多省形态：常规卡片=1 图；调价预告=1 图；off 且窗口远=0（静默）。
        """
        cases = [
            # (freq, remind_days, window_days, expect_msgs)
            ("daily", 0, 5, 1),  # 仅常规卡片
            ("daily", 1, 5, 1),  # 窗口远：无预告
            ("daily", 1, 1, 2),  # 窗口明天：卡片 + 独立预告（多省卡片无倒计时）
            ("daily", 3, 3, 2),  # 剩余 3 ≤ N=3：卡片 + 预告
            ("weekly", 0, 1, 0),  # 非每周推送日：静默
            ("weekly", 1, 1, 1),  # 非推送日但调价提醒独立生效
            ("off", 0, 1, 0),  # 全关：完全静默
            ("off", 1, 5, 0),  # off 且窗口远：静默（复刻 on_change 平日行为）
            ("off", 1, 1, 1),  # off + 窗口明天：仅预告
        ]
        for freq, remind, window, expect in cases:
            with self.subTest(freq=freq, remind=remind, window=window):
                pl, ctx, cfg = make_plugin(name=f"mx_{freq}_{remind}_{window}")
                try:
                    cfg["basic"]["push_frequency"] = freq
                    cfg["basic"]["change_remind_days"] = remind
                    if freq == "weekly":
                        cfg["basic"]["push_weekday"] = (datetime.now().weekday() + 1) % 7
                    with (
                        mock.patch.object(
                            plugin_main.datasource,
                            "fetch_province",
                            side_effect=fetch_with_window(window),
                        ),
                        mock.patch.object(
                            plugin_main.genimage, "generate", _fake_genimage_generate
                        ),
                    ):
                        await pl._push_round(datetime.now())
                    self.assertEqual(len(ctx.sent_messages), expect)
                finally:
                    for t in pl._tasks:
                        t.cancel()

    async def test_oil1001_single_prov_countdown_dedup(self):
        """F3：单省常规卡片自带倒计时行，当天预告不重发（daily+提醒=仅 2 条）。"""
        self.cfg["basic"]["push_frequency"] = "daily"
        self.cfg["basic"]["change_remind_days"] = 1
        self.cfg["basic"]["provinces"] = ["江苏"]
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(1)
        ):
            await self.plugin._push_round(datetime.now())
        # 单省卡片 = 文字 + 图片两条，预告被去重
        self.assertEqual(len(self.ctx.sent_messages), 2)

    async def test_oil1001_off_snapshot_first_baseline_no_push(self):
        """F1：快照首次仅记基线不推送（升级/新省份集不轰炸）。"""
        self.cfg["basic"]["push_frequency"] = "off"
        self.cfg["basic"]["change_remind_days"] = 1
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(9)
        ):
            await self.plugin._push_round(datetime.now())
        self.assertEqual(len(self.ctx.sent_messages), 0)

    # ---------- oil_1001：省份自适应（F7~F9） ----------

    async def test_oil1001_sets_grouping_two_sets(self):
        """两个目标偏好不同 → 两个省份集，各自生成一次（生图 2 次、互不影响）。"""
        self.cfg["basic"]["auto_provinces"] = True
        self.cfg["basic"]["notify_group"] = [
            "default:GroupMessage:10001",
            "default:GroupMessage:20002",
        ]
        for _ in range(3):
            self.plugin.store.record_query("default:GroupMessage:10001", "u1", ["广东"])
            self.plugin.store.record_query("default:GroupMessage:20002", "u2", ["四川"])
        gen_calls = []

        def fake_gen(
            prompt, endpoints, model=None, size=None, out_dir="/tmp", name="g.png"
        ):
            gen_calls.append(name)
            return _fake_genimage_generate(prompt, endpoints, model, size, out_dir, name)

        with (
            mock.patch.object(
                plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(9)
            ),
            mock.patch.object(plugin_main.genimage, "generate", fake_gen),
        ):
            await self.plugin._push_round(datetime.now())
        self.assertEqual(len(gen_calls), 2)  # 两个省份集各生成一次
        self.assertTrue(any("广东" in n for n in gen_calls))
        self.assertTrue(any("四川" in n for n in gen_calls))
        # 每目标单省=文字+图片两条
        got = {umo for umo, _ in self.ctx.sent_messages}
        self.assertEqual(got, {"default:GroupMessage:10001", "default:GroupMessage:20002"})
        self.assertEqual(len(self.ctx.sent_messages), 4)

    async def test_oil1001_same_set_shared_generation(self):
        """省份集相同的目标共享一次生成（生图 1 次，两目标都收到）。"""
        self.cfg["basic"]["auto_provinces"] = True
        self.cfg["basic"]["notify_group"] = [
            "default:GroupMessage:10001",
            "default:GroupMessage:20002",
        ]
        for _ in range(3):
            self.plugin.store.record_query("default:GroupMessage:10001", "u1", ["广东"])
            self.plugin.store.record_query("default:GroupMessage:20002", "u2", ["广东"])
        gen_calls = []

        def fake_gen(
            prompt, endpoints, model=None, size=None, out_dir="/tmp", name="g.png"
        ):
            gen_calls.append(name)
            return _fake_genimage_generate(prompt, endpoints, model, size, out_dir, name)

        with (
            mock.patch.object(
                plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(9)
            ),
            mock.patch.object(plugin_main.genimage, "generate", fake_gen),
        ):
            await self.plugin._push_round(datetime.now())
        self.assertEqual(len(gen_calls), 1)
        self.assertEqual(len(self.ctx.sent_messages), 4)

    async def test_oil1001_fallback_to_configured(self):
        """F8：auto_provinces 开但记录不足 → 回退配置省份。"""
        self.cfg["basic"]["auto_provinces"] = True
        self.plugin.store.record_query(
            "default:GroupMessage:10001", "u1", ["广东"]
        )  # 仅 1 条
        provs = self.plugin._resolve_provinces_for("default:GroupMessage:10001")
        self.assertEqual(provs, self.plugin.prefs.provinces())  # ["江苏", "上海"]

    async def test_oil1001_bare_cmd_uses_preferred_province(self):
        """F9：裸 /油价 缺省省份按会话近期查询偏好。"""
        self.cfg["basic"]["auto_provinces"] = True
        for _ in range(3):
            self.plugin.store.record_query("default:GroupMessage:10001", "u1", ["广东"])
        ev = MockEvent()
        out = await run_cmd(self.plugin.oil_price_cmd(ev, ""))
        self.assertIn("【广东】", out[0]["text"])  # 缺省按偏好单省
        self.assertNotIn("【上海】", out[0]["text"])

    async def test_oil1001_command_records_query_event(self):
        """F7：主动查询成功后记录事件（推送拉取不计）。"""
        ev = MockEvent()
        await run_cmd(self.plugin.oil_price_cmd(ev, "广东"))
        events = self.plugin.store.query_events("default:GroupMessage:10001")
        self.assertTrue(events)
        self.assertEqual(events[-1]["p"], ["广东"])
        self.assertEqual(events[-1]["u"], "tester")

    async def test_oil1001_state_pruned_to_current_sets(self):
        """F9：快照/预告去重状态只保留当前省份集。"""
        self.cfg["basic"]["change_remind_days"] = 1
        st = self.plugin._load_push_state()
        st["price_snaps"] = {"旧省集": {"江苏": {"92": 8.0}}, "江苏、上海": {}}
        st["remind_done"] = {"旧省集": "2026-10-01"}
        self.plugin._save_push_state(st)
        with mock.patch.object(
            plugin_main.datasource, "fetch_province", side_effect=fetch_with_window(1)
        ):
            await self.plugin._push_round(
                datetime.now()
            )  # 预告触发，remind_done 写入当前集
        st2 = self.plugin._load_push_state()
        self.assertEqual(set(st2.get("price_snaps")), {"江苏、上海"})
        self.assertEqual(set(st2.get("remind_done")), {"江苏、上海"})

    # ---------- Web API（TC-D/C16/H03） ----------
    async def test_d01_config_get_payload(self):
        with mock.patch.object(plugin_main, "request", FakeRequest(None)):
            resp = await self.plugin._web_config_get()
        data = json.loads(resp.body)
        for k in (
            "provinces",
            "push_hour",
            "push_minute",
            "push_frequency",
            "change_remind_days",
            "auto_provinces",
            "monthly_push",
            "private_targets",
            "group_targets",
            "user_daily_limit",
            "default_fuels",
            "app_key",
            "app_key_ready",
            "datasource",
            "register_url",
            "province_options",
            "fuel_options",
        ):
            self.assertIn(k, data)
        self.assertEqual(data["province_options"], PROVINCES)
        self.assertTrue(data["app_key_ready"])

    async def test_d02_stats_render_with_data(self):
        seed_history(self.plugin.store, "江苏", days=5)
        with mock.patch.object(
            plugin_main,
            "request",
            FakeRequest({"provinces": ["江苏"], "fuels": ["92"], "days": 30}),
        ):
            resp = await self.plugin._web_stats_render()
        data = json.loads(resp.body)
        self.assertTrue(data["image"].startswith("data:image/png;base64,"))
        raw = base64.b64decode(data["image"].split(",", 1)[1])
        self.assertTrue(raw[:8] == b"\x89PNG\r\n\x1a\n")
        self.assertGreaterEqual(len(data["summary"]), 1)

    async def test_d03_stats_render_no_data(self):
        with mock.patch.object(
            plugin_main,
            "request",
            FakeRequest({"provinces": ["西藏"], "fuels": ["92"], "days": 30}),
        ):
            resp = await self.plugin._web_stats_render()
        data = json.loads(resp.body)
        self.assertEqual(data["image"], "")
        self.assertIn("暂无历史数据", data["message"])

    async def test_d04_stats_render_no_appkey(self):
        pl, ctx, _ = make_plugin(name=self.id().split(".")[-1], appkey="")
        with mock.patch.object(plugin_main, "request", FakeRequest({})):
            resp = await pl._web_stats_render()
        self.assertEqual(resp.status_code, 400)

    # ---------- oil_1004：查询省份记忆管理 API ----------

    async def test_oil1004_prefs_list_and_label(self):
        self.plugin.store.record_query("default:GroupMessage:10001", "u1", ["广东"])
        self.plugin.store.record_query("default:FriendMessage:769411680", "u2", ["四川"])
        self.plugin.store.record_query("raw:umo:9", "u3", ["浙江"])
        with mock.patch.object(plugin_main, "request", FakeRequest(None)):
            resp = await self.plugin._web_prefs_list()
        data = json.loads(resp.body)
        self.assertIn("auto_provinces", data)
        labels = {t["label"]: t for t in data["targets"]}
        self.assertIn("群聊 10001", labels)
        self.assertIn("私聊 769411680", labels)
        self.assertIn("raw:umo:9", labels)  # 无法识别的 UMO 原样展示
        g = labels["群聊 10001"]
        self.assertEqual(len(g["events"]), 1)
        self.assertTrue(g["preferred_from_prefs"])  # 事件不足 → 回退配置省

    async def test_oil1004_prefs_manage_roundtrip(self):
        umo = "default:GroupMessage:10001"
        for _ in range(3):
            self.plugin.store.record_query(umo, "u1", ["江苏"])
        ev = self.plugin.store.query_events(umo)[0]

        # update：非法/空省份被拒；合法更新生效
        with mock.patch.object(
            plugin_main,
            "request",
            FakeRequest(
                {"action": "update", "umo": umo, "event": ev, "provinces": ["火星"]}
            ),
        ):
            resp = await self.plugin._web_prefs_manage()
        self.assertEqual(resp.status_code, 400)
        with mock.patch.object(
            plugin_main,
            "request",
            FakeRequest(
                {"action": "update", "umo": umo, "event": ev, "provinces": ["广东", "四川"]}
            ),
        ):
            resp = await self.plugin._web_prefs_manage()
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])
        self.assertEqual(self.plugin.store.query_events(umo)[0]["p"], ["广东", "四川"])

        # delete：事件不匹配 → 404
        with mock.patch.object(
            plugin_main,
            "request",
            FakeRequest(
                {"action": "delete", "umo": umo, "event": {"u": "x", "p": [], "t": ""}}
            ),
        ):
            resp = await self.plugin._web_prefs_manage()
        self.assertEqual(resp.status_code, 404)

        # clear：清空后列表为空；重复 clear → ok:False
        with mock.patch.object(
            plugin_main, "request", FakeRequest({"action": "clear", "umo": umo})
        ):
            resp = await self.plugin._web_prefs_manage()
        self.assertTrue(json.loads(resp.body)["ok"])
        self.assertEqual(self.plugin.store.query_events(umo), [])

        # 非法 action / 缺 umo
        with mock.patch.object(
            plugin_main, "request", FakeRequest({"action": "nuke", "umo": umo})
        ):
            resp = await self.plugin._web_prefs_manage()
        self.assertEqual(resp.status_code, 400)
        with mock.patch.object(plugin_main, "request", FakeRequest({"action": "clear"})):
            resp = await self.plugin._web_prefs_manage()
        self.assertEqual(resp.status_code, 400)

    async def test_save_handler_valid_and_persist_called(self):
        cfg = FakeConfig(base_config())
        pl, ctx, _ = make_plugin(name=self.id().split(".")[-1] + "save", config=cfg)
        payload = {
            "provinces": ["广东"],
            "push_hour": 7,
            "push_minute": 15,
            "monthly_push": True,
            "private_targets": [{"id": "123456001", "limit": 9}],
            "group_targets": [{"id": "10001", "limit": 4}],
            "user_daily_limit": 2,
            "default_fuels": ["95"],
            "app_key": "kkk",
        }
        with mock.patch.object(plugin_main, "request", FakeRequest(payload)):
            resp = await pl._web_config_save()
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(cfg.saved, 1)
        self.assertEqual(cfg["basic"]["provinces"], ["广东"])
        self.assertEqual(
            cfg["basic"]["notify_private"], ["default:FriendMessage:123456001"]
        )
        self.assertEqual(
            cfg["basic"]["target_limits"],
            {
                "default:FriendMessage:123456001": 9,
                "default:GroupMessage:10001": 4,
            },
        )
        self.assertTrue(pl._loop_started)  # TC-C16：appKey 就绪自动补启定时器

    async def test_save_handler_invalid_returns_400(self):
        with mock.patch.object(
            plugin_main, "request", FakeRequest({"provinces": [], "push_hour": 25})
        ):
            resp = await self.plugin._web_config_save()
        self.assertEqual(resp.status_code, 400)

    def test_webapi_routes_registered(self):
        routes = [r[0] for r in self.ctx.apis]
        self.assertIn("/astrbot_plugin_oil_price/config/get", routes)
        self.assertIn("/astrbot_plugin_oil_price/config/save", routes)
        self.assertIn("/astrbot_plugin_oil_price/stats/render", routes)
        self.assertIn("/astrbot_plugin_oil_price/prefs/list", routes)
        self.assertIn("/astrbot_plugin_oil_price/prefs/manage", routes)


# ======================= 真实 API 冒烟（可选） =======================


@unittest.skipUnless(
    os.environ.get("OIL_LIVE") == "1",
    "真实冒烟默认跳过（消耗付费配额）；设 OIL_LIVE=1 开启",
)
class TestLiveSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_live_fetch(self):
        data = datasource.fetch_province("江苏", os.environ.get("OIL_APPKEY", ""))
        self.assertIn("92", data["fuels"])


class ReportResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.rows.append((test.id(), "✅ 通过", ""))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.rows.append((test.id(), "❌ 失败", self._exc_info_to_string(err, test)))

    def addError(self, test, err):
        super().addError(test, err)
        self.rows.append((test.id(), "❌ 错误", self._exc_info_to_string(err, test)))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.rows.append((test.id(), "⏭ 跳过", reason))


class ReportRunner(unittest.TextTestRunner):
    resultclass = ReportResult


def write_report(result):
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs = os.path.join(plugin_root, "docs")
    os.makedirs(docs, exist_ok=True)
    path = os.path.join(docs, f"测试报告_v{plugin_main.PLUGIN_VERSION}.md")
    passed = sum(1 for _, status, _ in result.rows if status == "✅ 通过")
    status = "✅ 全部通过" if result.wasSuccessful() else "❌ 存在失败"
    lines = [
        f"# 油价插件 v{plugin_main.PLUGIN_VERSION} 全量测试报告",
        "",
        f"- 执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 总用例：{result.testsRun}",
        f"- 通过：{passed}",
        f"- 失败：{len(result.failures)}",
        f"- 错误：{len(result.errors)}",
        f"- 跳过：{len(result.skipped)}",
        f"- 定版结论：{status}",
        "",
        "| 用例 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for case, case_status, detail in result.rows:
        note = detail.splitlines()[-1] if detail else ""
        lines.append(f"| {case} | {case_status} | {note.replace('|', '/')} |")
    failures = [(c, d) for c, st, d in result.rows if st in ("❌ 失败", "❌ 错误")]
    if failures:
        lines += ["", "## 失败详情"]
        for case, detail in failures:
            lines += ["", f"### {case}", "~~~text", detail, "~~~"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n测试报告：{path}")
    return path


if __name__ == "__main__":
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    os.makedirs(TEST_ROOT, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules["__main__"])
    result = ReportRunner(verbosity=2).run(suite)
    write_report(result)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
