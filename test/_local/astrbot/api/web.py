"""astrbot.api.web 桩：json/error 响应与 request。"""

import json as _json


class Resp:
    def __init__(self, body: bytes, status_code: int = 200):
        self.body = body
        self.status_code = status_code


def json_response(data, status_code: int = 200):
    return Resp(_json.dumps(data, ensure_ascii=False).encode("utf-8"), status_code)


def error_response(msg, status_code: int = 400):
    return Resp(_json.dumps({"error": msg}, ensure_ascii=False).encode(), status_code)


class request:
    @staticmethod
    async def json(default=None):
        return default
