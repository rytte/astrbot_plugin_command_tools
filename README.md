# 命令转模型工具

把 AstrBot 及其插件命令变成模型可以自主调用的工具，让机器人帮你执行命令。

比如把 `/help` 加入白名单后，就可以直接说「看看你有哪些命令」。支持普通命令和子命令，也能回复文字、@ 提及、图片和文件。能否触发调用，还取决于模型的工具能力和当前人设设置。

## 🚀 快速开始

需要 **AstrBot 4.27.x 或更高的 4.x 版本**。

1. 将插件放入 `data/plugins/astrbot_plugin_command_tools`，或通过 WebUI 上传插件 ZIP，然后加载插件。手动部署时，在 AstrBot 的 Python 环境中执行 `python -m pip install -r requirements.txt`。
2. 管理员发送 `/cmdtools`，查看命令的完整标识和状态。
3. 发送 `/cmdtools add builtin_commands:help`，把帮助命令加入白名单。
4. 如果人设配置了工具白名单，记得启用 `/cmdtools` 中显示的 `cmd_...` 工具，然后就可以聊天试试了。

默认白名单为空，需要先添加命令。

## ⚙️ 命令与配置

以下管理命令仅限管理员使用：

| 命令 | 用途 |
| --- | --- |
| `/cmdtools` | 刷新并查看命令列表、状态和工具名 |
| `/cmdtools add builtin_commands:help` | 加入白名单 |
| `/cmdtools remove builtin_commands:help` | 移出白名单 |
| `/cmdtools add my_plugin:math add` | 添加子命令，空格保留，无需加引号 |

增删会自动保存并立即生效，无需重载。加入白名单后是否可用，以返回的状态为准；命令被禁用、不受支持或存在冲突时，不会生成工具。

也可以直接编辑插件配置，保存后重载插件：

```json
{
  "allowed_commands": ["builtin_commands:help"],
  "command_timeout": 30
}
```

- `allowed_commands`：填写 `插件名:完整命令名`，以 `/cmdtools` 输出为准，不带 `/`，不支持通配符。
- `command_timeout`：单次执行超时，默认 30 秒，可设为 1～120 秒。

未知字段或错误配置会直接报错。命令改名、启停后，可以发送 `/cmdtools` 刷新。

## 💬 参数和回复

每个命令生成一个独立工具，参数按原命令的函数声明自动生成。例如：

```python
async def add(self, event, a: int, b: int = 2):
    yield event.plain_result(str(a + b))
```

模型可以传入 `{"a": 1, "b": 3}`，也可以只传 `{"a": 1}`，让 `b` 使用默认值。无参数命令传 `{}`。

支持字符串、整数、浮点数、布尔值，以及这些类型的联合和可空声明。参数需要类型注解或明确的标量默认值；列表、字典、自定义类型、可变参数等暂不支持，原因会显示在 `/cmdtools` 和日志中。

回复方式：

- **纯文本**：作为工具结果返回模型。
- **含 At（@ 提及）、图片或文件**：整条消息链直接发送到当前会话，保留 @ 对象和组件顺序；其中的文本也会返回模型。
- **语音、视频、合并转发等其他组件**：明确报错；混合消息中只要有不支持的组件，整条都不会发送。流式回复暂不支持。

## 使用前了解一下

- **原有权限仍然有效。** 每次调用都会检查当前用户、命令及父命令组的权限，以及插件和会话的启用状态。
- **白名单开放的是整个命令。** 例如添加 `builtin_commands:provider` 后，也允许管理员通过模型调用它的切换模型功能。
- **不是所有命令都能直接转换。** 内置 `/reset`、`/new`、`/stop`、`/dashboard_update` 暂不支持；依赖原始消息、附件、后续用户输入或完整消息流水线的命令，需要单独适配。
- **只收集标准事件回复。** 通过平台 SDK、`context.send_message()` 或后台任务发送的内容，无法统一收集。
- **超时不会撤销已执行的操作。** 出错或超时后先确认结果，避免自动重试造成重复操作。

## 本地开发

准备好包含 AstrBot、pytest、pytest-asyncio 和 Ruff 的 Python 环境。AstrBot 源码与本插件位于同一上级目录时，在插件目录执行：

```powershell
../AstrBot/.venv/Scripts/python.exe -m pip install -r requirements.txt
../AstrBot/.venv/Scripts/python.exe -m pytest -q
../AstrBot/.venv/Scripts/ruff.exe check .
../AstrBot/.venv/Scripts/ruff.exe format --check .
```

源码在其他位置时，设置 `ASTRBOT_SOURCE` 指向它。测试使用真实的 AstrBot 命令过滤器和事件类，但会隔离平台发送与外部服务，不代表已完成真实聊天平台联调。
