# OCSub2WB

让 WorkBuddy 用上 OpenCode Go 订阅额度，并带一个原生 Windows 托盘/控制面板。

- 入口：`OCSub2WB.exe`（Win32，约 590KB，仓库包含）
- 转发层：`opencode-go-proxy.py`，纯 Python 标准库，只监听 `127.0.0.1:8787`
- 配置：`.env`（Key / 上游代理）+ `%USERPROFILE%\.workbuddy\models.json`（模型条目）
- 运行期文件：`cache\`（日志、备份、token、pid）；`.env` 和 `cache/` 不入库
- 当前版本：2.0；默认 UA：`workbuddy-opencode-proxy/2.0`

> exe 不内嵌 Python，靠 WorkBuddy 自带的 `pythonw` 拉起 `opencode-go-proxy.py`，
> 所以两个 `.py` 不能删。界面与文档统一称 OCSub2WB。
>
> `OpenCodeGoPanel`（窗口类/互斥体）和 manifest 的 `OpenCodeGo.Panel` 是历史内部名，
> 为了单实例和资源兼容保持不动，不是对外名称。
>
> 上游路径固定为 `/inference/go`，与套餐无关，本文档不讨论套餐。

## 为什么需要转发层

网关要求请求带 `x-opencode-session`，否则 `400 MissingSessionID`。
WorkBuddy 自定义模型只能填「模型名称 / 接口地址 / API Key」，没有自定义请求头入口。
所以由本地转发桥补头，其余字节原样转发：

```
WorkBuddy ──POST /v1/chat/completions──▶ 127.0.0.1:8787
                                            │ + x-opencode-session
                                            │ + user-agent
                                            ▼
                     https://opencode.ai/inference/go/openai/v1/chat/completions
```

## 安装与首次配置

1. 双击 `OCSub2WB.exe`。
2. 点「启动转发桥」；Key 为空也能先启动，状态会显示 `● 转发桥运行中`。
3. 选代理模式：直连可跳过；自动/手动填写 `http://IPv4:端口` 后点「应用」。
4. 粘贴 `oc_sk_...` 到 Key 输入框，点「保存」。
5. 点「一键配置」写入 WorkBuddy 模型。
6. WorkBuddy → 设置 → 模型 → 选 OpenCode 分组，可用「测试已选模型」验证。

重启还原后：打开 exe → 启动转发桥 → 「一键配置」→ 选中模型点「测试已选模型」。

重置只清 OC 托管条目时，自有模型会保留；`.env`、`cache/` 已在 `.gitignore` 中，
不要提交。

## 面板

| 区域 | 功能 |
|---|---|
| 顶部 | 运行状态、端口、上游模式、Key 掩码、pid；启动/停止转发桥、打开缓存目录、退出 |
| 额度 | 左侧 5 小时 / 本周 / 本月标签，右侧只显示百分比与重置时间；面板按 `.env` 的 Key 直接请求上游，转发桥没启动也能看 |
| 模型 | 实时目录（当前约 31 个 chat，以实时目录为准）；「规则」列标注免费/付费、训练、锁区、ZDR；点任意表头排序 |
| 模型操作 | `一键配置`、`探测免费模型`、`导入列表至WB`、`测试已选模型`、`刷新模型状态`、`清空WB列表` |
| Key | 粘贴新 `oc_sk_...` → 校验 → 写 `.env` 并改写全部托管条目，WorkBuddy 热加载生效 |
| 上游代理 | 直连 / 自动（仅锁区走代理）/ 手动（全部走代理） |
| 日志 | 尾 250 行，6 秒刷新 |

### 模型表

- 列宽自动计算：按表头（含排序箭头）和当前单元格内容取最大宽度，再按 DPI 加内边距。
- 模型 id 是弹性列：其余列优先完整显示；空间不足时 id 先压到只够显示「模型 id」，
  仍放不下则保持其它列完整并出现横向滚动条。
- 点表头排序：文本列按拼音，上下文/输出按数值，档位按档位数量；再点同列切换升降序，
  探测列空值始终排最后。选中行按 id 保留，「测试已选模型」不会测错行。
- 表格文字可左键拖选，Ctrl+C 复制；单元格之间 Tab、行之间换行，粘进 Excel 自动分列。

### 六个模型按钮

前五个依赖转发桥：未启动时置灰，鼠标悬停提示「需启动转发桥」。
`清空WB列表` 不依赖转发桥——它只改 WorkBuddy 的 models.json，转发桥没起时会调用
一次性本地进程完成，写前同样备份。

「模型」分组标题显示 `N 托管 / N 可选 / N 免费`。六个模型操作按钮产生的临时结果/进度
显示在按钮行右侧，保留到下一次按钮操作，不会被状态轮询、模型刷新或自动错误覆盖；
代理、Key、启停等其它操作仍走普通提示，可随时更新。

**刷新模型状态**：只读 `GET /api/models`，重新拉上游目录并读取 WorkBuddy 的导入标记，
刷新面板表格；它不写 models.json、不导入模型、不刷新下游 WorkBuddy。
真正会改 WB 的是「一键配置」「导入列表至WB」「清空WB列表」。

**测试已选模型（只手动、单模型）**：先在列表里选中一行（不选则测第一行），点「测试已选模型」。
只测这一个模型，最多 2 次很小的请求：非流式 + 流式；`chat` 不通就不再打 `stream`。
结果逐项显示 `relay / catalog / chat / stream / billing` 的通过/失败/跳过与原因。

**自检**按目录下发的思考能力取最低档：原始 effort 含 `none` 就直接发 `none`；
可切换思考就省略 `reasoning_effort`；只能思考则取支持的最低档（minimal→low→…）。
自检与正在进行的对话并发执行，不会中断或阻塞输出；不会巡检整个目录，也没有定时自检。

**清空WB列表**：删除全部托管条目（`vendor=OpenCode` 或 `url=127.0.0.1:8787`），
自有模型保留，不重建。平时用「一键配置」即可，它内部也是先删后写、一次落盘。

### 额度

面板直接读 `.env` 的 `OPENCODE_API_KEY` 请求 `inference/go/v1/usage`，不经过转发桥。
直连失败再按 `OPENCODE_UPSTREAM_PROXY`（off/auto/URL）回退。
错误会区分 `未配置 API Key` / `Key 无效` / `无有效订阅` / `网络不通`；
有上次数据时保留并在前面标 ⚠。

### 退出

`X` 收起到托盘（不退出进程）；托盘菜单可「隐藏/打开面板」或「退出并停止转发桥」；
`[退出]` 二次确认后停桥，并等端口真正释放（≤3s）才关窗。

托盘图标属于面板进程，所以不提供“只退面板、保留托盘”的选项——需要保留托盘就用隐藏。

## 系统托盘

- 单击托盘图标恢复面板；右键菜单：隐藏/打开面板、启动/停止转发桥、
  代理模式（直连/自动/手动）、退出并停止转发桥。
- 鼠标悬浮显示转发状态：运行中/未运行、端口、当前代理模式。
- 第一次点 `X` 会弹一次「已收起到托盘」，之后静默。
- Explorer 重启后图标自动重建。

## 模型命名

付费与免费各一套前缀，免费自动派生，不需要额外配置：

```
[OC] DeepSeek V4.1 Flash 1017    id: deepseek-v4.1-flash
[OC] GLM-5.3                     id: glm-5.3
[OC-FREE] Beam Free              id: beam-free
[OC-FREE] Hy4 flash Free         id: hy4-flash-free
```

名称直接用上游 `name`，不删末尾数字、不拼接训练/锁区备注；训练与锁区看面板「规则」列。
若加上前缀后仍然完全同名（同付费状态），才在名称末尾追加 `（<id>）` 区分。

派生规则：在基础前缀的第一个 `]` 前插入 `-FREE`（`[OC] ` → `[OC-FREE] `）。
前缀可用 `.env` 覆盖，**必须带引号**否则尾随空格会被吃掉：

```
OPENCODE_NAME_PREFIX="[OC] "
```

`id` 永远是网关 id，前缀只影响显示。

## 字段来源

models.json 的能力字段来自 OCG 目录声明，**不做行为探测**：

| 字段 | 来源 |
|---|---|
| 上下文 / 最大输出 | `limit.context` / `limit.output`；缺失时面板显示 `—`，不写 0 |
| 免费 / 付费 | `cost.text.input/output`（以及 `cache.read`）是否全为 0；cost 缺失才看 `-free` |
| 图片 | `modalities.input` 含 `image/*`（WorkBuddy 只有图片开关，PDF 无处可放） |
| 工具调用 | `capabilities.tools.supported` |
| 协议 / 端点 | 转发桥统一走 OpenAI Chat Completions，网关按模型翻译，不写进条目 |

### 思考能力

`capabilities.reasoning` 按三态处理：

- 明确 `toggle=true`，或 `effort` 含 `none/off`：可切换，写
  `supportedEfforts` + `canDisableThinking:true`。
- 有 `effort` 但缺 `toggle`：视为未知，**省略** `canDisableThinking`/`onlyReasoning`，
  由 WorkBuddy 默认显示开关。
- 无 `effort` 且无 `toggle`：强制思考，写 `supportedEfforts: []` +
  `canDisableThinking:false` + `onlyReasoning:true`。
- 有 effort 时 `defaultEffort` 取最高档；`budget` 只是 token 上限，不作为档位。

面板显示：

- 思考列：`强制` / `切换` / `无`
- 档位列：有 effort 显示列表；可切换但无 effort 显示 `on-off`；
  强制/无思考显示 `Fixed`
- `on-off` / `Fixed` 仅面板显示，不写入 models.json，也不发给 WorkBuddy

## 免费模型

实测快照（2026-11，工作区 Regions=Global、免费端点训练开）：

| 模型 | 直连 | 走代理 | 计费 |
|---|---|---|---|
| `beam-free` | 200 | 200 | cost=0，allowance=0 |
| `hy4-flash-free` | 200 | 200 | cost=0，allowance=0 |
| `iceman-contributor-free` | 400 | 400 | 需「允许付费端点基于请求数据训练」 |

> 快照只说明当时的观察，随工作区隐私设置和上游目录变化可能失效，不构成保证。

官方“免费档对外部客户端一律 403”已经不成立，实际取决于工作区隐私设置。
`iceman` 标价 0，但 API 的 400 明确要求打开的是**付费**训练开关；只开「免费端点训练」仍会 400。
打开它意味着付费模型请求也可能被用于训练。

`探测免费模型` 按当前路由只测一侧（直连模式测直连；手动测代理；自动按模型是否锁区决定），
「探测」列直接给结论：`可直连` / `代理可用` / `不可用·具体原因`（Key无效、需开Global、
地区受限、需开付费/免费训练、锁区需代理、限流等）；要求训练授权的模型，可用结论会追加 `·需训练`。
`导入列表至WB` 只导入实际路由 200 的模型。

## 上游代理

这里说的是 Clash / v2rayN 这类网络代理，和本机的「转发桥」不是一回事。三种模式：

> 注意：当代理软件处于 TUN 模式时，即使这里选「直连」，请求仍可能被上游代理接管。

| 模式 | 行为 |
|---|---|
| 直连 | 所有模型请求直连 |
| 手动 | **所有**模型请求走地址框代理 |
| 自动 | **仅锁区模型**（目录 `go.limited_regions=true`）走地址框代理，其余直连 |

- 点「直连」/「自动」立即生效；点「手动」改完地址再点「应用」。自动模式不再扫描本机端口。
- 地址框只在直连模式置灰；格式固定 `http://IPv4:端口`，只能用数字、点、冒号、斜杠和 `http://`
  这些字符，域名/账号密码/空格会被拒。
- 应用时先验证：有桥走 CONNECT（3 秒），无桥走 TCP（2 秒）；失败在地址框内标红并弹出提示，
  不保存。自动模式下锁区模型没有可用代理会直接返回 502 中文错误，不静默直连。
- 转发桥没启动时也可以改：直接写 `.env` 的 `OPENCODE_UPSTREAM_PROXY`（off/auto/manual）与
  `OPENCODE_UPSTREAM_PROXY_URL`，提示「已保存，启动转发桥后生效」。

旧 `.env` 的 `OPENCODE_UPSTREAM_PROXY=auto` 无 URL 时默认用 `http://127.0.0.1:7897`；
旧值直接是 URL 时自动迁移为手动模式。进程环境里的 `HTTP_PROXY`/`HTTPS_PROXY`
**不参与**判断（那是 Agent 沙箱注入的）。

## models.json 损坏与恢复

- 读取到非法 JSON/非数组时，面板/接口只读回退 `cache/backup/models.json.bak.*` 里最新合法备份。
- 回退状态写入 `/api/status` 的 `models_state=recovered`；写操作会被拒绝并返回 409，
  不会拿旧备份覆盖损坏文件。
- 面板首次检测到 recovered 会确认是否写回；确认后调用 `POST /api/models/restore`。
- 恢复时当前损坏文件复制为 `models.json.corrupt.<时间>`，再用临时文件原子替换；
  失败时原文件仍在。
- `cache/backup` 只保留最近 20 份；进程内用 RLock 保护读改写，不是跨进程文件锁。
- `/api/key` 在 models.json 损坏且无有效备份时返回文本 409/500，不会断连。

## 控制接口

面板通过 `127.0.0.1:8787/api/*` 操作，行文本协议，全部要求 `X-OCGO-Token`，
并拒绝带 `Origin` 的请求。令牌每次启动重新生成，写在 `cache\api.token`。

```
GET  /api/status            S: ver/proxy_ver/pid/port/mode/proxy_url/upstream/
                            key/models/models_state/requests/uptime
GET  /api/quota[?refresh=1] Q/R: 三档额度
GET  /api/models            M: 目录 + free/imported/思考形态/规则
POST /api/refresh?reset=1   R: 一键配置（body: free=all 或 id1,id2）
                            R 含 conflicts=N ids=...
POST /api/workbuddy/purge   P: 清空WB列表托管条目
POST /api/models/restore    M: 从最新备份恢复 models.json
POST /api/selftest          J(202): 单模型自检（body: model=<id>）
POST /api/probe             J(202): 免费模型探测
GET  /api/job/<id>          J/T/F: 轮询任务；仅保留最近 20 个终态
POST /api/key               K: body: key=oc_sk_...
POST /api/upstream-proxy    U: body: mode=off|auto|manual + url=...
GET  /api/log?lines=200     T: 日志尾巴；lines 10-2000，非法返回 400
POST /api/shutdown          S: 先回 200，再异步退出
```

`/v1/models` 有 10 分钟缓存：WorkBuddy 每 10 分钟用它巡检自定义模型，
缓存后不再打网关（日志里标记 `cached`）。

## 构建面板

```bat
cd panel
build.bat
```

依赖 `D:\dev\mingw64`（GCC 16.2）。步骤：

1. `windres app.rc` 生成 `app_res.o`（图标资源 ID 1）。
2. g++ 编译 `ocgo_panel.cpp` + `app_res.o`，输出 `..\OCSub2WB.exe`。
3. `patch_manifest.py` 通过 `UpdateResourceW` 替换资源 24/1。

mingw 的 specs 会强制注入默认清单，所以不能直接用 windres 写 manifest，否则会报
"multiple non-default manifests"。构建前先退出正在运行的 `OCSub2WB.exe`，否则 exe 被占用。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 面板 `○ 转发桥未运行` | 转发桥没起 | 点「启动转发桥」 |
| `Connection refused` | 同上 | 同上 |
| `400 MissingSessionID` | 直连了网关 | 接口地址必须是 `http://127.0.0.1:8787/v1` |
| `403 Cloudflare 1010` | 重复请求头被判伪造指纹 | 头名统一小写后再覆盖，别改回去 |
| `403 EntitlementError` | 无有效订阅 / Key 不属于该工作区 | 面板里换 Key |
| 免费模型 400 `opt-in` | 需要付费训练开关 | 见上文，自行权衡 |
| `403 region` | DeepSeek / LongCat 需 Regions=Global | Console → 隐私 → 全球 |
| 额度显示 `读取失败` | 上游瞬时失败 | 点「刷新额度」，已内置一次重试 |
| `models_state=recovered` / 409 | models.json 损坏，已回退备份 | 面板确认恢复，或 `POST /api/models/restore` |
| 面板操作报 `job error` | 转发桥重启过，旧 job 失效 | 面板会自动丢弃，重新操作即可 |
| 面板起不来 | Python 或权限问题 | 手动 `python opencode-go-proxy.py` 看报错 |

日志：`cache\logs\proxy-YYYYMMDD.log`，一行一请求，含状态码、字节、耗时、会话 ID。

## 限制

- 转发桥没起时该模型报连接失败，其它内置模型不受影响。
- 转发桥不存 Key、不写 Key 到日志；`.env` 与 models.json 里是明文（本机自用）。
- C 盘重启还原后 models.json 会丢，打开面板点「一键配置」即可；`D:\dev` 与 `.env` 不受影响。
- 不注册服务、不写全局环境变量、不开机自启。
- 思考强度按模型各自的能力表下发；WorkBuddy 里关掉思考只是不发 `reasoning_effort`，
  对“默认必思考”的模型不等于真停止；上游缺 toggle 时按未知处理，不保证能硬关。
- 自检只覆盖“这一个模型能不能用”，不验证路径容错/重复实例等系统项。
- 额度读取与「清空WB列表」不依赖转发桥；**保存新 Key 仍需要转发桥在跑**。
- 转发桥未启动时清空由一次性 `pythonw` 进程完成，仍会先备份 models.json，再原子替换。
- TUN 模式下系统级代理可能接管“直连”请求；面板只能控制本转发桥的出站代理。
- Windows 单用户环境，`.env`/models.json 是明文，未做 ACL 加固。

## 文件

| 文件 | 作用 |
|---|---|
| `OCSub2WB.exe` | 唯一入口（仓库包含） |
| `opencode-go-proxy.py` | 转发 + 控制接口 |
| `ocgo_common.py` | 唯一写盘者：env、上游代理探测、目录、models.json、额度、探测、自检 |
| `panel/` | 面板源码、`app.rc`、图标、`build.bat`、manifest patch |
| `.env` | `OPENCODE_API_KEY`、`OPENCODE_UPSTREAM_PROXY`、`OPENCODE_UPSTREAM_PROXY_URL`、`OPENCODE_NAME_PREFIX`；不入库 |
| `cache/` | `logs\`、`backup\`、`api.token`、`proxy.pid`；不入库 |
| `.gitignore` | 忽略 `.env`、`cache/`、`__pycache__`、`panel/app_res.o`、`*.tmp` |
| `README.md` | 本文件 |
