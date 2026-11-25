<div align="center">
  <img src="./assets/icon.png" width="88" height="88" alt="OCSub2WB">
  <h1>OCSub2WB</h1>
  <p>让 WorkBuddy 使用 OpenCode Go 订阅额度的 Windows 本地转发桥</p>
  <p>
    <a href="https://github.com/869Nitro/OCSub2WB/releases/download/v2.0.0/OCSub2WB-v2.0.0-win64.zip"><strong>下载 v2.0.0 · Windows x64</strong></a>
    &nbsp;·&nbsp; <a href="#快速开始">快速开始</a>
    &nbsp;·&nbsp; <a href="#常见问题">常见问题</a>
  </p>
</div>

## 快速开始

已验证的 WorkBuddy 版本为 `5.11.3.43806934-ecace0f7`（Windows x64）。面板复用 WorkBuddy 自带的 `pythonw.exe`，日常使用无需单独安装 Python。

1. **启动并登录 WorkBuddy。**
2. **下载并解压便携包。** 双击解压目录中的 `OCSub2WB.exe`，将同目录的两个 `.py` 文件随 EXE 一起保留。私有仓库的下载链接需要登录有访问权限的 GitHub 账号。
3. **点「启动转发桥」。** 面板显示「转发桥运行中」后即可继续。
4. **保存 OpenCode API Key。** 粘贴 `oc_sk_…` 并点「保存」。需要通过 Clash 等代理访问上游时，按[上游代理](#上游代理)设置。
5. **点「一键配置」。** 打开 WorkBuddy 的「设置 → 模型」，从 OpenCode 分组选择模型即可。面板会自动写入接口地址。

想验证某个模型时，在 **OCSub2WB 面板**选中对应行，点击「测试已选模型」。

[所有版本](https://github.com/869Nitro/OCSub2WB/releases) · [v2.0.0 SHA-256 校验文件](https://github.com/869Nitro/OCSub2WB/releases/download/v2.0.0/OCSub2WB-v2.0.0-win64.zip.sha256)

### 系统还原后

如果系统还原移除了 WorkBuddy，先重新安装并登录。若便携目录和 `.env` 仍在，打开 OCSub2WB，启动转发桥，再点「一键配置」。已保存的 Key 可以直接沿用。

## 日常操作

| 操作 | 用途 |
| --- | --- |
| **一键配置** | 从目录重建 OCSub2WB 托管的聊天模型条目，包含免费模型，保留自有模型。 |
| **探测免费模型** | 按当前路由探测每个免费模型，显示可用状态或失败原因。 |
| **导入列表至WB** | 重建托管条目，导入目录中的付费聊天模型，并只纳入探测返回 HTTP 200 的免费模型。 |
| **测试已选模型** | 手动测试一个模型，显示转发桥、目录、普通对话、流式对话和计费信息。 |
| **刷新模型状态** | 刷新模型表和 WorkBuddy 导入标记。目录可能来自缓存，此操作只读。 |
| **清空WB列表** | 备份后移除托管条目，保留自有模型。转发桥未启动时也可以执行。 |

表中前五项需要转发桥运行。点表头可排序，表格文字可选中并用 Ctrl+C 复制。模型表通过「规则」列显示免费、付费、训练、锁区及 ZDR 标记。单模型测试最多发送两次小型对话请求，普通对话失败时会跳过流式测试。

面板另有 **额度** 和 **日志**。额度显示 5 小时、本周、本月用量与重置时间，转发桥停着也能刷新。日志可查看最近 250 行，每 6 秒刷新。

窗口的 `×` 会收起到系统托盘，单击托盘图标即可恢复。选择「退出并停止转发桥」可同时关闭面板和转发桥。

## 上游代理

**`8787` 是 WorkBuddy 连入 OCSub2WB 的端口。** 上游代理地址用于 OCSub2WB 访问 OpenCode Go，例如 `http://127.0.0.1:7897`，这里应填写本机代理实际提供的地址。一键配置给 WorkBuddy 写入的是 `http://127.0.0.1:8787/v1`。

| 模式 | 模型请求如何发出 |
| --- | --- |
| **直连** | 由转发桥直接访问 OpenCode Go。 |
| **自动** | 仅目录标记为锁区的模型走已配置的代理，其余模型直连。 |
| **手动** | 所有模型请求都走已配置的代理。 |

地址格式为 `http://IPv4:端口`。切换「直连」或「自动」会尝试应用当前设置，修改地址后可点「应用」。面板会先验证连接，再保存到 `.env`。在「自动」模式下，锁区模型缺少可用代理时，转发桥会返回 `502 ProxyRequired`。

系统级 TUN 仍可接管直连流量。转发桥自己的模式由面板或 `.env` 决定。

## 常见问题

| 现象 | 怎么处理 |
| --- | --- |
| 转发桥未运行或 `Connection refused` | 在 OCSub2WB 点「启动转发桥」，确认状态显示运行中。 |
| `400 MissingSessionID` | 检查该模型是否经转发桥发送。在面板重新运行「一键配置」可恢复默认接口地址。 |
| `403 EntitlementError` | 检查 Key 是否属于有有效 OpenCode Go 订阅的工作区。 |
| `403 region` 或 `502 ProxyRequired` | 检查 OpenCode Console 的工作区隐私与区域设置，以及面板的上游代理模式和地址。 |
| 免费模型提示 `opt-in` | 上游要求该工作区启用相应训练选项。[配置与开发](#配置与开发)中的免费模型快照记录了当时的表现。 |
| 额度显示读取失败 | 点「刷新额度」。上游短暂失败时，面板会保留最近一次可用结果并标记异常。 |
| `models_state=recovered` 或写入返回 `409` | 在面板确认从备份恢复，再重新运行「一键配置」。 |
| 任务报 `job error` | 若转发桥刚重启，旧任务记录已失效，重新执行该操作即可。 |
| 安装或升级 WorkBuddy 后转发桥启动失败 | 检查便携目录里的两个 `.py` 文件，以及 WorkBuddy 自带的 `pythonw.exe` 是否仍可找到。 |

需要看转发详情时，从面板打开缓存目录，查看 `cache\logs\proxy-YYYYMMDD.log` 中的路由、状态码和耗时。

## 配置与开发

<details>
<summary><strong>模型命名、能力字段与免费模型</strong></summary>

显示名使用上游目录的完整 `name`，网关 `id` 原样写入。付费前缀是 `[OC] `，免费前缀由它派生为 `[OC-FREE] `。同名前缀下出现重复显示名时，末尾会追加 `（<id>）`。

| WorkBuddy 中的名称示例 | 模型 ID |
| --- | --- |
| `[OC] DeepSeek V4.1 Flash 1017` | `deepseek-v4.1-flash` |
| `[OC] GLM-5.3` | `glm-5.3` |
| `[OC-FREE] Beam Free` | `beam-free` |
| `[OC-FREE] Hy4 flash Free` | `hy4-flash-free` |

如需更改前缀，可在 `.env` 中配置。引号内的末尾空格会保留。

```dotenv
OPENCODE_NAME_PREFIX="[OC] "
```

| 能力 | 字段来源 |
| --- | --- |
| 上下文 / 最大输出 | `limit.context` / `limit.output`。目录缺失时面板显示 `—`，模型条目省略该字段。 |
| 免费 / 付费 | 检查 `cost.text` 的输入、输出和缓存读取价格。已知正价判为付费。价格不足以判断时检查 `-free` 后缀。 |
| 图片 / 工具调用 | `modalities.input` 中的 `image/*` / `capabilities.tools.supported`。 |
| 思考 | `capabilities.reasoning`。允许切换时写 `canDisableThinking: true`。明确强制思考时写 `canDisableThinking: false` 和 `onlyReasoning: true`。有档位但目录未声明能否切换时，省略这两个开关字段。 |

默认思考档位是 WorkBuddy 支持的最高档。`budget` 是 token 上限，面板的 `on-off` 和 `Fixed` 是显示标签。自检会按目录选择 `none`、省略 `reasoning_effort` 或使用支持的最低档。模型能否真正停止思考取决于上游。

**免费模型历史快照（2026-11）。** 当时工作区使用 Regions=Global 并开启了免费端点训练。

| 模型 | 直连 | 走代理 | 当时的结果 |
| --- | --- | --- | --- |
| `beam-free` | 200 | 200 | `cost=0`，`allowance=0` |
| `hy4-flash-free` | 200 | 200 | `cost=0`，`allowance=0` |
| `iceman-contributor-free` | 400 | 400 | 上游要求开启付费端点训练 |

结果随模型目录和工作区隐私设置变化。开启付费端点训练可能使付费模型的请求也被用于训练。

</details>

<details>
<summary><strong>配置文件、备份与恢复</strong></summary>

| 位置 | 用途 |
| --- | --- |
| 便携目录的 `.env` | OpenCode API Key、上游代理模式和地址、模型名前缀。参考 [`.env.example`](./.env.example)。 |
| `%USERPROFILE%\.workbuddy\models.json` | WorkBuddy 自定义模型。面板保存新 Key 时会校验 Key，并同步更新托管条目。 |
| 便携目录的 `cache\` | 日志、模型配置备份、面板控制令牌和转发桥 PID。 |

Key 保存在本机的 `.env` 和托管模型条目中，均为明文。`.env` 和 `cache/` 已列入 Git 忽略清单。启动转发桥后才可通过面板保存新 Key。

清空和重建时，`vendor=OpenCode` 或 URL 指向本机 `8787` 的条目会被识别为托管模型。改写前会备份原文件，再通过临时文件替换。`cache/backup/` 保留最近 20 份备份。读改写使用进程内锁。

如果 `models.json` 损坏，转发桥会尝试从最近的合法备份只读加载，并暂停相关写入。确认恢复时会先保留损坏的原件为 `models.json.corrupt.<时间>`，再替换配置文件。

旧版 `.env` 只记录 `OPENCODE_UPSTREAM_PROXY=auto` 时，兼容地址为 `http://127.0.0.1:7897`。旧值直接记录 URL 时按手动模式读取。面板保存时使用 `OPENCODE_UPSTREAM_PROXY` 和 `OPENCODE_UPSTREAM_PROXY_URL`。

</details>

<details>
<summary><strong>转发原理与控制接口</strong></summary>

WorkBuddy 的自定义模型通过本机转发桥访问 `opencode.ai/inference/go`。转发桥为请求补齐 `x-opencode-session`，统一 `User-Agent`，再按请求路径转给 OpenAI Chat Completions、Anthropic Messages 或 Responses 接口。WorkBuddy 的自定义模型配置使用 `POST /v1/chat/completions`。排查 `403 Cloudflare 1010` 时，可检查转发请求是否出现重复的 `User-Agent`。

```text
WorkBuddy ──► 127.0.0.1:8787 ──► OpenCode Go
                OCSub2WB
```

面板和自动写入的模型地址都使用 `8787`。Python 脚本的 `--port` 参数可以改监听端口，面板连接地址和一键配置写入的 URL 仍使用 `8787`。

控制接口使用行文本协议。`/api/*` 要求 `X-OCGO-Token` 并拒绝带 `Origin` 的请求。令牌在启动时生成，保存在 `cache\api.token`。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/status` | 读取版本、PID、监听端口、代理模式、模型及恢复状态。 |
| `GET /api/quota?refresh=1` | 强制刷新三档额度。面板的独立额度查询也可在停桥时运行。 |
| `GET /api/models` | 读取模型目录、能力、规则和导入标记。 |
| `POST /api/refresh?reset=1` | 重建托管模型。`free=all` 或 `free=id1,id2` 指定免费模型，结果包含 ID 冲突信息。 |
| `POST /api/workbuddy/purge` | 清空托管模型。 |
| `POST /api/models/restore` | 从最近的合法备份恢复模型配置。 |
| `POST /api/selftest` | 启动单模型自检，参数为 `model=<id>`。 |
| `POST /api/probe` | 启动免费模型探测。 |
| `GET /api/job/<id>` | 查询自检或探测任务，最近保留 20 个终态。 |
| `POST /api/key` | 校验并保存 Key，参数为 `key=oc_sk_…`。 |
| `POST /api/upstream-proxy` | 设置出站方式，传入 `mode`（`off`、`auto` 或 `manual`）及 `url`。 |
| `GET /api/log?lines=200` | 读取日志尾部。行数限定在 10–2000，无法解析为整数时返回 `400`。 |
| `POST /api/shutdown` | 先返回 `200`，随后异步停止转发桥。 |

WorkBuddy 访问的 `/v1/models` 使用 10 分钟响应缓存，命中时可在日志中看到 `cached`。

</details>

<details>
<summary><strong>本地构建与发布</strong></summary>

需要 MinGW-w64（GCC 16.2 或兼容版本）和可从命令行调用的 Python。构建脚本依次查找 `MINGW` 指定的工具目录、`PATH` 和 `D:\dev\mingw64\bin`。请先退出正在运行的面板，释放 EXE 文件。

```bat
cd panel
build.bat
```

脚本先用 `windres` 编译资源，再用 `g++` 生成 `..\OCSub2WB.exe`，最后执行 [`patch_manifest.py`](https://github.com/869Nitro/OCSub2WB/blob/main/panel/patch_manifest.py) 替换 Windows 清单。窗口类、互斥体与清单中的历史名称用于兼容现有资源。CI 使用 `build.bat --ci`。

[发布工作流](https://github.com/869Nitro/OCSub2WB/blob/main/.github/workflows/build-release.yml) 在 Windows 上构建 EXE、检查 Python 语法和运行库依赖，并生成便携 ZIP 与 SHA-256 校验文件。手动触发时产物上传到 Actions，发布 GitHub Release 后校验文件和 ZIP 会作为发行资产上传。上传依赖工作流的 `contents: write` 权限。

| 源码 | 用途 |
| --- | --- |
| [`opencode-go-proxy.py`](./opencode-go-proxy.py) | 本机转发和控制接口。 |
| [`ocgo_common.py`](./ocgo_common.py) | Key、模型配置、上游目录、额度与自检。 |
| [`panel/`](https://github.com/869Nitro/OCSub2WB/tree/main/panel) | Windows 面板源码、图标、清单和构建脚本。 |

</details>
