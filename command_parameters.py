"""Build and validate structured tool arguments from command signatures."""

from __future__ import annotations

import inspect
import json
import math
import types
import typing

from astrbot.core.star.filter.command import CommandFilter, GreedyStr

MAX_ARGUMENT_TEXT = 2000
EMPTY = inspect.Parameter.empty


def type_schema(annotation) -> dict:
    """Describe only explicitly supported scalar command types."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return {"anyOf": [type_schema(item) for item in typing.get_args(annotation)]}
    if annotation is str or annotation is GreedyStr:
        return {"type": "string", "maxLength": MAX_ARGUMENT_TEXT}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is None or annotation is type(None):
        return {"type": "null"}
    raise ValueError(f"不支持的参数类型：{annotation!r}")


def matches(value, schema: dict) -> bool:
    """Validate JSON scalar values without coercing strings or booleans."""
    if "anyOf" in schema:
        return any(matches(value, option) for option in schema["anyOf"])
    kind = schema["type"]
    if kind == "string":
        return isinstance(value, str) and len(value) <= MAX_ARGUMENT_TEXT
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    if kind == "boolean":
        return type(value) is bool
    return value is None


class CommandParameters:
    """Snapshot a bound handler's argument contract and generate its schema."""

    def __init__(self, handler, command_filter: CommandFilter) -> None:
        # Replacing an overridden parser could skip plugin-specific checks.
        if type(command_filter) is not CommandFilter:
            raise ValueError("自定义 CommandFilter 子类需要单独适配。")
        try:
            signature = inspect.signature(handler, eval_str=True)
        except (TypeError, ValueError, NameError) as exc:
            raise ValueError(f"无法读取命令参数声明：{exc}") from exc
        parameters = list(signature.parameters.values())
        if not parameters or parameters[0].kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise ValueError("命令处理器必须以事件作为第一个参数（绑定插件实例后）。")
        self.fields = tuple(parameters[1:])
        if [param.name for param in self.fields] != list(command_filter.handler_params):
            raise ValueError("命令函数签名与注册的参数列表不一致，请重载目标插件。")
        properties = {}
        required = []
        for param in self.fields:
            if param.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise ValueError(f"参数 {param.name} 不支持位置专用参数或可变参数。")
            annotation = param.annotation
            if annotation is EMPTY:
                # A concrete scalar default is evidence of its type; None isn't.
                if param.default is EMPTY or param.default is None:
                    raise ValueError(f"参数 {param.name} 缺少明确的类型声明。")
                annotation = type(param.default)
            try:
                schema = type_schema(annotation)
            except ValueError as exc:
                raise ValueError(f"参数 {param.name}：{exc}") from exc
            if param.default is EMPTY:
                required.append(param.name)
            else:
                if not matches(param.default, schema):
                    raise ValueError(
                        f"参数 {param.name} 的默认值与类型声明不一致或超限。"
                    )
                schema["default"] = param.default
            properties[param.name] = schema
        self.schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def bind(self, values: dict) -> dict:
        """Reject unknown/missing fields and bind declared defaults by name."""
        unknown = values.keys() - self.schema["properties"].keys()
        if unknown:
            raise ValueError(
                f"未知命令参数：{', '.join(sorted(unknown))}；"
                "请按工具声明的字段传参，不再支持旧的 arguments 包装格式。"
            )
        missing = set(self.schema["required"]) - values.keys()
        if missing:
            raise ValueError(f"缺少必填参数：{', '.join(sorted(missing))}")
        bound = {}
        for param in self.fields:
            value = values.get(param.name, param.default)
            schema = self.schema["properties"][param.name]
            if not matches(value, schema):
                raise ValueError(
                    f"参数 {param.name} 类型或长度错误，要求："
                    f"{json.dumps(schema, ensure_ascii=False, allow_nan=False)}"
                )
            bound[param.name] = value
        if len(self.to_text(bound)) > MAX_ARGUMENT_TEXT:
            raise ValueError("命令参数合计超过 2000 字符限制。")
        return bound

    @staticmethod
    def to_text(values: dict) -> str:
        """Build display text for event filters, never for argument parsing."""
        return " ".join(
            value if isinstance(value, str) else json.dumps(value, allow_nan=False)
            for value in values.values()
        )
