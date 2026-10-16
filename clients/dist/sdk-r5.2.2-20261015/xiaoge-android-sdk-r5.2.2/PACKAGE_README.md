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

## 更新说明（2026-10-15 刷新）

- AAR 由当前 `clients/android/xiaoge-sdk-core` release 构建重新生成。
- 新增双工开关 `ctrl.duplex`：`XiaogeVoiceSession.sendDuplex(boolean)`（直用 `XiaogeClient` 时为 `client.sendDuplex(boolean)`）上行；服务端成功调度后回执 `ctrl.duplex_state`，解析为 `DuplexStateEvent` 并触发两层 listener 的 `onDuplexState`，照常走 `onJson`。回执语义为已调度，不等于 agent loop 已完成赋值；开关不直接修改业务三态（sleeping/dialogue/listening 仍以 `onServerState` 为准）。Demo 底部新增 Duplex 开关可用于真机验证。
- 新增应用层保活（`ctrl.ping`）：WebSocket 连接建立并发送 `ctrl.hello` 后，`XiaogeClient` 传输层每 25 秒发送一次 `ctrl.ping`；`new XiaogeClient(config, listener, http, pingIntervalSec)` 可调整周期（单位秒，`<= 0` 关闭）。`XiaogeVoiceSession` 与直用 `XiaogeClient` 的 App 均自动生效。收到 `ctrl.pong` 时按 `client_ts_ms` 计算并输出 RTT `DEBUG` 日志；SDK 不做连续 pong 超时重连检测。
- 新增 `ProtocolCodec` 中的 `ctrl.state` wire 常量：`LINK_STATE_*` / `INTERACTION_MODE_*` / `ENGINE_GATE_*` / `RESOURCE_STATE_*`，比较 `StateEvent` 字段时使用，避免手写字符串（拼错不报错、永不匹配）；字段类型保持 `String`，未知协议值仍按宽松解析原样透传并记入 `parseWarnings`。详见 `docs/ANDROID_SDK_README.md`。
- 交付包清理：移除 `.gradle/`、`.idea/`、`demo/build/`、`local.properties` 等本地产物与本地配置（上一版交付包内含有残留）。

## 进一步说明

- Android SDK 调用说明：见 `docs/ANDROID_SDK_README.md`。
- 协议细节：见 `PROTOCOL.md`。
- 包内文件完整性：见 `SHA256SUMS.txt`。
