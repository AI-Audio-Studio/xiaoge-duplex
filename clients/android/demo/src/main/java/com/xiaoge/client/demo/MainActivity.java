package com.xiaoge.client.demo;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.text.TextUtils;
import android.util.Log;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;
import androidx.recyclerview.widget.SimpleItemAnimator;

import com.xiaoge.client.ClearEvent;
import com.xiaoge.client.CommandEvent;
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

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

public final class MainActivity extends Activity {
    private static final String TAG = "MainActivity";
    private static final int REQ_AUDIO = 1001;
    private static final String CREATE_SESSION_URL = "https://60.205.197.165:10099/create_session";
    private static final String DEVICE_ID = "android-demo-001";
    private static final String CREDENTIAL_JSON = "{\"type\":\"mock\",\"value\":\"android-demo\"}";
    private static final String CLIENT_VERSION = "xiaoge-android-demo-r5.2.2";
    private static final String API_KEY = BuildConfig.XIAOGE_API_KEY;

    private final List<ChatMessageItem> messages = new ArrayList<>();

    private Button callButton;
    private TextView statusText;
    private RecyclerView transcriptList;
    private ChatMessageAdapter adapter;

    private boolean running;
    private boolean starting;
    private int pendingUserIndex = -1;
    private XiaogeVoiceSession session;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        callButton = findViewById(R.id.callButton);
        statusText = findViewById(R.id.statusText);
        transcriptList = findViewById(R.id.transcriptList);

        adapter = new ChatMessageAdapter(messages);
        LinearLayoutManager layoutManager = new LinearLayoutManager(this);
        layoutManager.setStackFromEnd(true);
        transcriptList.setLayoutManager(layoutManager);
        transcriptList.setAdapter(adapter);
        if (transcriptList.getItemAnimator() instanceof SimpleItemAnimator) {
            ((SimpleItemAnimator) transcriptList.getItemAnimator()).setSupportsChangeAnimations(false);
        }

        callButton.setOnClickListener(v -> toggleCall());
        statusText.setText("Disconnected");
    }

    @Override
    protected void onDestroy() {
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
                    } else if (state == SessionState.LISTENING || state == SessionState.READY) {
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
            public void onStt(SttEvent event) {
                String text = event.text;
                Log.d(TAG, "data.stt " + text);
                runOnUiThread(() -> upsertPendingUserMessage(text));
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
            statusText.setText("Disconnected");
        });
        Log.d(TAG, "stopped");
    }

    private void clearTranscript() {
        messages.clear();
        pendingUserIndex = -1;
        if (adapter != null) {
            adapter.notifyDataSetChanged();
        }
    }

    private void upsertPendingUserMessage(String text) {
        if (TextUtils.isEmpty(text) || TextUtils.isEmpty(text.trim())) {
            return;
        }
        if (pendingUserIndex >= 0 && pendingUserIndex < messages.size()) {
            ChatMessageItem item = messages.get(pendingUserIndex);
            item.setText(text);
            item.setPending(true);
            adapter.notifyItemChanged(pendingUserIndex, ChatMessageAdapter.PAYLOAD_TEXT_ONLY);
        } else {
            ChatMessageItem item = ChatMessageItem.pendingUser(text);
            messages.add(item);
            pendingUserIndex = messages.size() - 1;
            adapter.notifyItemInserted(pendingUserIndex);
        }
        scrollToBottom();
    }

    private void addAssistantMessage(String text) {
        if (TextUtils.isEmpty(text) || TextUtils.isEmpty(text.trim())) {
            return;
        }
        finalizePendingUserMessage();
        int insertIndex = messages.size();
        messages.add(ChatMessageItem.assistant(text));
        adapter.notifyItemInserted(insertIndex);
        scrollToBottom();
    }

    private void finalizePendingUserMessage() {
        if (pendingUserIndex >= 0 && pendingUserIndex < messages.size()) {
            ChatMessageItem item = messages.get(pendingUserIndex);
            item.setPending(false);
            adapter.notifyItemChanged(pendingUserIndex, ChatMessageAdapter.PAYLOAD_TEXT_ONLY);
        }
        pendingUserIndex = -1;
    }

    private void scrollToBottom() {
        if (adapter != null && adapter.getItemCount() > 0) {
            transcriptList.scrollToPosition(adapter.getItemCount() - 1);
        }
    }
}
