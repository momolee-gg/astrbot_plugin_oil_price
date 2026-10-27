"""astrbot.api.star 桩：Context / Star / register。"""


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


def register(*args, **kwargs):
    def deco(cls):
        return cls

    return deco
