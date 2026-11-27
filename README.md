<div align="center">
  <img src="./assets/icon.png" width="80" height="80" alt="">
  <h1>OCSub2WB</h1>
  <p>在 Windows 上，将 WorkBuddy 的自定义模型接入自己的 OpenCode Go 订阅。</p>
  <p><a href="https://github.com/869Nitro/OCSub2WB/releases/latest"><strong>下载 Windows x64 便携包</strong></a> · <a href="#快速开始">快速开始</a> · <a href="https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md">使用手册</a></p>
</div>

## 快速开始

准备 Windows x64、已安装并登录的 WorkBuddy，以及自己的 OpenCode Go API Key。面板可以使用 WorkBuddy 自带的 Python，项目已用 WorkBuddy 5.11.3 验证转发。[OpenCode Go 订阅与 Key](https://opencode.ai/docs/console/go/#how-it-works)

1. 在 GitHub 发布页的 **Assets** 中下载 `OCSub2WB-v2.2.0-win64.zip`，完整解压到可写目录。[v2.2.0 发布页](https://github.com/869Nitro/OCSub2WB/releases/tag/v2.2.0)
2. 打开 WorkBuddy，再运行 `OCSub2WB.exe`。让 EXE 和包内的两个 `.py` 文件保持在同一目录。
3. 需要本机 HTTP 代理时，先设置上游代理并点 **应用**。把 OpenCode Go Key 粘贴到 **API Key** 输入框，点 **保存**，选择 **保存并启动转发桥，刷新模型列表**。只准备配置时可选 **仅保存 API Key**。[上游代理说明](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#上游代理)
4. 点 **一键配置**，在 WorkBuddy 的 **设置 → 模型** 中选择导入的 OpenCode 模型，发一条消息。成功完成的模型请求会出现在面板右上方。

面板为 WorkBuddy 写入的接口地址是 `http://127.0.0.1:8787/v1`。运行时保持转发桥开启。点击右上角 **×** 会收进系统托盘，点击 **退出** 并确认会同时停止转发桥。

## 运行界面

<p align="center">
  <img src="./assets/panel.png" width="980" alt="OCSub2WB v2.2.0 实机截图，最近转发请求显示 DeepSeek Flash 的五位数接收速率">
</p>

这是 **OCSub2WB v2.2.0** 的实机截图，右上角显示近期转发请求的接收速率和 TTFT。图中的五位数读数见下文的工具结果审查案例。

## 最近完成的转发请求

面板保留最近 **3 条**成功完成的模型转发请求，最新完成的一条在最上方。同一模型多次请求会各占一行。WorkBuddy 的一轮对话可以发起多次模型调用，经过转发桥的后台调用也会出现在表中。

| 指标 | 看什么 |
| --- | --- |
| `TTFT (s)` | 从转发桥发起上游请求到收到首段非思考输出的秒数。正文和工具调用内容都算输出。 |
| `接收 tok/s` | 按扣除已知思考 token 的输出用量，估算首段到末段之间的本机接收速率。上游集中发送时，短时间内收到多段输出，读数会偏高。 |

非流式请求会显示模型 ID，两个流式指标显示 `—`。流式响应缺少必要的用量数据、可计数输出不足两个 token 或首末输出在同一次读取中到达时，`接收 tok/s` 显示 `—`。收到首段输出后仍可显示 TTFT。具体计算和空值条件见[指标说明](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#最近请求指标)。

### 为什么对话中会出现 DeepSeek Flash 的高速记录？

**WorkBuddy 5.11.3 的工具结果审查设置只有开关。** 在这台机器的四组测试中，主对话分别使用 WorkBuddy 官方的 Hy4 Flash、DeepSeek Flash，以及经 OCSub2WB 转发的这两款模型。四组测试都观察到 DeepSeek Flash 的请求出现在转发历史里。

截图拍摄于主对话使用 WorkBuddy 官方 DeepSeek Flash 期间。本机日志显示，图中的三条转发请求被 WorkBuddy 标记为 `content_security_reviewer`，发送到了已配置的 OCSub2WB 端点。这里记录的是本次实际发生的路由。换一套模型目录或模型 ID 后，审查请求的去向需要重新验证。

本机四种对话配置均观察到这一现象：WorkBuddy 官方 Hy4 Flash、官方 DeepSeek Flash、经 OCSub2WB 转发的 DeepSeek Flash，以及经 OCSub2WB 转发的 Hy4 Flash。这是跨模型、跨对话端点复现的行为。具体是否触发审查、选到哪个端点，取决于这一轮的工具调用和 WorkBuddy 当时的模型配置。主对话和审查都用 DeepSeek Flash 时，可以在 WorkBuddy 日志中用 `chat` 和 `content_security_reviewer` 区分。

上图是使用 WorkBuddy 官方 DeepSeek Flash 对话期间的实机案例。日志中经过转发桥的三条记录属于工具结果审查，其中两条的接收速率为 **10,936.9** 和 **22,162.0 tok/s**。这类审查的回复很短，流式分块集中到达时，按首末输出到达间隔估算出的 **接收 tok/s** 会很高。这个读数衡量的是响应到达转发桥的速率，模型内部的生成速度需要上游提供相应计时。[WorkBuddy 安全中心说明](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Permission-Modes#ai-安全防护)

## 接下来怎么用

| 需要 | 操作 |
| --- | --- |
| 查看 5 小时、本周和本月的额度 | 点顶部 **刷新用量**。 |
| 确认免费模型是否可调用 | 点 **探测免费模型**，再用 **导入列表至WB** 导入付费聊天模型和本次探测通过的免费模型。 |
| 单独检查一个模型 | 在模型列表选中它，再点 **测试已选模型**。 |
| 调整窗口大小 | 拖动窗口边缘，或用标题栏的最大化和还原按钮。 |
| 查找失败原因 | 点 **打开日志目录**，也可以先看[常见问题](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#常见问题)。 |

升级、上游代理、备份恢复、配置文件和源码构建的详细步骤见[使用手册](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md)。

## 许可

OCSub2WB 是独立项目。项目代码与文档采用 [MIT](./LICENSE)，便携包所含运行库见[第三方许可](./THIRD-PARTY-NOTICES.md)。
