"""AstrBotConfig 桩：dict + schema 默认值合并 + 落盘。"""

import json
import os


class AstrBotConfig(dict):
    def __init__(self, config_path=None, schema=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_path = config_path
        if config_path and os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.update(data)
        for key, item in (schema or {}).items():
            if item.get("type") == "object":
                sec = dict(self.get(key) or {})
                for k, it in (item.get("items") or {}).items():
                    sec.setdefault(k, it.get("default"))
                self[key] = sec

    def save_config(self, indent=None):
        if not self.config_path:
            return
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(dict(self), f, ensure_ascii=False, indent=indent or 4)
