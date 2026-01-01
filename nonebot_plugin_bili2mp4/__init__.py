from __future__ import annotations

from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="nonebot-plugin-bili2mp4",
    description="在指定群内自动将B站视频解析并下载为 MP4 发送",
    usage="私聊bot“fhelp”查看",
    type="application",
    config=Config,
    homepage="https://github.com/j1udu/nonebot-plugin-bili2mp4",
    supported_adapters={"~onebot.v11"},
)

from . import main
