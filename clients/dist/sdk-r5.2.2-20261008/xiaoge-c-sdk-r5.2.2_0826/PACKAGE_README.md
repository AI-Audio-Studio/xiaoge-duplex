# Xiaoge C SDK R5.2.2 客户交付包

本交付包为端侧客户集成 Xiaoge Duplex R5.2.2 `/ws/session` 主链路准备，包含 C SDK 源码、示例、协议文档、证书以及 Windows x86_64 MinGW64 预编译静态库和 demo 可执行文件。

## 目录结构

```text
include/                         C SDK 公开头文件
src/                             C SDK 源码实现
examples/                        文件 demo、Windows 麦克风 demo、codec 自测、本地代理
third_party/cjson/               内置 cJSON 源码，供协议 JSON 解析/构建使用
cmake/                           CMake 构建文件和 Android adb 测试脚本
certs/                           云端/测试 Gateway CA PEM
lib/windows-x86_64-mingw64/       预编译静态库
bin/windows-x86_64-mingw64/       预编译 demo / 测试程序
docs/                            C SDK 原始说明
README.md                        clients SDK 总说明
PROTOCOL.md                      R5.2.2 协议说明
SDK_TEST_VALIDATION.md           SDK 验证说明
SHA256SUMS.txt                   包内文件校验
```

## 预编译产物

```text
lib/windows-x86_64-mingw64/libxiaoge_client.a
bin/windows-x86_64-mingw64/xiaoge_demo_file.exe
bin/windows-x86_64-mingw64/xiaoge_demo_mic_win.exe
bin/windows-x86_64-mingw64/xiaoge_c_codec_test.exe
```

这些产物来自当前 Windows MinGW64 构建环境。运行 demo 时仍需要系统或 MinGW64 环境可找到 libwebsockets/libcurl/OpenSSL/libuv 等运行时 DLL；如缺失 DLL，请安装对应 MinGW64 运行环境或把运行时 DLL 加入 PATH。

## 从源码重新构建

本包根目录已提供适配整理后目录结构的 `CMakeLists.txt`。在包根目录执行：

```bash
cmake -S . -B build
cmake --build build
ctest --test-dir build --output-on-failure
```

依赖：libwebsockets >= 4.0 且启用 TLS、libcurl、CMake、C 编译器；非 Windows 通常还需要 pthread。

## 主链路

当前 SDK 主链路为：

```text
POST create_session
  -> session.created(trace_id, session_id, access_token, ws_url)
  -> WSS /ws/session + Authorization: Bearer <access_token>
  -> ctrl.hello
  -> ctrl.ready / ctrl.state / data.* / PCM
```

旧 `/ws/audio` 不是本 SDK 交付目标。

## 证书与安全

正式验收不建议使用 `--insecure`。生产/正式测试应使用证书 SAN 匹配的域名访问 Gateway，或使用 `--ca-cert certs/cloud-ca.pem` / 客户自己的 CA bundle。

## 进一步说明

- C SDK API：见 `include/xiaoge_client.h` 和 `docs/C_SDK_README.md`。
- 协议细节：见 `PROTOCOL.md`。
- 验证步骤：见 `SDK_TEST_VALIDATION.md`。
