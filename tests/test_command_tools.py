"""Behavioral tests using AstrBot's real command filters and tool executor."""

import asyncio
import functools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import At, File, Image, Plain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata


def register(
    env,
    function,
    command="echo",
    extra_filters=(),
    module="tests.commands",
    parents=None,
):
    """Register a real filter before binding self, matching AstrBot's loader.

    Args:
        env: Isolated test environment.
        function: Unbound command function.
        command: Canonical command fragment.
        extra_filters: Additional real filter objects.
        module: Owning plugin's module.
        parents: Full names of parent command groups.

    Returns:
        Registered handler and its command filter.
    """
    handler = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module}_{function.__name__}",
        handler_name=function.__name__,
        handler_module_path=module,
        handler=function,
        event_filters=[],
        desc="Test command",
    )
    command_filter = CommandFilter(
        command, handler_md=handler, parent_command_names=parents
    )
    handler.event_filters = [command_filter, *extra_filters]
    handler.handler = functools.partial(function, SimpleNamespace())
    env.registry.append(handler)
    return handler, command_filter


async def make_plugin(env, allowed):
    """Initialize the bridge with an explicit test allowlist.

    Args:
        env: Isolated environment.
        allowed: Qualified command selectors.

    Returns:
        Initialized plugin instance.
    """
    plugin = env.bridge.Main(
        env.context, {"allowed_commands": allowed, "command_timeout": 1}
    )
    await plugin.initialize()
    return plugin


async def invoke(env, tool, arguments=""):
    """Call the actual model-tool entry point.

    Args:
        env: Isolated environment.
        tool: Registered command tool.
        arguments: Raw command argument text.

    Returns:
        Parsed tool result envelope.
    """
    context = ContextWrapper(SimpleNamespace(event=env.event, context=env.context))
    return json.loads(await tool.call(context, arguments=arguments))


@pytest.mark.parametrize(
    "config",
    [
        {"legacy_commands": []},
        {"allowed_commands": "help"},
        {"allowed_commands": ["help"]},
        {"allowed_commands": ["builtin_commands:/help"]},
        {"allowed_commands": ["*:help"]},
        {"allowed_commands": [1]},
        {"command_timeout": True},
        {"command_timeout": 0},
        {"command_timeout": float("nan")},
    ],
)
def test_invalid_config_fails_fast(env, config):
    with pytest.raises(ValueError):
        env.bridge.Main(env.context, config)


async def test_empty_allowlist_and_unknown_selector(env):
    async def echo(self, event):
        event.set_result("hello")

    register(env, echo)
    assert not (await make_plugin(env, [])).tools
    assert not (await make_plugin(env, ["sample:missing"])).tools
    assert env.manager.func_list == []


async def test_real_executor_collects_send_yield_and_result_with_isolation(env):
    async def echo(self, event, count: int, text: GreedyStr):
        assert (
            event.message_obj.message_str == event.message_str == "echo 2 hello world"
        )
        assert event.get_sender_id() == "user"
        assert event.unified_msg_origin == env.event.unified_msg_origin
        event.session_id = "changed"
        event.message_obj.sender.user_id = "changed"
        event.set_extra("private", "changed")
        await event.send(MessageChain().message("progress"))
        yield event.plain_result(text * count)
        event.set_result("done")
        event.stop_event()
        yield

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    env.event.set_result("outer result")
    context = ContextWrapper(SimpleNamespace(event=env.event, context=env.context))
    results = [
        item
        async for item in FunctionToolExecutor.execute(
            tool, context, arguments="2 hello world"
        )
    ]
    result = json.loads(results[0].content[0].text)
    assert result["status"] == "ok"
    assert result["output"] == "progress\nhello worldhello world\ndone"
    assert result["sent_messages"] == 0
    env.event.send.assert_not_awaited()
    assert env.event.get_result().get_plain_text() == "outer result"
    assert not env.event.is_stopped()
    assert not env.event._has_send_oper
    assert env.event.get_sender_id() == "user"
    assert env.event.session_id == "session"
    assert env.event.get_extra("private") is None
    assert env.event.message_obj.raw_message == {"text": "请帮我执行命令"}
    assert tool.handler_module_path == env.bridge.__name__


async def test_permission_before_argument_parsing(env):
    calls = []

    async def echo(self, event, count: int):
        calls.append(count)
        event.set_result(str(count))

    register(env, echo, extra_filters=[PermissionTypeFilter(PermissionType.ADMIN)])
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    result = await invoke(env, tool, "invalid")
    assert result["status"] == "error" and "权限" in result["error"]
    assert not calls
    env.event.role = "admin"
    assert (await invoke(env, tool, "3"))["output"] == "3"
    for args in ("", "abc", "3 extra"):
        assert (await invoke(env, tool, args))["status"] == "error"
    assert calls == [3]


async def test_parent_group_permissions_disabled_state_and_custom_filters(env):
    async def echo(self, event):
        event.set_result("ok")

    handler, leaf = register(env, echo, parents=["group"])
    group = CommandGroupFilter("group")
    group.add_sub_command_filter(leaf)
    parent = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "tests.commands_group",
        "group",
        "tests.commands",
        echo,
        [group, PermissionTypeFilter(PermissionType.ADMIN)],
    )
    env.registry.append(parent)
    plugin = await make_plugin(env, ["sample:group echo"])
    tool = plugin.tools["sample:group echo"]
    assert (await invoke(env, tool))["status"] == "error"
    env.event.role = "admin"
    assert (await invoke(env, tool))["output"] == "ok"
    parent.enabled = False
    assert (await invoke(env, tool))["status"] == "error"
    parent.enabled = True
    group.add_custom_filter(SimpleNamespace(filter=lambda event, cfg: False))
    assert (await invoke(env, tool))["status"] == "error"


@pytest.mark.parametrize(
    "denial", ["handler", "plugin", "config", "event", "session", "bridge", "builtin"]
)
async def test_call_rechecks_runtime_policy(env, monkeypatch, denial):
    calls = []

    async def echo(self, event):
        calls.append(True)
        event.set_result("ok")

    module = env.bridge.BUILTIN_MODULE if denial == "builtin" else "tests.commands"
    if denial == "builtin":
        monkeypatch.setitem(
            env.bridge.star_map,
            module,
            StarMetadata(name="sample", module_path=module, reserved=True),
        )
    handler, _ = register(env, echo, module=module)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    if denial == "handler":
        handler.enabled = False
    elif denial == "plugin":
        env.bridge.star_map[module].activated = False
    elif denial == "config":
        env.config["plugin_set"] = ["astrbot_plugin_command_tools"]
    elif denial == "event":
        env.event.plugins_name = ["astrbot_plugin_command_tools"]
    elif denial == "session":
        monkeypatch.setattr(
            env.bridge.SessionPluginManager,
            "filter_handlers_by_session",
            AsyncMock(return_value=[]),
        )
    elif denial == "bridge":
        monkeypatch.setattr(
            env.bridge.SessionPluginManager,
            "is_plugin_enabled_for_session",
            AsyncMock(return_value=False),
        )
    else:
        env.config["disable_builtin_commands"] = True
    assert (await invoke(env, tool))["status"] == "error"
    assert not calls


async def test_rename_unload_refresh_and_terminate_invalidate_tools(env):
    async def echo(self, event):
        event.set_result("ok")

    handler, command_filter = register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    command_filter.command_name = "renamed"
    command_filter._cmpl_cmd_names = None
    assert (await invoke(env, tool))["status"] == "error"
    await plugin.refresh()
    assert not env.manager.func_list
    command_filter.command_name = "echo"
    command_filter._cmpl_cmd_names = None
    await plugin.refresh()
    new_tool = plugin.tools["sample:echo"]
    assert (await invoke(env, tool))["status"] == "error"
    assert (await invoke(env, new_tool))["status"] == "ok"
    env.registry.remove(handler)
    assert (await invoke(env, new_tool))["status"] == "error"
    unrelated = SimpleNamespace(name="unrelated")
    env.manager.func_list.append(unrelated)
    await plugin.terminate()
    assert env.manager.func_list == [unrelated]
    assert (await invoke(env, new_tool))["status"] == "error"


async def test_conflicts_and_disabled_tool_state(env, monkeypatch):
    async def echo(self, event):
        event.set_result("ok")

    async def duplicate(self, event):
        event.set_result("wrong")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    tool.active = False
    await plugin.refresh()
    assert plugin.tools["sample:echo"] is tool and not tool.active
    assert (await invoke(env, tool))["status"] == "error"
    tool.active = True
    duplicate_handler, _ = register(env, duplicate)
    assert (await invoke(env, tool))["status"] == "error"
    await plugin.refresh()
    assert not plugin.tools
    env.registry.remove(duplicate_handler)
    monkeypatch.setattr(env.bridge.sp, "get_async", AsyncMock(return_value=[tool.name]))
    await plugin.refresh()
    assert not plugin.tools["sample:echo"].active


async def test_timeout_cancels_handler_and_reports_partial_output(env):
    cancelled = []

    async def echo(self, event):
        await event.send(MessageChain().message("started"))
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "error" and "超时" in result["error"]
    assert result["output"] == "started" and cancelled == [True]


@pytest.mark.parametrize("reply_method", ["send", "yield", "return", "set_result"])
async def test_media_reply_interfaces_preserve_chain_and_event(env, reply_method):
    reply = env.event.chain_result(
        [
            Plain("合成结果："),
            Image.fromURL("https://example.com/image.png"),
            Plain("附件"),
            File(name="result.txt", url="https://example.com/result.txt"),
        ]
    )
    reply.use_markdown_ = False
    reply.stop_event()
    env.event.set_result("outer result")

    async def echo(self, event):
        event.session_id = "changed"
        event.message_obj.sender.user_id = "changed"
        if reply_method == "send":
            await event.send(reply)
        elif reply_method == "return":
            return reply
        else:
            event.set_result(reply)

    async def generator(self, event):
        yield reply
        pytest.fail("A stopped media reply must stop the handler")

    register(env, generator if reply_method == "yield" else echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "ok"
    assert result["sent_messages"] == 1
    assert result["output"] == "合成结果：附件"
    env.event.send.assert_awaited_once_with(reply)
    assert env.event.send.call_args.args[0] is reply
    assert env.event.session_id == "session"
    assert env.event.get_sender_id() == "user"
    assert env.event.get_result().get_plain_text() == "outer result"
    assert not env.event.is_stopped()


@pytest.mark.parametrize("media_type", ["url", "base64", "local", "file"])
async def test_media_is_sent_before_temporary_file_cleanup(env, tmp_path, media_type):
    asset = tmp_path / "result.png"
    asset.write_bytes(b"generated media")
    component = {
        "url": Image.fromURL("https://example.com/image.png"),
        "base64": Image.fromBytes(b"generated media"),
        "local": Image.fromFileSystem(str(asset)),
        "file": File(name=asset.name, file=str(asset)),
    }[media_type]

    async def platform_send(message):
        assert asset.read_bytes() == b"generated media"
        assert message.chain == [component]

    env.event.send.side_effect = platform_send

    async def echo(self, event):
        try:
            await event.send(event.chain_result([component]))
        finally:
            asset.unlink()

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "ok"
    assert result["sent_messages"] == 1
    assert result["output"] == "已向当前会话发送 1 条含图片或文件的消息。"
    env.event.send.assert_awaited_once()
    assert not asset.exists()
    assert str(asset) not in json.dumps(result)


async def test_media_generator_sends_in_order_including_final_result(env):
    replies = [
        env.event.chain_result([Image.fromBytes(bytes([index]))]) for index in range(3)
    ]

    async def echo(self, event):
        await event.send(replies[0])
        yield replies[1]
        event.set_result(replies[2])

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "ok" and result["sent_messages"] == 3
    assert [call.args[0] for call in env.event.send.await_args_list] == replies


@pytest.mark.parametrize("catch_error", [False, True])
async def test_media_delivery_failure_reports_partial_sends(env, catch_error):
    env.event.send.side_effect = [None, RuntimeError("platform send failed")]

    async def echo(self, event):
        await event.send(MessageChain().message("started"))
        reply = event.chain_result([Image.fromBytes(b"image")])
        await event.send(reply)
        try:
            await event.send(reply)
        except RuntimeError:
            if not catch_error:
                raise

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "error"
    assert "发送失败" in result["error"] and "请勿自动重试" in result["error"]
    assert result["sent_messages"] == 1
    assert result["output"] == "started"
    assert env.event.send.await_count == 2


async def test_media_send_is_covered_by_command_timeout(env):
    cancelled = []

    async def platform_send(message):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    env.event.send.side_effect = platform_send

    async def echo(self, event):
        yield event.chain_result([Image.fromBytes(b"image")])
        pytest.fail("A timed-out send must not continue the command")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "error" and "超时" in result["error"]
    assert result["sent_messages"] == 0
    assert cancelled == [True]


async def test_unsupported_component_rejects_whole_reply(env):
    async def echo(self, event):
        event.set_result(
            event.chain_result([Plain("text"), Image.fromBytes(b"image"), At(qq="123")])
        )

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "error" and "At" in result["error"]
    assert result["sent_messages"] == 0 and result["output"] == ""
    env.event.send.assert_not_awaited()


async def test_output_truncation(env):
    async def echo(self, event):
        await event.send(MessageChain().message("a" * 20000))

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "ok" and result["output"].endswith("[输出已截断]")
    assert len(result["output"]) < 16100


async def test_platform_and_message_type_filters(env):
    async def echo(self, event):
        event.set_result("ok")

    register(
        env,
        echo,
        extra_filters=[
            filter.EventMessageTypeFilter(filter.EventMessageType.GROUP_MESSAGE)
        ],
    )
    plugin = await make_plugin(env, ["sample:echo"])
    assert (await invoke(env, plugin.tools["sample:echo"]))["status"] == "error"


async def test_nested_group_and_generator_cleanup(env):
    closed = []

    async def echo(self, event):
        try:
            event.stop_event()
            yield event.plain_result("done")
            pytest.fail("A stopped command must not continue")
        finally:
            closed.append(True)

    _, leaf = register(env, echo, parents=["root child"])
    root = CommandGroupFilter("root")
    child = CommandGroupFilter("child", parent_group=root)
    child.add_sub_command_filter(leaf)
    root.add_sub_command_filter(child)
    for name, group in [("root", root), ("child", child)]:
        env.registry.append(
            StarHandlerMetadata(
                EventType.AdapterMessageEvent,
                f"tests.commands_{name}",
                name,
                "tests.commands",
                echo,
                [group],
            )
        )
    plugin = await make_plugin(env, ["sample:root child echo"])
    result = await invoke(env, plugin.tools["sample:root child echo"])
    assert result["status"] == "ok" and result["output"] == "done"
    assert closed == [True]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"arguments": 2},
        {"arguments": "", "user_id": "admin"},
        {"arguments": "a" * 2001},
    ],
)
async def test_invalid_model_arguments_do_not_execute(env, kwargs):
    async def echo(self, event):
        pytest.fail("Invalid model arguments must not execute commands")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    context = ContextWrapper(SimpleNamespace(event=env.event, context=env.context))
    result = json.loads(await plugin.tools["sample:echo"].call(context, **kwargs))
    assert result["status"] == "error"


async def test_real_session_plugin_policy(env, monkeypatch):
    async def echo(self, event):
        pytest.fail("A session-disabled plugin must not run")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    monkeypatch.setattr(
        env.bridge.SessionPluginManager,
        "filter_handlers_by_session",
        env.real_session_filter,
    )
    monkeypatch.setattr(
        env.bridge.SessionPluginManager,
        "is_plugin_enabled_for_session",
        env.real_session_enabled,
    )
    monkeypatch.setattr(
        env.bridge.sp,
        "get_async",
        AsyncMock(
            return_value={
                env.event.unified_msg_origin: {"disabled_plugins": ["sample"]}
            }
        ),
    )
    assert (await invoke(env, plugin.tools["sample:echo"]))["status"] == "error"


async def test_unload_during_policy_check_and_refresh(env, monkeypatch):
    async def echo(self, event):
        pytest.fail("An unloaded command must not run")

    handler, _ = register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])

    async def unload_during_check(event, handlers):
        env.registry.remove(handler)
        return handlers

    monkeypatch.setattr(
        env.bridge.SessionPluginManager,
        "filter_handlers_by_session",
        unload_during_check,
    )
    assert (await invoke(env, plugin.tools["sample:echo"]))["status"] == "error"

    async def terminate_during_refresh(*args):
        await plugin.terminate()
        return []

    monkeypatch.setattr(env.bridge.sp, "get_async", terminate_during_refresh)
    await plugin.refresh()
    assert plugin.closed and not plugin.tools and not env.manager.func_list


async def test_registration_collision_is_not_overwritten(env):
    async def echo(self, event):
        event.set_result("ok")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    unrelated = SimpleNamespace(name=tool.name)
    env.manager.func_list.append(unrelated)
    with pytest.raises(ValueError, match="工具名称冲突"):
        await plugin.refresh()
    assert env.manager.func_list == [tool, unrelated]


async def test_builtin_help_provider_and_lifecycle_exclusion(env, monkeypatch):
    import astrbot.builtin_stars.builtin_commands.commands.help as help_module
    from astrbot.builtin_stars.builtin_commands.commands.help import HelpCommand
    from astrbot.builtin_stars.builtin_commands.commands.provider import (
        ProviderCommands,
    )
    from astrbot.builtin_stars.builtin_commands.main import Main as Builtins

    module = env.bridge.BUILTIN_MODULE
    monkeypatch.setitem(
        env.bridge.star_map,
        module,
        StarMetadata(name="builtin_commands", module_path=module, reserved=True),
    )
    monkeypatch.setattr(
        help_module, "get_dashboard_version", AsyncMock(return_value="test")
    )
    monkeypatch.setattr(
        HelpCommand, "_query_astrbot_notice", AsyncMock(return_value="")
    )
    monkeypatch.setattr(
        HelpCommand,
        "_build_reserved_command_lines",
        AsyncMock(return_value=["/provider - View or switch LLM Provider"]),
    )
    env.context.get_all_providers = lambda: []
    env.context.get_all_tts_providers = lambda: []
    env.context.get_all_stt_providers = lambda: []
    env.context.get_using_provider_async = AsyncMock(return_value=None)
    help_handler, _ = register(env, Builtins.help, command="help", module=module)
    help_handler.handler = functools.partial(
        Builtins.help, SimpleNamespace(help_c=HelpCommand(env.context))
    )
    provider_handler, _ = register(
        env,
        Builtins.provider,
        command="provider",
        module=module,
        extra_filters=[PermissionTypeFilter(PermissionType.ADMIN)],
    )
    provider_handler.handler = functools.partial(
        Builtins.provider, SimpleNamespace(provider_c=ProviderCommands(env.context))
    )
    register(env, Builtins.reset, command="reset", module=module)
    plugin = await make_plugin(
        env,
        [
            "builtin_commands:help",
            "builtin_commands:provider",
            "builtin_commands:reset",
        ],
    )
    assert "builtin_commands:reset" not in plugin.tools
    result = await invoke(env, plugin.tools["builtin_commands:help"])
    assert result["status"] == "ok" and "AstrBot v" in result["output"]
    provider_tool = plugin.tools["builtin_commands:provider"]
    assert (await invoke(env, provider_tool))["status"] == "error"
    env.event.role = "admin"
    result = await invoke(env, provider_tool)
    assert result["status"] == "ok" and "LLM Providers" in result["output"]
