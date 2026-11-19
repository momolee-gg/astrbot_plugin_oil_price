"""astrbot.api.event 桩：事件类 / filter 子模块 / MessageChain。

与线上 AstrBot 对齐：本包不导出 EventMessageType（需从
`astrbot.api.event.filter` 导入），`filter` 以子模块形态提供。
"""

from . import filter
from .filter import EventMessageType, PermissionType

__all__ = [
    "AstrMessageEvent",
    "MessageChain",
    "filter",
    "EventMessageType",
    "PermissionType",
]


class AstrMessageEvent:
    pass


class MessageChain:
    def __init__(self):
        self.chain = []
