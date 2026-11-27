<div align="center">
  <img src="./assets/icon.png" width="80" height="80" alt="">
  <h1>OCSub2WB</h1>
  <p>在 Windows 上，将 WorkBuddy 的自定义模型接入自己的 OpenCode Go 订阅。</p>
  <p><a href="https://github.com/869Nitro/OCSub2WB/releases/latest"><strong>下载 Windows x64 便携包</strong></a> · <a href="#快速开始">快速开始</a> · <a href="https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md">使用手册</a></p>
</div>

## 快速开始

准备 Windows x64、已安装并登录的 WorkBuddy，以及自己的 OpenCode Go API Key。面板可以使用 WorkBuddy 自带的 Python，项目已用 WorkBuddy 5.11.3 验证转发。[OpenCode Go 订阅与 Key](https://opencode.ai/docs/console/go/#how-it-works)

1. 在 GitHub 发布页的 **Assets** 中下载 `OCSub2WB-v2.1.0-win64.zip`，完整解压到可写目录。[v2.1.0 发布页](https://github.com/869Nitro/OCSub2WB/releases/tag/v2.1.0)
2. 打开 WorkBuddy，再运行 `OCSub2WB.exe`。让 EXE 和包内的两个 `.py` 文件保持在同一目录。
3. 在面板点 **启动转发桥**，等状态显示已启动。把 OpenCode Go Key 粘贴到 **API Key** 输入框，点 **保存**。需要本机 HTTP 代理时，先设置上游代理并点 **应用**。[上游代理说明](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#上游代理)
4. 点 **一键配置**，在 WorkBuddy 的 **设置 → 模型** 中选择导入的 OpenCode 模型，发一条消息。成功完成的模型请求会出现在面板右上方。

面板为 WorkBuddy 写入的接口地址是 `http://127.0.0.1:8787/v1`。运行时保持转发桥开启。点击右上角 **×** 会收进系统托盘，点击 **退出** 并确认会同时停止转发桥。

## 运行界面

<p align="center">
  <img src="./assets/panel.png" width="980" alt="OCSub2WB v2.1.0 实际运行界面，显示使用额度、最近三条模型请求、模型目录和日志">
</p>

这是 v2.1.0 实际运行时的截图。额度、模型目录和性能数字是当时的读数，随订阅、上游目录和请求而变化。

## 最近完成的请求

面板保留最近 **3 条**成功完成的模型请求，最新完成的一条在最上方。同一模型多次请求会各占一行。指标以单次转发请求为单位，WorkBuddy 的一轮对话可以有多次模型调用。

| 指标 | 看什么 |
| --- | --- |
| `TTFT (s)` | 从转发桥发起上游请求到收到首段非思考输出的秒数。正文和工具调用内容都算输出。 |
| `tok/s` | 按扣除已知思考 token 的输出用量，估算首段到末段之间的生成速度。首段等待和流末的用量事件不计入这段耗时。 |

非流式请求会显示模型 ID，两个流式指标显示 `—`。如果流式响应缺少必要的用量数据，仍可能显示 TTFT。具体计算和空值条件见[指标说明](https://github.com/869Nitro/OCSub2WB/blob/main/docs/usage.md#最近请求指标)。

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
