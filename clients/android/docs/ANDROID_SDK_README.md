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
| `sendDuplex(boolean enabled)` | 双工开关：`false` 关闭双工（语音/手发文字只显示不回复），`true` 开启；回执见 `onDuplexState`。 |
| `sendPcm(byte[] pcm)` | 仅在 `AudioInputMode.EXTERNAL` 外部音频输入模式下可用。 |
| `sendCmdAck(...)` / `sendCmdResult(...)` | App 自行接管命令执行时发送 ack/result。 |

### 回调说明

| 回调 | 说明 |
| --- | --- |
| `onState(SessionState state)` | SDK 高层会话状态变化。 |
| `onReady(int sampleRate, boolean reconnected)` | 服务端 `ctrl.ready` 到达，语音链路可用。 |
| `onClear(ClearEvent event)` | 服务端清理/打断事件，常用 `event.reason`。 |
| `onServerState(StateEvent event)` | 服务端 `ctrl.state` 状态更新，常用 `event.interactionMode`、`event.resourceState`。 |
| `onDuplexState(DuplexStateEvent event)` | `ctrl.duplex_state` 回执（`sendDuplex` 后到达），`event.enabled` 为最新开关状态；表示已调度，不一定已生效。 |
| `onStt(SttEvent event)` | 用户语音识别文本事件，常用 `event.text`、`event.isFinal`。 |
| `onReply(ReplyEvent event)` | 助手回复文本事件，常用 `event.text`、`event.intentType`。 |
| `onCommand(CommandEvent event)` | 服务端下发命令事件，常用 `event.cmdId`、`event.action`、`event.params`。 |
| `onError(ErrorEvent event)` | 服务端 `data.error` 事件，常用 `event.code`、`event.retryable`。 |
| `onJson(JSONObject payload)` | 所有下行 JSON raw observer，适合日志/协议审计。 |
| `onProtocolError(ProtocolErrorEvent event)` | SDK 本地入站错误，目前仅覆盖无法解析的畸形 JSON 文本帧；不是服务端 `data.error`。 |
| `onFailure(Throwable error)` | SDK 本地异常或网络失败。 |

回调运行在 SDK 内部线程；Android UI 更新必须使用 `Activity.runOnUiThread(...)`、`Handler` 或其他主线程调度方式。`onClear`、`onServerState`、`onStt`、`onReply`、`onCommand`、`onError` 均接收类型化事件对象。`onJson(JSONObject payload)` 是唯一 raw observer，适合日志、协议审计或读取尚未建模的字段；业务处理不要同时在 `onJson` 和类型化命令回调里执行同一命令。事件对象字段在用户回调前复制完成，`event.raw` 保留原始协议内容供调试。`ReplyEvent.isFinal` 是 SDK 为统一文本事件接口派生的值，R5.2.2 `data.reply` 协议 payload 没有 `final` 字段，SDK 不会把它写入 raw payload。

### 下行事件宽松解析

下行事件不会因服务端协议字段漂移而丢失：字段缺失时以类型默认值交付（字符串 `""`、数字 `0`、布尔 `false`、对象/数组为空），多余字段忽略（仍可在 `event.raw` 中访问），未知枚举值（如新增 `intent_type`/错误码）原样透传，数字/布尔字符串自动转型。每个事件对象携带 `parseWarnings`（`String[]`，完全合规时为空数组）记录全部偏离项，SDK 会同时在 Logcat 以 `WARN` 输出（tag `XiaogeClient`），便于发现服务端协议漂移。业务侧如需严格判断，可检查 `event.parseWarnings.length == 0` 或 `event.text` 是否为空。

### ctrl.state 状态常量

比较 `StateEvent` 字段时使用 `ProtocolCodec` 中的 wire 常量，避免手写字符串（拼错不报错、永不匹配）：

```java
import com.xiaoge.client.ProtocolCodec;

@Override
public void onServerState(StateEvent event) {
    if (ProtocolCodec.INTERACTION_MODE_SLEEPING.equals(event.interactionMode)) {
        // 休眠态 UI
    }
}
```

可用常量：`LINK_STATE_CONNECTING/CONNECTED/RECONNECTING/CLOSED`、`INTERACTION_MODE_SLEEPING/DIALOGUE/LISTENING`、`ENGINE_GATE_CLOSED/OPEN/KWS_ONLY`、`RESOURCE_STATE_SLEEPING_HOT/SLEEPING_WARM/ACTIVE_AGENT/RELEASED_IDLE/PENDING_RECONNECT`。字段类型保持 `String`，未知协议值仍按宽松解析原样透传并记入 `parseWarnings`。本地 `SessionState`（会话生命周期）与服务端 `interaction_mode`（业务三态）是两个概念，不要互相比较。

### 交互状态 interaction_mode 获取方式

`interactionMode` 只能从 `onServerState(StateEvent event)` 回调获取；SDK 不缓存当前状态、也不提供查询接口，业务侧自行保存最新值：

```java
private volatile String latestInteractionMode;

@Override
public void onServerState(StateEvent event) {
    latestInteractionMode = event.interactionMode;
    if (ProtocolCodec.INTERACTION_MODE_SLEEPING.equals(event.interactionMode)) {
        // 休眠态 UI（注意切换到主线程更新 UI）
    }
}
```

- **下发时机**：握手完成后紧跟 `ctrl.ready` 下发一次初始状态，之后服务端每次状态变化重发。
- **快照语义**：每条 `ctrl.state` 都是四字段的**全量快照**，广播可能重复；按最新一条更新 UI/状态机，不必把每帧当作新事件（DEVICE_PROTOCOL §3.1）。
- **三态含义**：

| `interaction_mode` | 含义 | 典型 `engine_gate` | 典型 `resource_state` |
| --- | --- | --- | --- |
| `sleeping` | 业务休眠，等待唤醒/有效输入 | `open` | `SleepingHot` |
| `dialogue` | 正常交互模式 | `open` | `ActiveAgent` |
| `listening` | 收听输入，但被拒轮次不进入正常回复 | `open` | `ActiveAgent` |

- **注意**：实际 wire 字段是 `interaction_mode`（不是 `interaction_state`）；当前三态全部 `engine_gate="open"`，不要因 sleeping/listening 自行停止上传音频；`linkState` 是连接状态，不代表是否在对话；本地 `SessionState` 是会话生命周期状态，与服务端业务三态是两个概念。比较方式见上文「ctrl.state 状态常量」。回调运行在 SDK 内部线程，UI 更新需切主线程。

### 应用层保活（ctrl.ping）

保活在 `XiaogeClient` 传输层实现，`XiaogeVoiceSession` 与直接使用 `XiaogeClient` 的 App 均自动生效：
WebSocket 连接建立并发送 `ctrl.hello` 后，SDK 每 25 秒发送一次
`{"type":"ctrl.ping","ts_ms":<epoch ms>}`（Logcat tag `XiaogeClient` 可见）。25 秒是客户端策略
（对齐 DEVICE_PROTOCOL §4.2），不是服务端强制期限。

- 调整周期：`new XiaogeClient(config, listener, http, pingIntervalSec)`，单位秒；`<= 0` 关闭自动保活。
- 收到 `ctrl.pong` 时 SDK 按 `client_ts_ms` 回显用客户端时钟计算 RTT 并输出 `DEBUG` 日志，
  随后仍交给 `onJson(payload)`；pong 不会唤醒 dialogue，也不延长业务 listening/idle 超时。
- 保活仅保持传输/会话活性，SDK 不做"连续 pong 未收到则重连"的检测；`XiaogeVoiceSession`
  的自动重连由传输失败（`onFailure`）触发，与 pong 无关。

### 双工开关（ctrl.duplex）

```java
session.sendDuplex(false);   // XiaogeVoiceSession；直用 XiaogeClient 时为 client.sendDuplex(false)

@Override
public void onDuplexState(DuplexStateEvent event) {
    Log.i("demo", "duplex enabled=" + event.enabled);
}
```

- `sendDuplex(enabled)` 发送 `{"type":"ctrl.duplex","enabled":<bool>}`（DEVICE_PROTOCOL §3.3），
  在 `XiaogeClient` 传输层实现，`XiaogeVoiceSession` 与直用 `XiaogeClient` 的 App 均可用。
- 回执 `ctrl.duplex_state` 解析为 `DuplexStateEvent`（`enabled`、`tsMs`、`traceId`、`sessionId`、
  `raw`、`parseWarnings`），两层 listener（`XiaogeClient.Listener.onDuplexState` /
  `XiaogeSessionListener.onDuplexState`）都会触发，并照常走 `onJson`。
- 回执语义是**已调度**，不等于 agent loop 已完成赋值；开关不直接修改业务三态
  （sleeping/dialogue/listening 仍以 `onServerState` 为准）。
- 没有 `full_duplex` wire 字段；`ctrl.mic_state` 未支持，不要发送。
- demo（`clients/android/demo`）底部有 Duplex 开关可用于真机验证：通话中切换后，logcat 可见
  `ctrl.duplex` 上行与 `ctrl.duplex_state` 回执，并弹 Toast 提示（scheduled）。

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

`sendCmdResult` 另有带 `result` 参数的重载,用于在 `data.cmd_result` 中携带可选的业务返回值(透传一个 JSON 对象),例如采集类命令的结果:

```java
s.sendCmdResult(
        event,
        ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
        "ok",
        "completed",
        false,
        new JSONObject("{\"type\":\"cap\",\"value\":40,\"gauge_id\":1}"));
```

`result` 为 `null` 或使用旧重载时,消息中不含 `result` 字段,与旧协议完全兼容。

SDK 不会默认回放 `cmd_ack` / `cmd_result`，业务侧不要在 `onJson(JSONObject)` 和 `onCommand(CommandEvent)` 中重复执行同一命令。

### 播放链路预热（首字时延优化）

输出通路（音频 HAL / A2DP 流）在无数据约 0.65~3.3s 后会休眠/挂起，下一轮回复的首包需要付出数百毫秒的重开成本。SDK 默认内置播放管线预热机制（参考已真机验证的方案，冷起播中位 736ms 降到约 130~170ms），无需 App 做任何配置：

- 预热布防后，播放循环每 100ms 向常驻 `AudioTrack` 直写一小帧静音（不进入播放队列），直到真实首帧到达即停——管线保温且不会在真实音频前堆积静音。
- 三个布防点：
  1. **本地说完估计**（SDK 采集模式自动生效）：按麦克风帧 RMS 估计"用户刚说完"，领先服务端断句 1~3s 提前布防；
  2. **`data.stt` final**：服务端断句兜底（说完估计漏检、说话过轻时覆盖），也是 `AudioInputMode.EXTERNAL` 外部音频输入模式下的唯一布防点；
  3. **引擎启动**（`ctrl.ready` 建连后）：覆盖问候语轮和重连后的首轮。
- 误布防的代价仅为最长 8s 的低占空比静音流，对音量和语义无影响。

时延埋点（`Log.d`，logcat tag `XiaogeAudio`，可长期采集）：

```text
latency_node=mic_speech_end ...           本地说完估计触发
latency_node=stream_prewarm_armed ...     预热布防
latency_node=tts_first_write ...          回复首包写入 AudioTrack
latency_node=playback_first_presented ..., firstWriteToFirstPresentedMs=...   首帧进入输出通路
```

其中 `firstWriteToFirstPresentedMs` 即"手机侧起播成本"，可用于验收预热效果。如需 A/B 对照或调整参数（布防时长、说完估计 RMS 阈值等），参见 `AudioEngine` 与 `SpeechEndEstimator` 中的常量；2s 间隔背景心跳默认关闭，可将 `AudioEngine.KEEPALIVE_ENABLED` 置 `true` 开启。

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
