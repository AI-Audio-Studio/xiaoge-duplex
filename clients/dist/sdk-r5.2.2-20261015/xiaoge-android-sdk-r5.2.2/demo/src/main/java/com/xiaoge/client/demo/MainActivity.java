package com.xiaoge.client.demo;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.BatteryManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.Log;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;
import androidx.recyclerview.widget.SimpleItemAnimator;

import com.xiaoge.client.ClearEvent;
import com.xiaoge.client.CommandEvent;
import com.xiaoge.client.DuplexStateEvent;
import com.xiaoge.client.ErrorEvent;
import com.xiaoge.client.ProtocolCodec;
import com.xiaoge.client.ReplyEvent;
import com.xiaoge.client.SessionState;
import com.xiaoge.client.StateEvent;
import com.xiaoge.client.SttEvent;
import com.xiaoge.client.XiaogeConfig;
import com.xiaoge.client.XiaogeSessionListener;
import com.xiaoge.client.XiaogeTls;
import com.xiaoge.client.XiaogeVoiceSession;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

public final class MainActivity extends Activity {
    private static final String TAG = "MainActivity";
    private static final int REQ_AUDIO = 1001;
    private static final int REQ_LOCATION = 1002;
    private static final long LOCATION_TIMEOUT_MS = 15000L;
    private static final String ACTION_CHECK_BATTERY = "check_battery";
    private static final String[] LOCATION_PROVIDERS = {
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER,
    };
    private static final String CREATE_SESSION_URL = "https://60.205.197.165:10097/create_session";
    private static final String DEVICE_ID = "android-demo-001";
    private static final String CREDENTIAL_JSON = "{\"type\":\"mock\",\"value\":\"android-demo\"}";
    private static final String CLIENT_VERSION = "xiaoge-android-demo-r5.2.2";
    private static final String API_KEY = BuildConfig.XIAOGE_API_KEY;

    private final List<ChatMessageItem> messages = new ArrayList<>();

    private Button callButton;
    private TextView statusText;
    private RecyclerView transcriptList;
    private ChatMessageAdapter adapter;
    private Switch duplexSwitch;
    /** 服务端 ctrl.duplex_state 回执回写开关时抑制 checked-change 重发。 */
    private boolean applyingServerDuplex;

    private boolean running;
    private boolean starting;
    // 最新一条用户消息槽位：同一 utterance_id 的 partial/final 始终更新这一条，
    // 被 agent 回复标记完成后也不释放；只有出现"真正的新一句"才新建条目
    // （新 utterance_id 若是未定稿旧句的文本延续则并入同一条）。
    private int pendingUserIndex = -1;
    private String pendingUtteranceId;
    private XiaogeVoiceSession session;

    private LocationManager locationManager;
    private LocationListener locationListener;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable locationTimeout = this::onLocationTimeout;
    private boolean locating;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        callButton = findViewById(R.id.callButton);
        statusText = findViewById(R.id.statusText);
        transcriptList = findViewById(R.id.transcriptList);
        duplexSwitch = findViewById(R.id.duplexSwitch);

        adapter = new ChatMessageAdapter(messages);
        LinearLayoutManager layoutManager = new LinearLayoutManager(this);
        layoutManager.setStackFromEnd(true);
        transcriptList.setLayoutManager(layoutManager);
        transcriptList.setAdapter(adapter);
        if (transcriptList.getItemAnimator() instanceof SimpleItemAnimator) {
            ((SimpleItemAnimator) transcriptList.getItemAnimator()).setSupportsChangeAnimations(false);
        }

        callButton.setOnClickListener(v -> toggleCall());
        duplexSwitch.setEnabled(false);
        duplexSwitch.setOnCheckedChangeListener(this::onDuplexToggled);
        statusText.setText("Disconnected");

        requestLocationOnce();
    }

    @Override
    protected void onDestroy() {
        stopLocationUpdates();
        stopClient();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_AUDIO) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                connect();
            } else {
                Toast.makeText(this, "Microphone permission is required.", Toast.LENGTH_LONG).show();
            }
        } else if (requestCode == REQ_LOCATION) {
            if (hasLocationPermission()) {
                startOneShotLocation();
            } else {
                Log.d(TAG, "location permission denied");
                Toast.makeText(this, "Location permission denied.", Toast.LENGTH_SHORT).show();
            }
        }
    }

    // ---- 经纬度获取（Android 原生 LocationManager，当前仅打印日志） ----

    private boolean hasLocationPermission() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                        == PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
                        == PackageManager.PERMISSION_GRANTED;
    }

    private void requestLocationOnce() {
        if (!hasLocationPermission()) {
            requestPermissions(
                    new String[]{
                            Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.ACCESS_COARSE_LOCATION
                    },
                    REQ_LOCATION);
            return;
        }
        startOneShotLocation();
    }

    @SuppressLint("MissingPermission")
    private void startOneShotLocation() {
        if (locating) {
            return;
        }
        if (locationManager == null) {
            locationManager = (LocationManager) getSystemService(LOCATION_SERVICE);
        }
        if (locationListener == null) {
            locationListener = new LocationListener() {
                @Override
                public void onLocationChanged(Location location) {
                    stopLocationUpdates();
                    Log.d(TAG, "location lat=" + location.getLatitude()
                            + " lng=" + location.getLongitude()
                            + " accuracy=" + location.getAccuracy()
                            + " provider=" + location.getProvider());
                }

                // API 30 以下是抽象方法，显式留空避免老设备上 AbstractMethodError。
                @Override
                public void onStatusChanged(String provider, int status, Bundle extras) {
                }

                @Override
                public void onProviderEnabled(String provider) {
                }

                @Override
                public void onProviderDisabled(String provider) {
                }
            };
        }
        try {
            List<String> enabled = new ArrayList<>();
            for (String provider : LOCATION_PROVIDERS) {
                if (locationManager.isProviderEnabled(provider)) {
                    enabled.add(provider);
                }
            }
            if (enabled.isEmpty()) {
                Log.d(TAG, "location unavailable (providers disabled)");
                return;
            }
            locating = true;
            for (String provider : enabled) {
                locationManager.requestLocationUpdates(provider, 0, 0, locationListener);
            }
            mainHandler.postDelayed(locationTimeout, LOCATION_TIMEOUT_MS);
        } catch (SecurityException e) {
            locating = false;
            Log.d(TAG, "location request failed: " + e.getMessage());
        }
    }

    /** 一次性定位超时：回退到最新缓存位置（getLastKnownLocation）。 */
    @SuppressLint("MissingPermission")
    private void onLocationTimeout() {
        if (!locating) {
            return;
        }
        stopLocationUpdates();
        Location best = null;
        try {
            for (String provider : LOCATION_PROVIDERS) {
                Location last = locationManager.getLastKnownLocation(provider);
                if (last != null && (best == null || last.getTime() > best.getTime())) {
                    best = last;
                }
            }
        } catch (SecurityException e) {
            Log.d(TAG, "location fallback failed: " + e.getMessage());
            return;
        }
        if (best != null) {
            Log.d(TAG, "location (cached) lat=" + best.getLatitude()
                    + " lng=" + best.getLongitude()
                    + " accuracy=" + best.getAccuracy()
                    + " provider=" + best.getProvider());
        } else {
            Log.d(TAG, "no location fix");
        }
    }

    private void stopLocationUpdates() {
        locating = false;
        mainHandler.removeCallbacks(locationTimeout);
        LocationManager lm = locationManager;
        LocationListener ll = locationListener;
        if (lm != null && ll != null) {
            lm.removeUpdates(ll);
        }
    }

    /** 电量查询命令：读取系统电量百分比并以 result cap 对象上报。 */
    private void handleCheckBattery(XiaogeVoiceSession s, CommandEvent event) {
        try {
            BatteryManager bm = (BatteryManager) getSystemService(BATTERY_SERVICE);
            int capacity = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
            Log.d(TAG, "check_battery capacity=" + capacity);
            if (capacity == Integer.MIN_VALUE) {
                // getIntProperty 对不支持该属性的电源返回 MIN_VALUE。
                s.sendCmdResult(
                        event,
                        ProtocolCodec.CMD_RESULT_STATUS_FAILED,
                        "battery_unavailable",
                        "capacity property not supported",
                        true);
                return;
            }
            JSONObject result = new JSONObject()
                    .put("type", "cap")
                    .put("value", capacity)
                    .put("gauge_id", 1);
            s.sendCmdResult(
                    event,
                    ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
                    "ok",
                    "completed",
                    false,
                    result);
        } catch (Exception e) {
            s.sendCmdResult(
                    event,
                    ProtocolCodec.CMD_RESULT_STATUS_FAILED,
                    "battery_query_failed",
                    e.getMessage(),
                    true);
        }
    }

    private void toggleCall() {
        if (starting) {
            return;
        }
        if (running) {
            stopClient();
            return;
        }
        connect();
    }

    private void onDuplexToggled(CompoundButton buttonView, boolean isChecked) {
        if (applyingServerDuplex) {
            return;
        }
        XiaogeVoiceSession s = session;
        if (s == null || !running) {
            applyingServerDuplex = true;
            duplexSwitch.setChecked(!isChecked);
            applyingServerDuplex = false;
            Toast.makeText(this, "Not connected", Toast.LENGTH_SHORT).show();
            return;
        }
        Log.d(TAG, "ctrl.duplex enabled=" + isChecked + " (waiting ack)");
        s.sendDuplex(isChecked);
    }

    private void connect() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
            statusText.setText("Requesting microphone permission");
            return;
        }
        clearTranscript();
        starting = true;
        callButton.setEnabled(false);
        statusText.setText("Connecting");
        Log.d(TAG, "connecting");
        try {
            XiaogeConfig cfg = new XiaogeConfig(
                    CREATE_SESSION_URL,
                    DEVICE_ID,
                    CREDENTIAL_JSON,
                    API_KEY,
                    Arrays.asList("audio", "text", "cmd", "state"),
                    CLIENT_VERSION,
                    "{}");
            XiaogeVoiceSession voiceSession = new XiaogeVoiceSession(
                    this, cfg, listener(), XiaogeTls.insecureClient());
            session = voiceSession;
            voiceSession.start();
        } catch (Exception e) {
            Log.d(TAG, "connect failed: " + e.getMessage());
            starting = false;
            running = false;
            callButton.setEnabled(true);
            callButton.setText("Start Call");
            duplexSwitch.setEnabled(false);
            statusText.setText("Disconnected");
            Toast.makeText(
                    MainActivity.this,
                    "Connect failed: " + e.getMessage(),
                    Toast.LENGTH_LONG
            ).show();
        }
    }

    private XiaogeSessionListener listener() {
        return new XiaogeSessionListener() {
            @Override
            public void onState(SessionState state) {
                runOnUiThread(() -> {
                    if (state == SessionState.CONNECTING || state == SessionState.RECONNECTING) {
                        statusText.setText("Connecting");
                    } else if (state == SessionState.READY) {
                        statusText.setText("Ready");
                    } else if (state == SessionState.AGENT_SPEAKING) {
                        statusText.setText("Speaking");
                    } else if (state == SessionState.STOPPED) {
                        statusText.setText("Disconnected");
                    } else if (state == SessionState.ERROR) {
                        statusText.setText("Error");
                    }
                });
            }

            @Override
            public void onReady(int sampleRate, boolean reconnected) {
                Log.d(TAG, "ctrl.ready sample_rate=" + sampleRate + " reconnected=" + reconnected);
                XiaogeVoiceSession s = session;
                if (s != null) {
                    s.sendFrontendState("hint", "awake", "speech", 1000);
                }
                runOnUiThread(() -> {
                    starting = false;
                    running = true;
                    callButton.setEnabled(true);
                    callButton.setText("End Call");
                    duplexSwitch.setEnabled(true);
                    statusText.setText("Ready");
                });
            }

            @Override
            public void onClear(ClearEvent event) {
                Log.d(TAG, "ctrl.clear " + (event.reason == null ? "" : event.reason));
            }

            @Override
            public void onServerState(StateEvent event) {
                String mode = event.interactionMode;
                Log.d(TAG, "ctrl.state " + mode);
                if (!TextUtils.isEmpty(mode)) {
                    runOnUiThread(() -> statusText.setText(mode));
                }
            }

            @Override
            public void onDuplexState(DuplexStateEvent event) {
                Log.d(TAG, "ctrl.duplex_state enabled=" + event.enabled + " ts_ms=" + event.tsMs);
                runOnUiThread(() -> {
                    applyingServerDuplex = true;
                    duplexSwitch.setChecked(event.enabled);
                    applyingServerDuplex = false;
                    Toast.makeText(
                            MainActivity.this,
                            "Duplex " + (event.enabled ? "on" : "off") + " (scheduled)",
                            Toast.LENGTH_SHORT).show();
                });
            }

            @Override
            public void onStt(SttEvent event) {
                String text = event.text;
                String utteranceId = event.utteranceId;
                boolean isFinal = event.isFinal();
                Log.d(TAG, "data.stt " + text + (isFinal ? " (final)" : ""));
                runOnUiThread(() -> upsertPendingUserMessage(utteranceId, text, isFinal));
            }

            @Override
            public void onReply(ReplyEvent event) {
                String text = event.text;
                Log.d(TAG, "data.reply " + text);
                runOnUiThread(() -> addAssistantMessage(text));
            }

            @Override
            public void onCommand(CommandEvent event) {
                Log.d(TAG, "onCommand: " + event.cmdId + " " + event.action);

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

                if (ACTION_CHECK_BATTERY.equals(event.action)) {
                    handleCheckBattery(s, event);
                    return;
                }

                try {
                    // TODO: 在业务线程中执行真实机器人动作。
                    Thread.sleep(1000);
                    // 2. 执行成功。
                    s.sendCmdResult(
                            event,
                            ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
                            "ok",
                            "completed",
                            false);
                } catch (Exception e) {
                    // 3. 执行失败。
                    s.sendCmdResult(
                            event,
                            ProtocolCodec.CMD_RESULT_STATUS_FAILED,
                            "action_failed",
                            e.getMessage(),
                            true);
                }
            }

            @Override
            public void onError(ErrorEvent event) {
                String code = event.code;
                Log.d(TAG, "data.error " + code);
                runOnUiThread(() -> {
                    statusText.setText("Error");
                    Toast.makeText(MainActivity.this, "Error: " + code, Toast.LENGTH_SHORT).show();
                });
            }

            @Override
            public void onFailure(Throwable error) {
                Log.d(TAG, "failure " + error.getMessage());
                runOnUiThread(() -> {
                    starting = false;
                    callButton.setEnabled(true);
                    duplexSwitch.setEnabled(false);
                    Toast.makeText(
                            MainActivity.this,
                            "Failure: " + error.getMessage(),
                            Toast.LENGTH_SHORT
                    ).show();
                });
            }
        };
    }

    private void stopClient() {
        starting = false;
        XiaogeVoiceSession s = session;
        session = null;
        if (s != null) {
            s.stop();
        }
        runOnUiThread(() -> {
            running = false;
            callButton.setEnabled(true);
            callButton.setText("Start Call");
            duplexSwitch.setEnabled(false);
            statusText.setText("Disconnected");
        });
        Log.d(TAG, "stopped");
    }

    private void clearTranscript() {
        messages.clear();
        pendingUserIndex = -1;
        pendingUtteranceId = null;
        if (adapter != null) {
            adapter.notifyDataSetChanged();
        }
    }

    private void upsertPendingUserMessage(String utteranceId, String text, boolean isFinal) {
        if (TextUtils.isEmpty(text) || TextUtils.isEmpty(text.trim())) {
            return;
        }
        ChatMessageItem tracked = trackedUserItem();
        // 同一 utterance（即使中途被 agent 回复标记完成）始终更新同一条，避免与回复交错时被拆成两条。
        boolean sameUtterance = tracked != null
                && (TextUtils.isEmpty(utteranceId) || utteranceId.equals(pendingUtteranceId));
        // 说话中途停顿超时会被服务端拆成新 utterance_id（旧句永远没有 final），但主 STT 仍整句累积：
        // 新文本是未定稿旧句的延续时并入同一条，只显示最终完整句。
        boolean continuation = tracked != null && tracked.pending()
                && !TextUtils.isEmpty(utteranceId) && isTextContinuation(tracked.text(), text);
        if (continuation) {
            pendingUtteranceId = utteranceId;
        }
        if (tracked != null && (sameUtterance || continuation)) {
            tracked.setText(text);
            tracked.setPending(!isFinal);
            adapter.notifyItemChanged(pendingUserIndex, ChatMessageAdapter.PAYLOAD_TEXT_ONLY);
        } else {
            ChatMessageItem item = ChatMessageItem.pendingUser(text);
            messages.add(item);
            pendingUserIndex = messages.size() - 1;
            pendingUtteranceId = utteranceId;
            adapter.notifyItemInserted(pendingUserIndex);
        }
        scrollToBottom();
    }

    private ChatMessageItem trackedUserItem() {
        if (pendingUserIndex >= 0 && pendingUserIndex < messages.size()) {
            return messages.get(pendingUserIndex);
        }
        return null;
    }

    /** 去掉标点后判断 newText 与 oldText 是否为同一句话的增量（互为前缀，容忍中途重新加标点）。 */
    private static boolean isTextContinuation(String oldText, String newText) {
        String oldStripped = stripPunctuation(oldText);
        String newStripped = stripPunctuation(newText);
        if (oldStripped.isEmpty()) {
            return true;
        }
        return newStripped.startsWith(oldStripped) || oldStripped.startsWith(newStripped);
    }

    private static String stripPunctuation(String text) {
        if (text == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (Character.isLetterOrDigit(c)) {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private void addAssistantMessage(String text) {
        if (TextUtils.isEmpty(text) || TextUtils.isEmpty(text.trim())) {
            return;
        }
        // agent 回复只把当前用户条目标记完成，不释放槽位归属：
        // 回复可能属于上一句，而下一句的 partial 可能已经到达。
        if (pendingUserIndex >= 0 && pendingUserIndex < messages.size()) {
            ChatMessageItem item = messages.get(pendingUserIndex);
            item.setPending(false);
            adapter.notifyItemChanged(pendingUserIndex, ChatMessageAdapter.PAYLOAD_TEXT_ONLY);
        }
        int insertIndex = messages.size();
        messages.add(ChatMessageItem.assistant(text));
        adapter.notifyItemInserted(insertIndex);
        scrollToBottom();
    }

    private void scrollToBottom() {
        if (adapter != null && adapter.getItemCount() > 0) {
            transcriptList.scrollToPosition(adapter.getItemCount() - 1);
        }
    }
}
