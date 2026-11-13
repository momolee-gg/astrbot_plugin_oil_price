"""astrbot.api.event 桩：事件类 / filter 装饰器 / MessageChain。"""


class AstrMessageEvent:
    pass


class EventMessageType:
    ALL = "all"
    PRIVATE_MESSAGE = "private"
    GROUP_MESSAGE = "group"


class PermissionType:
    ADMIN = "admin"
    MEMBER = "member"


class _Filter:
    def command(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def llm_tool(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def event_message_type(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def permission_type(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco


filter = _Filter()
filter.PermissionType = PermissionType


class MessageChain:
    def __init__(self):
        self.chain = []
