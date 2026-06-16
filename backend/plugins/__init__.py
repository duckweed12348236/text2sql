from abc import ABCMeta, abstractmethod, ABC

from metaclasses import SingletonMeta


class Singleton(metaclass=SingletonMeta):
    pass


class PluginMeta(SingletonMeta, ABCMeta):
    plugin_classes = []

    def __new__(cls, name, bases, attrs):
        sub_cls = super().__new__(cls, name, bases, attrs)
        if name != "Plugin":
            cls.plugin_classes.append(sub_cls)
        return sub_cls


class Plugin(ABC, metaclass=PluginMeta):
    @abstractmethod
    async def init(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


class PluginManager:
    plugins: list[Plugin] = []

    @classmethod
    async def init(cls):
        for plugin_cls in PluginMeta.plugin_classes:
            plugin = plugin_cls()
            await plugin.init()
            cls.plugins.append(plugin)

    @classmethod
    async def close(cls):
        for plugin in cls.plugins:
            await plugin.close()
