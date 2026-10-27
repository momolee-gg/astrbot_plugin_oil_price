"""astrbot.api.event 桩：事件类 / filter 装饰器 / MessageChain。"""


class AstrMessageEvent:
    pass


class _Filter:
    def command(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def llm_tool(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco


filter = _Filter()


class MessageChain:
    def __init__(self):
        self.chain = []
