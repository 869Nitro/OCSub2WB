<div align="center">
  <img src="./assets/icon.png" width="80" height="80" alt="">
  <h1>OCSub2WB</h1>
  <p>在 Windows 上，将 WorkBuddy 的自定义模型接入自己的 OpenCode Go 订阅。</p>
  <p><a href="https://github.com/869Nitro/OCSub2WB/releases/latest"><strong>下载 Windows x64 便携包</strong></a> · <a href="#快速开始">快速开始</a> · <a href="https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md">使用手册</a></p>
</div>

## OCSub2WB 解决了什么问题？

OCSub2WB 是一个 Windows 本机转发桥。WorkBuddy 把自定义模型请求发到本机，OCSub2WB 再把请求发给 OpenCode Go。配置完成后，你可以继续在 WorkBuddy 里选模型、发消息和使用工具。

OpenCode Go 的对话请求需要携带它能识别的稳定会话标识，供上游优化会话路由和复用提示词缓存。OCSub2WB 优先沿用请求里的 `x-opencode-session`。默认模式下，缺少这个头时，转发桥会尝试用请求里第一条有文本的用户消息生成标识。推导不到时使用预设值，并把标识作为 `x-opencode-session` 发给 OpenCode Go。 [OpenCode 会话标识说明](https://opencode.ai/docs/console/harnesses#sessions)

面板还能批量导入和更新聊天模型，写入模型 ID、上下文与输出上限、工具调用及思考设置，并保留你自行添加的其他模型。你也可以查看订阅用量、探测免费模型、查看近期转发记录，以及为 OpenCode Go 单独设置上游 HTTP 代理。

## 快速开始

准备 Windows x64、已安装并登录的 WorkBuddy，以及自己的 OpenCode Go API Key。面板可以使用 WorkBuddy 自带的 Python。[OpenCode Go 订阅与 Key](https://opencode.ai/docs/console/go/#how-it-works)

**实测范围**　已用 WorkBuddy 5.11.3 和 Go 基础档验证转发。Go Plus 和 Go Teams 与基础档共用推理和用量接口，按公开接口判断可沿用相同的接入方式，这两个套餐尚未实测。使用 Go Teams 时，请填在团队工作区创建的成员个人 API Key。 [OpenCode Go 套餐与用量接口](https://opencode.ai/docs/console/go/#usage)

1. 在 GitHub 发布页的 **Assets** 中下载 `OCSub2WB-v2.2.1-win64.zip`，完整解压到可写目录。[v2.2.1 发布页](https://github.com/869Nitro/OCSub2WB/releases/tag/v2.2.1)
2. 打开 WorkBuddy，再运行 `OCSub2WB.exe`。让 EXE 和包内的两个 `.py` 文件保持在同一目录。
3. 需要本机 HTTP 代理时，先设置上游代理并点 **应用**。把 OpenCode Go Key 粘贴到 **API Key** 输入框，点 **保存**，选择 **保存并启动转发桥，刷新模型列表**。只准备配置时可选 **仅保存 API Key**。[上游代理说明](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#上游代理)
4. 点 **一键配置**，在 WorkBuddy 的 **设置 → 模型** 中选择导入的 OpenCode 模型，发一条消息。成功完成的模型请求会出现在面板右上方。

面板为 WorkBuddy 写入的接口地址是 `http://127.0.0.1:8787/v1`。运行时保持转发桥开启。点击右上角 **×** 会收进系统托盘，点击 **退出** 并确认会同时停止转发桥。

## 运行界面

<p align="center">
  <img src="./assets/panel.png" width="980" alt="OCSub2WB v2.2.1 实机截图，最近转发请求表显示 Tok/s、Δt 和 TTFT，下方显示模型探测结果">
</p>

这是 **OCSub2WB v2.2.1** 的实机截图。右上角显示最近的转发记录，下方模型列表显示上下文、输出上限、思考设置和探测结果。

## 最近完成的转发请求

面板保留最近 **3 条**成功完成的模型转发请求，按完成时间从新到旧排列。同一模型多次请求会各占一行。

| 指标 | 看什么 |
| --- | --- |
| `TTFT (s)` | 从转发桥发起上游请求到收到首段非思考输出的秒数。正文和工具调用内容都算输出。 |
| `Tok/s` | 按扣除已知思考 token 的输出用量，估算首段到末段之间的本机接收速率。上游集中发送时，短时间内收到多段输出，读数会偏高。 |
| `Δt` | 首段到末段输出的接收间隔，用于计算旁边的 Tok/s。短间隔用毫秒显示。 |

非流式请求会显示模型 ID，三个流式指标显示 `—`。流式响应缺少必要的用量数据或可计数输出不足两个 token 时，`Tok/s` 显示 `—`。首末输出在同一次读取中到达时，`Tok/s` 和 `Δt` 都显示 `—`。收到首段输出后仍可显示 TTFT。

### 为什么一轮对话会出现多条记录？

WorkBuddy 的「工具内容注入防护」会检查工具返回的内容。启用后，同一轮任务里除了主对话请求，还可能产生工具结果审查请求。OCSub2WB 按经过转发桥的模型请求逐条记录，因此表中的多条记录可能来自同一轮任务。 [WorkBuddy 安全中心说明](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Permission-Modes#ai-安全防护)

## 接下来怎么用

| 需要 | 操作 |
| --- | --- |
| 查看 5 小时、本周和本月的额度 | 点顶部 **刷新用量**。 |
| 确认免费模型是否可调用 | 点 **探测免费模型**，再用 **导入列表至WB** 导入付费聊天模型和本次探测通过的免费模型。 |
| 单独检查一个模型 | 在模型列表选中它，再点 **测试已选模型**。 |
| 调整窗口大小 | 拖动窗口边缘，或用标题栏的最大化和还原按钮。 |
| 查找失败原因 | 点 **打开日志目录**，也可以先看[常见问题](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#常见问题)。 |

升级、上游代理、备份恢复、配置文件和源码构建的详细步骤见[使用手册](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md)。

## 如果 WorkBuddy 官方支持会话标识，还需要 OCSub2WB 吗？

如果将来 WorkBuddy 的自定义模型请求能提供 OpenCode Go 认可的稳定会话标识，而且你用自己的 API Key 和接口地址实测直连成功，只用一两个常用模型时，可以优先在 WorkBuddy 的自定义模型设置里直连 OpenCode Go。 [OpenCode 直连接入说明](https://opencode.ai/docs/console/harnesses)

如果你经常导入或更换一批模型，想探测免费模型、集中看订阅用量和近期请求，或者需要给 OpenCode Go 单独设代理，仍可使用 OCSub2WB。当前的「一键配置」会把托管模型指向本机转发桥，近期请求表也只记录经过它的请求。

## 数据处理与隐私

- **请求发往哪里。** 默认转发桥监听本机回环地址。WorkBuddy 发到桥上的模型请求会转发给 OpenCode Go，内容可能包含对话消息和工具返回的数据。启用上游代理时，请求经你配置的代理连接 OpenCode Go。数据保留和训练规则以所选模型及工作区设置为准。 [OpenCode Go 隐私说明](https://opencode.ai/docs/console/go/#privacy)
- **API Key 存在哪里。** Key 以**明文**保存在便携目录的 `.env` 和 WorkBuddy 的托管模型配置中。模型配置备份也可能包含 Key。
- **日志记录什么。** 本机转发日志会记录请求路径、模型 ID、会话标识、状态码和耗时等排查信息。公开反馈问题时，请先从日志、配置和截图中移除 Key、会话标识等敏感信息。 [本地文件与数据](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#本地文件与数据)

## 贡献者

- [869Nitro](https://github.com/869Nitro) 发起并维护项目，负责需求设计、实机测试与发布。
- **WorkBuddy**（AI 助手）完成了 v2.0.0 及更早版本的代码实现与发布。
- **OpenAI Codex**（AI 编程助手）负责代码实现与自动化测试，协助排查问题和编写文档。

## 许可

OCSub2WB 是独立项目。项目代码与文档采用 [MIT](./LICENSE)，便携包所含运行库见[第三方许可](./THIRD-PARTY-NOTICES.md)。
