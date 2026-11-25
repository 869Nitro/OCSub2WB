# 第三方许可

OCSub2WB 的自有代码和文档采用 [MIT](./LICENSE)。Windows 便携包中的 OCSub2WB.exe 静态链接了以下运行库。构建日志记录使用的工具链版本。

| 组件 | 随包提供的许可 |
| --- | --- |
| GCC 的 libgcc 和 libstdc++ | [GPLv3](./licenses/GPL-3.0.txt) 和 [GCC Runtime Library Exception 3.1](./licenses/GCC-Runtime-Library-Exception-3.1.txt)。来源为 [GCC 16.2.0](https://github.com/gcc-mirror/gcc/tree/releases/gcc-16.2.0)。 |
| MinGW-w64 CRT 和头文件中的运行时代码 | [MinGW-w64 许可](./licenses/MinGW-w64-COPYING.txt)、[头文件声明](./licenses/MinGW-w64-headers-notices.txt)和 [CRT 声明](./licenses/MinGW-w64-runtime-notices.txt)。来源为 [MinGW-w64 14.0.0](https://github.com/mingw-w64/mingw-w64/tree/v14.0.0)。 |
| MinGW-w64 winpthreads | [winpthreads 的版权与许可](./licenses/winpthreads-COPYING.txt)。 |

便携包调用 Windows 系统 DLL。Python 和 WorkBuddy 来自用户的安装环境。OpenCode Go 是用户订阅的上游服务。
