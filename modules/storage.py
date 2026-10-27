"""本地存储：查询历史（按省按日）、用户每日配额、查询偏好事件。仅依赖标准库。"""

import json
import os
from datetime import datetime, timedelta


class Store:
    def __init__(self, history_file: str, quota_file: str, events_file: str = ""):
        self.history_file = history_file
        self.quota_file = quota_file
        self.events_file = events_file

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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
