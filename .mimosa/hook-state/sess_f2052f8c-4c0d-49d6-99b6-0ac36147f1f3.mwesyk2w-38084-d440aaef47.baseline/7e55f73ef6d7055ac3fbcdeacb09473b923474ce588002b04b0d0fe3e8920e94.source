"""本地存储：查询历史（按省按日）、用户每日配额、查询偏好事件、见过的会话。仅依赖标准库。"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


class Store:
    def __init__(
        self,
        history_file: str,
        quota_file: str,
        events_file: str = "",
        sessions_file: str = "",
    ):
        self.history_file = history_file
        self.quota_file = quota_file
        self.events_file = events_file
        self.sessions_file = sessions_file

    # ---------- 基础 ----------

    @staticmethod
    def _load(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _save(path: str, data: dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 查询历史 ----------

    def history(self) -> dict:
        return self._load(self.history_file)

    @staticmethod
    def today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def store_prices(self, prov: str, data: dict):
        date = self.today()
        h = self.history()
        h.setdefault(prov, {})[date] = {
            "fuels": data.get("fuels") or {},
            "before_change_time": data.get("before_change_time") or "",
            "next_change_time": data.get("next_change_time") or "",
        }
        self._save(self.history_file, h)

    def get_day(self, prov: str, date: str):
        return self.history().get(prov, {}).get(date)

    def get_prev(self, prov: str, within_days: int = 8):
        """该省最近一次历史记录（不含今天），涨跌对比兜底用。"""
        today = datetime.now().date()
        h = self.history().get(prov, {})
        for i in range(1, within_days):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            if d in h:
                return h[d]
        return None

    def trend_series(self, prov: str, days: int = 30) -> list:
        """按日期升序 [(date, fuels)]，最多 days 天。"""
        h = self.history().get(prov, {})
        today = datetime.now().date()
        out = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            e = h.get(d)
            if e and e.get("fuels"):
                out.append((d, e["fuels"]))
        return out

    def month_series(self, prov: str, year: int, month: int) -> list:
        """指定自然月的 [(date, fuels)]，按日期升序。"""
        h = self.history().get(prov, {})
        out = []
        d = datetime(year, month, 1).date()
        while d.month == month:
            key = d.strftime("%Y-%m-%d")
            e = h.get(key)
            if e and e.get("fuels"):
                out.append((key, e["fuels"]))
            d += timedelta(days=1)
        return out

    # ---------- 用户每日配额 ----------

    def quota_consume(self, sender_id: str, limit: int) -> tuple:
        """扣减一次查询配额。返回 (是否允许, 已用次数, 上限)；limit<=0 不限。"""
        if limit <= 0:
            return True, 0, 0
        if not sender_id:
            return True, 0, limit
        quota = self._load(self.quota_file)
        today = self.today()
        rec = quota.get(sender_id) or {}
        if rec.get("date") != today:
            rec = {"date": today, "count": 0}
        used = int(rec.get("count", 0))
        if used >= limit:
            return False, used, limit
        rec["count"] = used + 1
        rec["date"] = today
        quota[sender_id] = rec
        self._save(self.quota_file, quota)
        return True, used + 1, limit

    # ---------- 查询偏好事件（oil_1001 优化点二） ----------

    def record_query(self, umo: str, sender: str, provinces: list, max_keep: int = 50):
        """记录一次成功的主动查询事件，按目标滚动保留最近 max_keep 条（F7）。"""
        if not self.events_file or not umo or not provinces:
            return
        data = self._load(self.events_file)
        events = data.get(umo) or []
        events.append(
            {
                "u": str(sender or ""),
                "p": [str(p) for p in provinces],
                "t": datetime.now().isoformat(timespec="seconds"),
            }
        )
        data[umo] = events[-max_keep:]
        self._save(self.events_file, data)

    def query_events(self, umo: str) -> list:
        """该目标的查询事件（按时间升序，旧→新）。"""
        if not self.events_file:
            return []
        return (self._load(self.events_file).get(umo)) or []

    # ---------- 查询偏好记忆管理（oil_1004：WebUI 查看/修改/删除） ----------

    def all_query_events(self) -> dict:
        """全部查询事件原始数据 {umo: [event...]}（展示用）。"""
        if not self.events_file:
            return {}
        return self._load(self.events_file)

    @staticmethod
    def _match_event(ev: dict, want: dict) -> bool:
        """事件按 (用户, 省份集, 时间) 精确匹配——内容相同的多条同改同删，无副作用。"""
        return (
            str((ev or {}).get("u") or "") == str((want or {}).get("u") or "")
            and [str(p) for p in (ev or {}).get("p") or []]
            == [str(p) for p in (want or {}).get("p") or []]
            and str((ev or {}).get("t") or "") == str((want or {}).get("t") or "")
        )

    def update_query_event(self, umo: str, want: dict, provs: list) -> bool:
        """把匹配的事件省份集改为 provs；命中并修改返回 True。"""
        if not self.events_file or not umo or not provs:
            return False
        data = self._load(self.events_file)
        events = data.get(umo) or []
        hit = False
        for ev in events:
            if self._match_event(ev, want):
                ev["p"] = [str(p) for p in provs]
                hit = True
        if hit:
            self._save(self.events_file, data)
        return hit

    def delete_query_event(self, umo: str, want: dict) -> bool:
        """删除匹配的查询事件；命中返回 True。"""
        if not self.events_file or not umo:
            return False
        data = self._load(self.events_file)
        events = data.get(umo) or []
        rest = [ev for ev in events if not self._match_event(ev, want)]
        if len(rest) == len(events):
            return False
        if rest:
            data[umo] = rest
        else:
            data.pop(umo, None)
        self._save(self.events_file, data)
        return True

    def clear_query_events(self, umo: str) -> bool:
        """清空某目标的全部查询事件（下次推送回退配置省份）。"""
        if not self.events_file or not umo:
            return False
        data = self._load(self.events_file)
        if umo not in data:
            return False
        data.pop(umo)
        self._save(self.events_file, data)
        return True

    # ---------- 见过的会话记录（oil_1010：设置页「从记录会话选择」数据源） ----------

    @staticmethod
    def _session_kind(umo: str) -> str:
        """从 UMO 中段解析群/私类型（主流适配器均为 GroupMessage/FriendMessage）。"""
        if ":GroupMessage:" in umo:
            return "group"
        if ":FriendMessage:" in umo:
            return "private"
        return ""

    def record_seen_session(self, umo: str, label: str, max_keep: int = 200):
        """记录一条见过的会话（UMO + 最近发言者昵称），滚动保留 max_keep 条。

        由主循环低频合并落盘（消息处理器只进内存队列），高频消息不产生逐条写盘。
        """
        if not self.sessions_file or not umo:
            return
        data = self._load(self.sessions_file)
        sessions = data.get("sessions") or {}
        prev = sessions.get(umo) or {}
        sessions[umo] = {
            "label": str(label or prev.get("label") or ""),
            "kind": self._session_kind(umo),
            "first": str(prev.get("first") or datetime.now().isoformat(timespec="seconds")),
            "last": datetime.now().isoformat(timespec="seconds"),
        }
        if len(sessions) > max_keep:
            oldest = sorted(sessions.items(), key=lambda kv: str(kv[1].get("last") or ""))
            for old_umo, _ in oldest[: len(sessions) - max_keep]:
                sessions.pop(old_umo, None)
        data["sessions"] = sessions
        self._save(self.sessions_file, data)

    def all_seen_sessions(self) -> list:
        """见过的会话列表（最近活跃在前）；bot=UMO 首段（适配器实例名）。"""
        if not self.sessions_file:
            return []
        data = self._load(self.sessions_file)
        sessions = data.get("sessions") or {}
        out = [
            {
                "umo": umo,
                "bot": umo.split(":", 1)[0] if ":" in umo else "",
                "label": str(rec.get("label") or ""),
                "kind": str(rec.get("kind") or ""),
                "first": str(rec.get("first") or ""),
                "last": str(rec.get("last") or ""),
            }
            for umo, rec in sessions.items()
        ]
        out.sort(key=lambda s: s["last"], reverse=True)
        return out


def pick_preferred_provinces(
    events: list, window: int = 20, top: int = 3, min_events: int = 3
):
    """从查询事件选取近期偏好省份（F8，D8/D9 决策）。

    - 事件不足 min_events 条返回 None，由调用方回退配置省份（冷启动兜底）；
    - 只看最近 window 条；按「成员 × 省份」去重各保留最近一次（群聊防单人刷屏
      绑架全群，私聊即本人每省最近一次），再按最近出现倒序取前 top 个。
    events 为升序列表（越靠后越新），元素形如 {"u": 用户, "p": [省份...]}。
    """
    if len(events) < min_events:
        return None
    latest = {}
    for i, ev in enumerate(events[-window:]):
        user = str((ev or {}).get("u") or "")
        for prov in (ev or {}).get("p") or []:
            latest[(user, str(prov))] = i
    prov_latest = {}
    for (_user, prov), i in latest.items():
        prov_latest[prov] = max(prov_latest.get(prov, -1), i)
    ranked = sorted(prov_latest, key=lambda p: prov_latest[p], reverse=True)
    return ranked[:top]
