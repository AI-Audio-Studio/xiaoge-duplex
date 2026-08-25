# Xiaoge Android SDK R5.2.2 中文调用说明

本文说明如何在 Android App 中集成 `xiaoge-sdk-core-release.aar`，并使用 Xiaoge Duplex R5.2.2 主链路完成建连、语音采集、下行播放和业务回调。

## 1. SDK 范围

Android SDK 当前实现的是 R5.2.2 主链路：

```text
POST create_session
  -> session.created(trace_id, session_id, access_token, ws_url)
  -> WSS /ws/session + Authorization: Bearer <access_token>
  -> ctrl.hello
  -> ctrl.ready / ctrl.state / data.* / PCM
```

历史 `/ws/audio` 不是本 SDK 的交付目标。收到服务端下发的 `data.cmd` 后，SDK 通过 `onCommand(CommandEvent)` 交给业务侧处理。SDK 不会默认执行真实机器人动作，业务侧确认要执行或拒绝命令时，应显式上报命令状态。所有 raw 下行 JSON 统一通过 `onJson(JSONObject payload)` 观察。

## 2. 环境要求

- Android Studio 可导入 Gradle 工程。
- Android Gradle Plugin：8.5.2。
- Gradle Wrapper：8.9。
- `compileSdk`：35。
- `minSdk`：23。
- Java：17。

## 3. AAR 集成

将交付包中的 AAR 复制到客户 App：

```text
app/libs/xiaoge-sdk-core-release.aar
```

在 App 模块 `build.gradle` 中声明依赖：

```gradle
dependencies {
    implementation files('libs/xiaoge-sdk-core-release.aar')
    implementation "com.squareup.okhttp3:okhttp:4.12.0"
    implementation "org.json:json:20240303"
}
```

如客户工程已使用 Maven 仓库或制品库，也可以将 AAR 发布到内部 Maven 后用坐标依赖；但依赖版本仍需与上方保持兼容。

## 4. 权限配置

`AndroidManifest.xml` 至少需要：

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.RECORD_AUDIO" />
```

Android 6.0 及以上需要在运行时申请 `RECORD_AUDIO`。Demo 的 `MainActivity` 已包含运行时申请逻辑。

## 5. API Key 与敏感信息

不要把云端 `x-api-key` 写死在 App 源码中。Demo 使用工程根目录 `local.properties` 注入：

```properties
XIAOGE_API_KEY=your-cloud-api-key
```

`XiaogeConfig` 只会在 `create_session` HTTP 请求中携带非空 `x-api-key`。WebSocket 鉴权使用 `create_session` 返回的 `access_token`，请求头为：

```text
Authorization: Bearer <access_token>
```

## 6. 推荐调用方式：XiaogeVoiceSession

`XiaogeVoiceSession` 是移动端推荐入口，负责：

- `XiaogeClient` 建连和重连。
- 麦克风采集与上行 PCM。
- 下行 PCM 播放。
- 音频焦点管理。
- 会话状态与业务事件回调。

示例：

```java
XiaogeConfig cfg = new XiaogeConfig(
        "https://60.205.197.165:10099/create_session",
        "android-demo-001",
        "{\"type\":\"mock\",\"value\":\"android-demo\"}",
        apiKey,
        Arrays.asList("audio", "text", "cmd", "state"),
        "xiaoge-android-sdk-r5.2.2",
        "{}");

XiaogeSessionListener listener = new XiaogeSessionListener() {
    @Override
    public void onReady(int sampleRate, boolean reconnected) {
        // SDK 内部线程回调；如需更新 UI，请切回主线程。
        session.sendFrontendState("hint", "awake", "speech", 1000);
    }

    @Override
    public void onStt(SttEvent event) {
        String text = event.text;
        boolean isFinal = event.isFinal;
    }

    @Override
    public void onReply(ReplyEvent event) {
        String text = event.text;
        String intentType = event.intentType;
    }

    @Override
    public void onCommand(CommandEvent event) {
        // 业务侧确认要执行或拒绝命令时，调用 sendCmdAck / sendCmdResult。
    }

    @Override
    public void onFailure(Throwable error) {
        // 处理网络、鉴权、音频等异常。
    }
};

XiaogeVoiceSession session = new XiaogeVoiceSession(
        context,
        cfg,
        listener,
        XiaogeTls.cloudClient(context));
session.start();

// 页面销毁或用户结束通话时释放资源。
session.stop();
```

> 注意：上例中的 `session` 变量如果在匿名内部类中引用，实际工程中请保存为成员变量，参考 Demo 的 `MainActivity`。

### 常用方法

| 方法 | 说明 |
| --- | --- |
| `start()` | 启动会话。每个 `XiaogeVoiceSession` 只能调用一次。 |
| `stop()` | 停止会话并释放 WebSocket、音频、音频焦点等资源。幂等。 |
| `state()` | 获取当前 `SessionState`。 |
| `sendFrontendState(trustLevel, wakeState, vad, ttlMs)` | 发送前端状态，例如唤醒、VAD、信任等级。 |
| `sendPcm(byte[] pcm)` | 仅在 `AudioInputMode.EXTERNAL` 外部音频输入模式下可用。 |
| `sendCmdAck(...)` / `sendCmdResult(...)` | App 自行接管命令执行时发送 ack/result。 |

### 回调说明

| 回调 | 说明 |
| --- | --- |
| `onState(SessionState state)` | SDK 高层会话状态变化。 |
| `onReady(int sampleRate, boolean reconnected)` | 服务端 `ctrl.ready` 到达，语音链路可用。 |
| `onClear(ClearEvent event)` | 服务端清理/打断事件，常用 `event.reason`。 |
| `onServerState(StateEvent event)` | 服务端 `ctrl.state` 状态更新，常用 `event.interactionMode`、`event.resourceState`。 |
| `onStt(SttEvent event)` | 用户语音识别文本事件，常用 `event.text`、`event.isFinal`。 |
| `onReply(ReplyEvent event)` | 助手回复文本事件，常用 `event.text`、`event.intentType`。 |
| `onCommand(CommandEvent event)` | 服务端下发命令事件，常用 `event.cmdId`、`event.action`、`event.params`。 |
| `onError(ErrorEvent event)` | 服务端 `data.error` 事件，常用 `event.code`、`event.retryable`。 |
| `onJson(JSONObject payload)` | 所有下行 JSON raw observer，适合日志/协议审计。 |
| `onProtocolError(ProtocolErrorEvent event)` | SDK 本地入站 JSON/字段校验失败；不是服务端 `data.error`。 |
| `onFailure(Throwable error)` | SDK 本地异常或网络失败。 |

回调运行在 SDK 内部线程；Android UI 更新必须使用 `Activity.runOnUiThread(...)`、`Handler` 或其他主线程调度方式。`onClear`、`onServerState`、`onStt`、`onReply`、`onCommand`、`onError` 均接收类型化事件对象。`onJson(JSONObject payload)` 是唯一 raw observer，适合日志、协议审计或读取尚未建模的字段；业务处理不要同时在 `onJson` 和类型化命令回调里执行同一命令。事件对象字段在用户回调前复制完成，`event.raw` 保留原始协议内容供调试。`ReplyEvent.isFinal` 是 SDK 为统一文本事件接口派生的值，R5.2.2 `data.reply` 协议 payload 没有 `final` 字段，SDK 不会把它写入 raw payload。

### 发送 `cmd_ack` / `cmd_result`

当 App 要执行云端下发的 `data.cmd` 时，推荐在收到 `onCommand(CommandEvent event)` 后基于事件对象回复 ack/result。SDK 会从该 `event` 中复制 `traceId`、`sessionId`、`utteranceId` 和 `cmdId`，业务侧只需要填写协议规定的 `status` 和业务 `code`。

推荐使用 `ProtocolCodec` 中的状态常量，避免手写字符串：

```java
import com.xiaoge.client.ProtocolCodec;

@Override
public void onCommand(CommandEvent event) {
    XiaogeVoiceSession s = session;
    if (s == null) {
        return;
    }

    // 1. 已接收并排队执行命令。
    s.sendCmdAck(
            event,
            ProtocolCodec.CMD_ACK_STATUS_ACCEPTED,
            "ok",
            "queued");

    // 2. 可选：通知云端命令已开始执行。
    s.sendCmdResult(
            event,
            ProtocolCodec.CMD_RESULT_STATUS_RUNNING,
            "ok",
            "started");

    try {
        // TODO: 在业务线程中执行真实机器人动作。
        runRobotAction(event);

        // 3. 执行成功。
        s.sendCmdResult(
                event,
                ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
                "ok",
                "completed",
                false);
    } catch (Exception e) {
        // 4. 执行失败。
        s.sendCmdResult(
                event,
                ProtocolCodec.CMD_RESULT_STATUS_FAILED,
                "action_failed",
                e.getMessage(),
                true);
    }
}
```

如果 App 拒绝执行命令，例如能力不支持或参数非法，可直接回复 rejected ack，不再发送 result：

```java
s.sendCmdAck(
        event,
        ProtocolCodec.CMD_ACK_STATUS_REJECTED,
        "capability_unsupported",
        "unsupported");
```

合法 `cmd_ack` status 为 `accepted`、`rejected`、`duplicate`；合法 `cmd_result` status 为 `running`、`succeeded`、`failed`、`canceled`、`timeout`。SDK 仍兼容合法原始字符串，但非法 status 会在本地抛出 `IllegalArgumentException`，不会发送到服务端。

SDK 不会默认回放 `cmd_ack` / `cmd_result`，业务侧不要在 `onJson(JSONObject)` 和 `onCommand(CommandEvent)` 中重复执行同一命令。

## 7. 自行管理音频：XiaogeClient

如果 App 已有自己的音频采集/播放链路，可以直接使用低层 `XiaogeClient`：

```java
XiaogeClient client = new XiaogeClient(cfg, new XiaogeClient.Listener() {
    @Override
    public void onReady(int sampleRate) {}

    @Override
    public void onAudio(byte[] pcm) {
        // 播放服务端下行 PCM。
    }

    @Override
    public void onStt(SttEvent event) {}

    @Override
    public void onReply(ReplyEvent event) {}

    @Override
    public void onFailure(Throwable error) {}
});

client.start();
client.sendFrontendState("hint", "awake", "speech", 1000);
client.sendPcm(pcmFrame);
client.close();
```

PCM 帧大小不能超过 32768 bytes。SDK 默认协议音频格式为 16 kHz、16-bit、mono、little-endian PCM。

## 8. TLS 与证书

SDK 内置云端 CA 证书资源：

```text
xiaoge-sdk-core/src/main/res/raw/xiaoge_cloud_ca.pem
```

正式测试/生产建议使用：

```java
OkHttpClient http = XiaogeTls.cloudClient(context);
```

如客户使用自己的 CA bundle，可通过 `XiaogeTls.fromPemCa(inputStream)` 构造 OkHttpClient。

`XiaogeTls.insecureClient()` 会跳过证书和主机名校验，仅允许在临时调试或 fake-server 环境使用，不应用于正式验收和生产。

## 9. Demo 运行

交付包中的 `demo/` 是一个最小 Android App，功能包括：

- POST `create_session`。
- 使用 `Authorization: Bearer` 打开 WSS `/ws/session`。
- 发送 `ctrl.hello` 和 `ctrl.frontend_state`。
- 使用 `AudioRecord` 采集麦克风 PCM。
- 使用 `AudioTrack` 播放下行 TTS PCM。
- 显示 `ctrl.state`、`data.stt`、`data.reply`、`data.cmd`、`data.error`。

配置 API key：

```properties
# local.properties
XIAOGE_API_KEY=your-cloud-api-key
```

构建：

```bash
./gradlew.bat :demo:assembleDebug
```

安装：

```bash
adb install -r demo/build/outputs/apk/debug/demo-debug.apk
```

如果使用 Android 模拟器访问宿主机 fake-server，`create_session` 地址通常需要写成：

```text
http://10.0.2.2:<port>/create_session
```

真机访问局域网服务时，请使用真机可访问的 IP/域名，并确认防火墙、端口、证书 SAN 均正确。

## 10. 常见问题

### `create_session failed: 401/403`

检查 `XIAOGE_API_KEY` 是否配置、是否被正确注入、是否与目标 Gateway 环境匹配。

### `create_session failed: 404` 或连接到了 `/ws/audio`

确认 Gateway 返回的 `ws_url` 是 `/ws/session`。SDK 会拒绝历史 `/ws/audio` 或带 query token 的旧链路。

### 麦克风没有声音

确认 Manifest 声明了 `RECORD_AUDIO`，且用户已授予运行时麦克风权限。真机上还需确认系统没有禁止 App 使用麦克风。

### TLS 证书失败

正式环境优先使用域名和 `XiaogeTls.cloudClient(context)`。如果证书 SAN 与 IP/域名不匹配，需要使用证书匹配的访问地址或更新 CA/证书配置。不要用 `insecureClient()` 作为正式解决方案。

### UI 更新崩溃

`XiaogeSessionListener` 回调不在主线程。所有 UI 操作都需要切回 Android 主线程。
