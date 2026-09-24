"""Behavioral tests using AstrBot's real command filters and tool executor."""

import asyncio
import functools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import (
    At,
    File,
    Forward,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)


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


async def invoke(env, tool, **arguments):
    """Call the actual model-tool entry point.

    Args:
        env: Isolated environment.
        tool: Registered command tool.
        arguments: Named command arguments.

    Returns:
        Parsed tool result envelope.
    """
    context = ContextWrapper(SimpleNamespace(event=env.event, context=env.context))
    return json.loads(await tool.call(context, **arguments))


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
            tool, context, count=2, text="hello world"
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
    result = await invoke(env, tool, count="invalid")
    assert result["status"] == "error" and "权限" in result["error"]
    assert not calls
    env.event.role = "admin"
    assert (await invoke(env, tool, count=3))["output"] == "3"
    for args in ({}, {"count": "abc"}, {"count": 3, "extra": 4}):
        assert (await invoke(env, tool, **args))["status"] == "error"
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
@pytest.mark.parametrize(
    "reply_type",
    [
        "media",
        "at",
        "at_text",
        "at_media",
        "record",
        "video",
        "node",
        "nodes",
        "forward",
    ],
)
async def test_reply_interfaces_preserve_chain_and_event(env, reply_method, reply_type):
    components = []
    expected_output = "已向当前会话发送 1 条命令回复消息。"
    if reply_type.startswith("at"):
        components.append(At(qq="123", name="User"))
    if reply_type == "at_text":
        components.extend([Plain("处理完成"), At(qq=456)])
        expected_output = "处理完成"
    elif reply_type in {"media", "at_media"}:
        components.extend(
            [
                Plain("合成结果："),
                Image.fromURL("https://example.com/image.png"),
                Plain("附件"),
                File(name="result.txt", url="https://example.com/result.txt"),
            ]
        )
        expected_output = "合成结果：附件"
    elif reply_type == "record":
        components.append(Record.fromURL("https://example.com/audio.mp3"))
    elif reply_type == "video":
        components.append(Video.fromURL("https://example.com/video.mp4"))
    elif reply_type in {"node", "nodes"}:
        node = Node(
            uin="123",
            name="Author",
            content=[
                Plain("转发内容"),
                At(qq="456"),
                Image.fromBytes(b"image"),
                Record.fromURL("https://example.com/audio.mp3"),
                Video.fromURL("https://example.com/video.mp4"),
                File(name="result.txt", url="https://example.com/result.txt"),
            ],
        )
        if reply_type == "node":
            components.append(node)
        else:
            components.extend(
                [
                    Plain("聊天记录："),
                    Nodes(
                        [node, Node(uin="456", name="Other", content=[Nodes([node])])]
                    ),
                ]
            )
            expected_output = "聊天记录："
    elif reply_type == "forward":
        components.append(Forward(id="forward-id"))
    reply = env.event.chain_result(components)
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
        pytest.fail("A stopped reply must stop the handler")

    register(env, generator if reply_method == "yield" else echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "ok"
    assert result["sent_messages"] == 1
    assert result["output"] == expected_output
    env.event.send.assert_awaited_once_with(reply)
    assert env.event.send.call_args.args[0] is reply
    assert reply.chain == components
    assert env.event.session_id == "session"
    assert env.event.get_sender_id() == "user"
    assert env.event.get_result().get_plain_text() == "outer result"
    assert not env.event.is_stopped()


@pytest.mark.parametrize(
    "media_type", ["url", "base64", "local", "file", "record", "video", "nodes"]
)
async def test_media_is_sent_before_temporary_file_cleanup(env, tmp_path, media_type):
    asset = tmp_path / "result.png"
    asset.write_bytes(b"generated media")
    component = {
        "url": Image.fromURL("https://example.com/image.png"),
        "base64": Image.fromBytes(b"generated media"),
        "local": Image.fromFileSystem(str(asset)),
        "file": File(name=asset.name, file=str(asset)),
        "record": Record.fromFileSystem(str(asset)),
        "video": Video.fromFileSystem(str(asset)),
        "nodes": Nodes(
            [
                Node(
                    uin="123",
                    name="Author",
                    content=[
                        Record.fromFileSystem(str(asset)),
                        Video.fromFileSystem(str(asset)),
                    ],
                )
            ]
        ),
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
    assert result["output"] == "已向当前会话发送 1 条命令回复消息。"
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
@pytest.mark.parametrize("reply_type", ["image", "at", "record", "video", "nodes"])
async def test_reply_delivery_failure_reports_partial_sends(
    env, catch_error, reply_type
):
    env.event.send.side_effect = [None, RuntimeError("platform send failed")]

    async def echo(self, event):
        await event.send(MessageChain().message("started"))
        component = {
            "image": Image.fromBytes(b"image"),
            "at": At(qq="123"),
            "record": Record.fromURL("https://example.com/audio.mp3"),
            "video": Video.fromURL("https://example.com/video.mp4"),
            "nodes": Nodes([Node(uin="123", name="Author", content=[Plain("text")])]),
        }[reply_type]
        reply = event.chain_result([component])
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


@pytest.mark.parametrize("wrapper", ["direct", "node", "nodes", "nested"])
async def test_unsupported_component_rejects_whole_reply(env, wrapper):
    component = Json({"app": "unsupported"})
    if wrapper != "direct":
        component = Node(uin="123", name="Author", content=[component])
    if wrapper in {"nodes", "nested"}:
        component = Nodes([component])
    if wrapper == "nested":
        component = Nodes([Node(uin="456", name="Other", content=[component])])

    async def echo(self, event):
        event.set_result(
            event.chain_result(
                [
                    Plain("text"),
                    At(qq="123"),
                    Image.fromBytes(b"image"),
                    Record.fromURL("https://example.com/audio.mp3"),
                    component,
                ]
            )
        )

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"])
    assert result["status"] == "error" and "不支持的消息组件：Json；" in result["error"]
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
        {"arguments": ""},
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


async def test_structured_schema_types_required_defaults_and_empty_command(env):
    async def echo(
        self,
        event,
        count: int,
        text: str,
        scale: float = 1.5,
        enabled: bool = False,
        target: str | int | None = None,
    ):
        event.set_result(json.dumps([count, text, scale, enabled, target]))

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    schema = tool.parameters
    assert schema["required"] == ["count", "text"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["count"] == {"type": "integer"}
    assert schema["properties"]["enabled"] == {"type": "boolean", "default": False}
    assert schema["properties"]["scale"] == {"type": "number", "default": 1.5}
    assert schema["properties"]["target"]["anyOf"] == [
        {"type": "string", "maxLength": 2000},
        {"type": "integer"},
        {"type": "null"},
    ]
    result = await invoke(env, tool, text="hello world", count=2, target=7)
    assert result["status"] == "ok"
    assert json.loads(result["output"]) == [2, "hello world", 1.5, False, 7]

    async def empty(self, event):
        event.set_result("no args")

    register(env, empty, command="empty")
    plugin.allowed.add("sample:empty")
    await plugin.refresh()
    empty_tool = plugin.tools["sample:empty"]
    assert empty_tool.parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert (await invoke(env, empty_tool))["output"] == "no args"
    result = await invoke(env, empty_tool, arguments="")
    assert result["status"] == "error" and "arguments" in result["error"]


@pytest.mark.parametrize("value", [12, "0012", None])
async def test_nullable_union_preserves_supplied_type(env, value):
    async def echo(self, event, target: int | str | None):
        assert type(target) is type(value)
        assert target == value
        event.set_result("ok")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    assert (await invoke(env, tool))["status"] == "error"
    assert (await invoke(env, tool, target=value))["status"] == "ok"


async def test_optional_field_omission_differs_from_explicit_null(env):
    async def echo(self, event, value: str | None = "default", other: int = 2):
        event.set_result(json.dumps([value, other]))

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    result = await invoke(env, tool, other=9)
    assert json.loads(result["output"]) == ["default", 9]
    result = await invoke(env, tool, value=None)
    assert json.loads(result["output"]) == [None, 2]


@pytest.mark.parametrize("text", ["", '  中文  "text"\nline\tend  '])
async def test_strings_and_greedy_text_preserve_whitespace(env, text):
    async def echo(self, event, ordinary: str, rest: GreedyStr):
        assert ordinary == rest == text
        assert event.get_extra("parsed_params") == {"ordinary": text, "rest": text}
        event.set_result("ok")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    assert (await invoke(env, plugin.tools["sample:echo"], ordinary=text, rest=text))[
        "status"
    ] == "ok"


@pytest.mark.parametrize(
    "overrides",
    [
        {"count": "2"},
        {"count": True},
        {"count": 2.5},
        {"count": None},
        {"scale": True},
        {"scale": float("nan")},
        {"scale": float("inf")},
        {"enabled": "false"},
        {"enabled": 0},
        {"text": 3},
        {"text": []},
        {"text": {}},
        {"text": "a" * 2001},
        {"target": False},
        {"extra": "unexpected"},
        {"arguments": "2 x"},
    ],
)
async def test_structured_values_are_validated_before_filters_or_handler(
    env, overrides
):
    calls = []

    async def echo(
        self,
        event,
        count: int,
        text: str,
        scale: float = 1.5,
        enabled: bool = False,
        target: int | str | None = None,
    ):
        pytest.fail("Invalid values must not run the handler")

    _, command_filter = register(env, echo)
    command_filter.add_custom_filter(
        SimpleNamespace(filter=lambda event, cfg: calls.append(True))
    )
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(
        env, plugin.tools["sample:echo"], **{"count": 2, "text": "ok", **overrides}
    )
    assert result["status"] == "error" and "参数" in result["error"]
    assert calls == []
    env.event.send.assert_not_awaited()


async def test_argument_total_length_limit(env):
    async def echo(self, event, a: str, b: str):
        pytest.fail("Oversized arguments must not run")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    result = await invoke(env, plugin.tools["sample:echo"], a="a" * 1000, b="b" * 1000)
    assert result["status"] == "error" and "合计" in result["error"]


async def test_resolved_annotations_default_inference_and_keyword_only_params(env):
    async def echo(
        self, event, count: "int", *, enabled=False, context: str = "default"
    ):
        event.set_result(json.dumps([count, enabled, context]))

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    assert tool.parameters["properties"]["enabled"]["type"] == "boolean"
    result = await invoke(env, tool, count=3, enabled=True, context="custom")
    assert result["status"] == "ok"
    assert json.loads(result["output"]) == [3, True, "custom"]


@pytest.mark.parametrize(
    "declaration",
    [
        "value",
        "value=None",
        "value: list[str]",
        "value: int = None",
        "value: str = 1",
        "*values: str",
        "**values: str",
        "value: int, /",
    ],
)
async def test_unsupported_declarations_are_reported_without_breaking_other_tools(
    env, declaration
):
    namespace = {}
    exec(f"async def unsupported(self, event, {declaration}):\n    pass", namespace)
    register(env, namespace["unsupported"], command="unsupported")

    async def echo(self, event):
        event.set_result("ok")

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo", "sample:unsupported"])
    assert list(plugin.tools) == ["sample:echo"]
    assert "sample:unsupported" in plugin.unsupported
    assert (await invoke(env, plugin.tools["sample:echo"]))["output"] == "ok"
    await plugin.list_commands(env.event)
    listing = env.event.get_result().get_plain_text()
    assert "参数不支持：" in listing and "sample:unsupported" in listing


async def test_subcommand_keeps_parent_and_leaf_filters_with_structured_args(env):
    checked = []

    async def echo(self, event, a: int, b: int = 2):
        event.set_result(str(a + b))

    _, leaf = register(env, echo, parents=["math"])
    group = CommandGroupFilter("math")
    group.add_sub_command_filter(leaf)
    env.registry.append(
        StarHandlerMetadata(
            EventType.AdapterMessageEvent,
            "tests.commands_math",
            "math",
            "tests.commands",
            echo,
            [group, PermissionTypeFilter(PermissionType.ADMIN)],
        )
    )

    def custom_filter(event, cfg):
        checked.append(event.get_message_str())
        return event.get_extra("parsed_params") == {"a": 3, "b": 2}

    group.add_custom_filter(SimpleNamespace(filter=custom_filter))
    leaf.add_custom_filter(SimpleNamespace(filter=custom_filter))
    plugin = await make_plugin(env, ["sample:math echo"])
    tool = plugin.tools["sample:math echo"]
    assert (await invoke(env, tool, a=3))["status"] == "error"
    assert checked == []
    env.event.role = "admin"
    assert (await invoke(env, tool, a=3))["output"] == "5"
    assert checked == ["math echo 3 2", "math echo 3 2"]
    checked.clear()
    assert (await invoke(env, tool, a=1))["status"] == "error"
    assert checked == ["math echo 1 2"]


async def test_schema_refresh_invalidates_old_calls_and_keeps_disabled_state(env):
    async def echo(self, event, value=1):
        event.set_result(str(value))

    handler, _ = register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    original = plugin.tools["sample:echo"]

    # bool and int defaults compare equal in Python; their schemas must still differ.
    async def changed(event, value=True):
        event.set_result(str(value))

    handler.handler = changed
    result = await invoke(env, original, value=1)
    assert result["status"] == "error" and "参数声明已变更" in result["error"]
    original.active = False
    await plugin.refresh()
    updated = plugin.tools["sample:echo"]
    assert updated is not original and updated.name == original.name
    assert updated.parameters["properties"]["value"]["type"] == "boolean"
    assert not updated.active
    updated.active = True
    assert (await invoke(env, original, value=1))["status"] == "error"
    assert (await invoke(env, updated, value=False))["output"] == "False"


async def test_builtin_provider_declared_fields_are_passed_by_name(env, monkeypatch):
    from astrbot.builtin_stars.builtin_commands.main import Main as Builtins

    module = env.bridge.BUILTIN_MODULE
    monkeypatch.setitem(
        env.bridge.star_map,
        module,
        StarMetadata(
            name="builtin_commands",
            module_path=module,
            reserved=True,
        ),
    )
    handler, _ = register(env, Builtins.provider, command="provider", module=module)
    provider = AsyncMock()
    handler.handler = functools.partial(
        Builtins.provider,
        SimpleNamespace(
            provider_c=SimpleNamespace(provider=provider),
        ),
    )
    plugin = await make_plugin(env, ["builtin_commands:provider"])
    result = await invoke(env, plugin.tools["builtin_commands:provider"], idx=2)
    assert result["status"] == "ok"
    assert provider.await_args.args[1:] == (2, None)


async def test_custom_filter_cannot_redirect_the_selected_command(env):
    async def echo(self, event, count: int):
        pytest.fail("A command name mismatch must not execute")

    def redirect(event, cfg):
        event.message_str = "another 2"
        return True

    _, command_filter = register(env, echo)
    command_filter.add_custom_filter(SimpleNamespace(filter=redirect))
    plugin = await make_plugin(env, ["sample:echo"])
    assert (await invoke(env, plugin.tools["sample:echo"], count=2))[
        "status"
    ] == "error"


async def test_custom_command_filter_subclass_is_explicitly_rejected(env):
    class RestrictedCommandFilter(CommandFilter):
        def filter(self, event, cfg):
            return False

    async def echo(self, event, count: int):
        pytest.fail("Overridden command filters must never be bypassed")

    handler, _ = register(env, echo)
    # Registration inspects an unbound self/event function before loader binding.
    handler.handler = echo
    restricted = RestrictedCommandFilter("echo", handler_md=handler)
    handler.handler = functools.partial(echo, SimpleNamespace())
    handler.event_filters = [restricted]
    plugin = await make_plugin(env, ["sample:echo"])
    assert not plugin.tools
    assert "自定义 CommandFilter" in plugin.unsupported["sample:echo"]


async def test_declared_arguments_field_is_a_business_parameter(env):
    async def echo(self, event, arguments: int):
        event.set_result(str(arguments))

    register(env, echo)
    plugin = await make_plugin(env, ["sample:echo"])
    tool = plugin.tools["sample:echo"]
    assert (await invoke(env, tool, arguments=3))["output"] == "3"
    assert (await invoke(env, tool, arguments="3"))["status"] == "error"


@pytest.fixture
async def management(env, tmp_path):
    """Use real configuration persistence and the decorated management filters."""
    path = tmp_path / "plugin.json"
    config = AstrBotConfig(
        str(path), default_config={"allowed_commands": [], "command_timeout": 7}
    )
    plugin = env.bridge.Main(env.context, config)
    await plugin.initialize()
    handler = next(
        item
        for item in star_handlers_registry
        if item.handler_module_path == env.bridge.__name__
        and item.handler_name == "manage_commands"
    )

    async def send(arguments="", role="admin"):
        event = env.event
        event.role = role
        event.is_at_or_wake_command = True
        event.message_str = "cmdtools" + (f" {arguments}" if arguments else "")
        event.clear_result()
        if all(rule.filter(event, env.config) for rule in handler.event_filters):
            await plugin.manage_commands(event, **event.get_extra("parsed_params"))
        result = event.get_result()
        return result.get_plain_text() if result else ""

    return SimpleNamespace(plugin=plugin, config=config, path=path, send=send)


async def test_management_persists_add_remove_and_invalidates_cached_tool(
    env, management
):
    async def echo(self, event):
        event.set_result("ok")

    register(env, echo)
    listing = await management.send()
    assert "[未加入白名单] sample:echo" in listing
    assert "cmdtools add" in listing and "cmdtools remove" in listing
    added = await management.send("add sample:echo")
    assert "已加入白名单并保存" in added and "已注册" in added
    tool = management.plugin.tools["sample:echo"]
    assert tool in env.manager.func_list
    assert (await invoke(env, tool))["output"] == "ok"
    saved = json.loads(management.path.read_text(encoding="utf-8-sig"))
    assert saved == {"allowed_commands": ["sample:echo"], "command_timeout": 7}

    reloaded_config = AstrBotConfig(
        str(management.path), default_config=management.config.default_config
    )
    reloaded = env.bridge.Main(env.context, reloaded_config)
    assert reloaded.allowed == {"sample:echo"}

    removed = await management.send("remove sample:echo")
    assert "已移出白名单并保存" in removed
    assert not management.plugin.allowed and not management.plugin.tools
    assert tool not in env.manager.func_list
    assert (await invoke(env, tool))["status"] == "error"
    assert json.loads(management.path.read_text(encoding="utf-8-sig")) == {
        "allowed_commands": [],
        "command_timeout": 7,
    }


async def test_management_handles_full_subcommand_and_missing_entries(env, management):
    async def echo(self, event):
        pass

    register(env, echo, command="add", parents=["math"])
    reply = await management.send("add sample:math add")
    assert "已注册" in reply
    assert "sample:math add" in management.plugin.tools
    assert "未找到" in await management.send("add absent:old command")
    assert management.config["allowed_commands"] == [
        "sample:math add",
        "absent:old command",
    ]
    assert "[未找到] absent:old command" in await management.send()
    await management.send("remove absent:old command")
    await management.send("remove sample:math add")
    assert not management.config["allowed_commands"]
    assert not management.plugin.tools


@pytest.mark.parametrize(
    "arguments",
    [
        "add sample:echo",
        "remove sample:echo",
        "",
    ],
)
async def test_management_requires_admin(management, arguments):
    assert await management.send(arguments, role="member") == ""
    assert not management.config["allowed_commands"]


@pytest.mark.parametrize(
    "arguments",
    [
        "add",
        "remove",
        "add help",
        "remove sample:/echo",
        "add *:echo",
        "add sample:echo other:command",
        "add sample:echo:extra",
        "update sample:echo",
    ],
)
async def test_management_rejects_invalid_input_without_saving(
    management, monkeypatch, arguments
):
    save = Mock(side_effect=AssertionError("Invalid input must not be saved"))
    monkeypatch.setattr(AstrBotConfig, "save_config", save)
    assert "用法" in await management.send(arguments)
    assert not management.plugin.allowed and not management.config["allowed_commands"]
    save.assert_not_called()


async def test_management_duplicate_and_absent_changes_do_not_save(
    management, monkeypatch
):
    await management.send("add sample:missing")
    save = Mock(side_effect=AssertionError("No-op changes must not be saved"))
    monkeypatch.setattr(AstrBotConfig, "save_config", save)
    assert "已在白名单中" in await management.send("add sample:missing")
    assert "不在白名单中" in await management.send("remove sample:absent")
    assert management.config["allowed_commands"] == ["sample:missing"]
    save.assert_not_called()


@pytest.mark.parametrize("action", ["add", "remove"])
async def test_management_save_failure_preserves_config_and_tools(
    env, management, monkeypatch, action
):
    async def echo(self, event):
        event.set_result("ok")

    register(env, echo)
    if action == "remove":
        await management.send("add sample:echo")
    previous_config = dict(management.config)
    previous_tools = dict(management.plugin.tools)
    previous_file = management.path.read_bytes()
    monkeypatch.setattr(
        AstrBotConfig, "save_config", Mock(side_effect=OSError("Disk unavailable"))
    )
    reply = await management.send(f"{action} sample:echo")
    assert "保存白名单失败" in reply and "未生效" in reply
    assert management.config == previous_config
    assert management.path.read_bytes() == previous_file
    assert management.plugin.allowed == set(previous_config["allowed_commands"])
    assert management.plugin.tools == previous_tools
    assert env.manager.func_list == list(previous_tools.values())


async def test_management_reports_refresh_failure_and_still_revokes_command(
    env, management, monkeypatch
):
    async def echo(self, event):
        pytest.fail("Removed command must not execute even if refresh fails")

    register(env, echo)
    await management.send("add sample:echo")
    tool = management.plugin.tools["sample:echo"]
    monkeypatch.setattr(
        management.plugin, "refresh", AsyncMock(side_effect=ValueError("Tool conflict"))
    )
    reply = await management.send("remove sample:echo")
    assert "已移出白名单并保存" in reply and "工具刷新失败" in reply
    assert not management.plugin.allowed
    assert (
        json.loads(management.path.read_text(encoding="utf-8-sig"))["allowed_commands"]
        == []
    )
    assert (await invoke(env, tool))["status"] == "error"


async def test_management_serializes_edits_and_rejects_changes_after_termination(
    env, management, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    refresh = management.plugin.refresh

    async def delayed_refresh():
        entered.set()
        await release.wait()
        await refresh()

    monkeypatch.setattr(management.plugin, "refresh", delayed_refresh)
    first = asyncio.create_task(management.send("add sample:first"))
    await entered.wait()
    second_event = env.bridge.CommandEvent(env.event, "cmdtools add sample:second")
    second = asyncio.create_task(
        management.plugin.manage_commands(second_event, "add sample:second")
    )
    release.set()
    await asyncio.gather(first, second)
    assert management.config["allowed_commands"] == ["sample:first", "sample:second"]
    await management.plugin.terminate()
    assert "插件已停用" in await management.send("add sample:third")
    assert management.config["allowed_commands"] == ["sample:first", "sample:second"]
