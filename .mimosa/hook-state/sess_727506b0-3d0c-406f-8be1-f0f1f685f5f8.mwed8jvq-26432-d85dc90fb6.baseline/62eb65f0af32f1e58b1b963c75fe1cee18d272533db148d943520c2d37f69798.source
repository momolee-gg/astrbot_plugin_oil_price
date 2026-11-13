"""astrbot.api.message_components 桩：Plain / Image 组件。"""


class Plain:
    def __init__(self, text=""):
        self.text = text


class Image:
    def __init__(self, path=""):
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)
