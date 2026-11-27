# 第三方组件与许可

OCSub2WB 项目的代码与文档采用 [MIT](./LICENSE)。本页列出 **v2.2.0 Windows x64 便携包**中由工具链静态链接的运行时代码，以及包内附带的许可文本。

## Windows 便携包

v2.2.0 的 Windows x64 便携包使用 WinLibs 工具链构建，采用 UCRT 和 POSIX 线程。[WinLibs 工具链发布记录](https://winlibs.com/)

| 组件 | 构建包版本 | 源码与附带文本 |
| --- | --- | --- |
| GCC 的 `libgcc` 和 `libstdc++` | `16.2.0`（WinLibs r3） | [GCC 16.2.0 源码](https://github.com/gcc-mirror/gcc/tree/releases/gcc-16.2.0) · [GPLv3](./licenses/GPL-3.0.txt) · [GCC Runtime Library Exception 3.1](./licenses/GCC-Runtime-Library-Exception-3.1.txt) |
| MinGW-w64 CRT 和头文件运行时代码 | `14.0.0` | [MinGW-w64 源码版本](https://github.com/mingw-w64/mingw-w64/tree/v14.0.0) · [COPYING](./licenses/MinGW-w64-COPYING.txt) · [头文件声明](./licenses/MinGW-w64-headers-notices.txt) · [CRT 声明](./licenses/MinGW-w64-runtime-notices.txt) |
| MinGW-w64 的 winpthreads | `14.0.0` | [MinGW-w64 源码版本](https://github.com/mingw-w64/mingw-w64/tree/v14.0.0) · [winpthreads 版权及许可](./licenses/winpthreads-COPYING.txt) |

便携包中的许可文件放在 `licenses/`。源码构建步骤见[使用手册](https://github.com/869Nitro/OCSub2WB/blob/v2.2.0/docs/usage.md#命令行与构建)。

## 运行环境

面板调用 Windows 提供的系统 DLL。Python 和 WorkBuddy 来自用户的安装环境。OpenCode Go 是用户自行订阅的上游服务。[OpenCode Go](https://opencode.ai/docs/console/go/)
