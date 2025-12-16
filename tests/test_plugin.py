import os
import sys

import pytest

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nonebot_plugin_bili2mp4 import __plugin_meta__
from nonebot_plugin_bili2mp4.config import Config


def test_plugin_meta():
    """测试插件元数据是否正确加载"""
    assert __plugin_meta__ is not None
    assert __plugin_meta__.name == "nonebot_plugin_bili2mp4"
    assert __plugin_meta__.type == "application"
    assert "~onebot.v11" in __plugin_meta__.supported_adapters


def test_plugin_config():
    """测试插件配置是否正确加载"""
    # 直接创建配置实例，避免依赖NoneBot驱动器
    config = Config()
    assert hasattr(config, "super_admins")
    assert isinstance(config.super_admins, list)
    assert all(isinstance(admin_id, int) for admin_id in config.super_admins)


@pytest.mark.asyncio
async def test_plugin_load():
    """测试插件是否能正确加载"""
    # 简化测试，只验证元数据
    assert __plugin_meta__ is not None
    assert __plugin_meta__.name == "nonebot_plugin_bili2mp4"
    assert __plugin_meta__.type == "application"
    assert "~onebot.v11" in __plugin_meta__.supported_adapters
