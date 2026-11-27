# 第三方组件与许可

OCSub2WB 项目的代码与文档采用 [MIT](./LICENSE)。本页列出 **v2.2.1 Windows x64 便携包**中由工具链静态链接的运行时代码，以及包内附带的许可文本。

## Windows 便携包

v2.2.1 的发布资产来自 Windows GitHub Actions 中通过测试的 MSYS2 MINGW64 构建。下表的版本是此构建实际安装的包版本。[v2.2.1 构建记录](https://github.com/869Nitro/OCSub2WB/actions/runs/46046637393/job/140667430333)

| 组件 | 构建包版本 | 源码与附带文本 |
| --- | --- | --- |
| GCC 的 `libgcc` 和 `libstdc++` | `16.2.0-7` | [GCC 16.2.0 源码](https://github.com/gcc-mirror/gcc/tree/releases/gcc-16.2.0) · [GPLv3](./licenses/GPL-3.0.txt) · [GCC Runtime Library Exception 3.1](./licenses/GCC-Runtime-Library-Exception-3.1.txt) |
| MinGW-w64 CRT 和头文件运行时代码 | `14.0.0.r598.ge3d18511f-1` | [MinGW-w64 源码版本](https://github.com/mingw-w64/mingw-w64/commit/e3d18511f) · [COPYING](./licenses/MinGW-w64-COPYING.txt) · [头文件声明](./licenses/MinGW-w64-headers-notices.txt) · [CRT 声明](./licenses/MinGW-w64-runtime-notices.txt) |
| MinGW-w64 的 winpthreads | `14.0.0.r598.ge3d18511f-1` | [MinGW-w64 源码版本](https://github.com/mingw-w64/mingw-w64/commit/e3d18511f) · [winpthreads 版权及许可](./licenses/winpthreads-COPYING.txt) |

便携包中的许可文件放在 `licenses/`。构建流程从实际使用的 MSYS2 工具链复制 MinGW-w64 相关原文。[构建与打包配置](https://github.com/869Nitro/OCSub2WB/blob/main/.github/workflows/build-release.yml)

## 运行环境

面板调用 Windows 提供的系统 DLL。Python 和 WorkBuddy 来自用户的安装环境。OpenCode Go 是用户自行订阅的上游服务。[OpenCode Go](https://opencode.ai/docs/console/go/)
