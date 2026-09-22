"""Load the plugin against a real, local AstrBot checkout."""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", PLUGIN_DIR.parent / "AstrBot"))
sys.path.insert(0, str(ASTRBOT_SOURCE))
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="command-tools-tests-")

from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.message_components import Plain  # noqa: E402
from astrbot.core.platform.astrbot_message import (  # noqa: E402
    AstrBotMessage,
    MessageMember,
)
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402
from astrbot.core.star.star import StarMetadata  # noqa: E402
from astrbot.core.star.star_handler import StarHandlerRegistry  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "command_tools_test_plugin", PLUGIN_DIR / "main.py"
)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


@pytest.fixture
def env(monkeypatch):
    """Provide real AstrBot event and registry classes without external services."""
    registry = StarHandlerRegistry()
    real_session_filter = bridge.SessionPluginManager.filter_handlers_by_session
    real_session_enabled = bridge.SessionPluginManager.is_plugin_enabled_for_session
    monkeypatch.setattr(bridge, "star_handlers_registry", registry)
    monkeypatch.setattr(bridge.sp, "get_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        bridge.SessionPluginManager,
        "filter_handlers_by_session",
        AsyncMock(side_effect=lambda event, handlers: handlers),
    )
    monkeypatch.setattr(
        bridge.SessionPluginManager,
        "is_plugin_enabled_for_session",
        AsyncMock(return_value=True),
    )
    monkeypatch.setitem(
        bridge.star_map,
        bridge.__name__,
        StarMetadata(
            name="astrbot_plugin_command_tools",
            module_path=bridge.__name__,
            activated=True,
        ),
    )
    monkeypatch.setitem(
        bridge.star_map,
        "tests.commands",
        StarMetadata(
            name="sample",
            module_path="tests.commands",
            activated=True,
        ),
    )
    config = {"plugin_set": ["*"], "disable_builtin_commands": False}
    manager = SimpleNamespace(func_list=[])
    context = Context.__new__(Context)
    context.provider_manager = SimpleNamespace(llm_tools=manager)
    context.get_config = lambda umo=None: config
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "session"
    message.message_id = "message"
    message.sender = MessageMember(user_id="user", nickname="User")
    message.message_str = "请帮我执行命令"
    message.message = [Plain(message.message_str)]
    message.raw_message = {"text": message.message_str}
    event = AstrMessageEvent(
        message.message_str,
        message,
        PlatformMetadata("webchat", "Test", "test"),
        "session",
    )
    event.send = AsyncMock()
    return SimpleNamespace(
        bridge=bridge,
        registry=registry,
        context=context,
        manager=manager,
        event=event,
        config=config,
        real_session_filter=real_session_filter,
        real_session_enabled=real_session_enabled,
    )
