# Xiaoge Android SDK R5.2.2 客户交付包

本交付包为 Android 客户端集成 Xiaoge Duplex R5.2.2 `/ws/session` 主链路准备，包含 Android SDK AAR、Demo 工程源码、中文调用说明、协议文档和包内文件校验清单。

## 目录结构

```text
aar/                              Android SDK release AAR
docs/                             中文 Android SDK 调用说明
demo/                             Android 调用 Demo 源码
build.gradle                      Android 工程根构建文件
settings.gradle                   Android 工程模块配置
gradle.properties                 Android 工程配置
gradle/                           Gradle wrapper
PROTOCOL.md                       R5.2.2 协议说明
SHA256SUMS.txt                    包内文件校验
```

## 预编译产物

```text
aar/xiaoge-sdk-core-release.aar
```

AAR 来自 `clients/android/xiaoge-sdk-core` 的 release 构建。客户 App 集成时请同时声明 `okhttp` 和 `org.json` 依赖，具体示例见 `docs/ANDROID_SDK_README.md`。

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

## Demo 使用

Demo 源码位于 `demo/`，可作为 Android Studio 工程导入本交付包根目录后运行。交付包内 Demo 已配置为依赖 `aar/xiaoge-sdk-core-release.aar`，不需要 SDK 源码模块。运行前请在工程根目录创建 `local.properties` 并配置：

```properties
XIAOGE_API_KEY=your-cloud-api-key
```

构建 Demo：

```bash
./gradlew.bat :demo:assembleDebug
```

安装 Demo：

```bash
adb install -r demo/build/outputs/apk/debug/demo-debug.apk
```

## 更新说明（2026-10-08 刷新）

- AAR 由当前 `clients/android/xiaoge-sdk-core` release 构建重新生成。
- 下行事件解析改为宽松模式：服务端 JSON 字段缺失、多余或枚举值未知时，事件不再被丢弃，`onStt`/`onReply`/`onCommand` 等回调正常触发（缺失字段取类型默认值，多余字段忽略，未知枚举值透传，数字/布尔字符串自动转型）；每个事件对象新增 `parseWarnings` 字段（`String[]`，完全合规时为空）记录全部偏离项，SDK 同时在 Logcat 以 `WARN` 输出（tag `XiaogeClient`）。详见 `docs/ANDROID_SDK_README.md`。
- `onProtocolError` 语义收窄：仅覆盖无法解析的畸形 JSON 文本帧。
- 内置播放链路预热（首字时延优化）：无需 App 配置，冷起播中位约 736ms 降至 130~170ms；机制与时延埋点说明见 `docs/ANDROID_SDK_README.md` 的"播放链路预热"章节。
- `session.created` 响应允许多余字段；必填字段与 `ws_url` 路径校验保持不变；出站帧校验保持严格，与 R5.2.2 合同一致。

## 进一步说明

- Android SDK 调用说明：见 `docs/ANDROID_SDK_README.md`。
- 协议细节：见 `PROTOCOL.md`。
- 包内文件完整性：见 `SHA256SUMS.txt`。
