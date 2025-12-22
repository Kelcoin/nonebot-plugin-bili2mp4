from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="nonebot-plugin-bili2mp4",
    description="在指定群内自动将B站小程序/分享链接解析并下载为MP4后发送。支持私聊管理开�?停止、设置B站Cookie、清晰度与大小限制�?,
    usage=(
        "超级管理员私聊命令：\n"
        "1) 转换<群号>\n"
        "2) 停止转换<群号>\n"
        "3) 设置B站COOKIE <cookie字符�?\n"
        "4) 清除B站COOKIE\n"
        "5) 设置清晰�?数字>（如 720/1080�? 代表不限制）\n"
        "6) 设置最大大�?数字>MB�? 代表不限制）\n"
        "7) 查看参数 / 查看转换列表\n"
        "说明：启用的群里检测到B站分享（含小程序卡片）将尝试下载并发送MP4；需要时可设置Cookie�?
    ),
    type="application",
    config=Config,
    homepage="https://github.com/j1udu/nonebot-plugin-bili2mp4",
    supported_adapters={"~onebot.v11"},
    extra={},
)
try:
    from . import __main__

except (ImportError, ValueError):
    pass
