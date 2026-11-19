"""astrbot.api.event.filter 桩：事件/权限过滤器装饰器与类型枚举。

与线上 AstrBot 对齐：`filter` 以「模块」形态从 `astrbot.api.event` 导出，
EventMessageType / PermissionType 均定义在 filter 模块上（服务器 4.29.4 实测）。
"""

from enum import IntFlag


class EventMessageType(IntFlag):
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    OTHER_MESSAGE = 4


class PermissionType:
    ADMIN = "admin"
    MEMBER = "member"


def command(*args, **kwargs):
    def deco(fn):
        return fn

    return deco


def llm_tool(*args, **kwargs):
    def deco(fn):
        return fn

    return deco


def event_message_type(*args, **kwargs):
    def deco(fn):
        return fn

    return deco


def permission_type(*args, **kwargs):
    def deco(fn):
        return fn

    return deco
