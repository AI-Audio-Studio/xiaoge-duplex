package com.xiaoge.client;

import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

public final class XiaogeClient {
    private static final String TAG = "XiaogeClient";
    public interface Listener {
        default void onReady(int sampleRate) {}
        default void onAudio(byte[] pcm) {}
        default void onJson(JSONObject payload) {}
        default void onReadyEvent(ReadyEvent event) {}
        default void onClear(ClearEvent event) {}
        default void onState(StateEvent event) {}
        default void onStt(SttEvent event) {}
        default void onReply(ReplyEvent event) {}
        default void onCommand(CommandEvent event) {}
        default void onError(ErrorEvent event) {}
        default void onProtocolError(ProtocolErrorEvent event) {}
        default void onFailure(Throwable error) {}
    }

    private final XiaogeConfig config;
    private final Listener listener;
    private final OkHttpClient http;
    private final ExecutorService callbackExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "xiaoge-cb");
        t.setDaemon(true);
        return t;
    });
    private SessionInfo session;
    private WebSocket ws;
    private int frontendSeq;

    public XiaogeClient(XiaogeConfig config, Listener listener) {
        this(config, listener, new OkHttpClient());
    }

    public XiaogeClient(XiaogeConfig config, Listener listener, OkHttpClient http) {
        this.config = config;
        this.listener = listener;
        this.http = http;
    }

    public void start() throws Exception {
        String requestJson = ProtocolCodec.createSessionRequest(config);
        RequestBody body = RequestBody.create(requestJson, MediaType.get("application/json"));
        Request.Builder createBuilder = new Request.Builder().url(config.createSessionUrl).post(body);
        addApiKey(createBuilder);
        Request req = createBuilder.build();
        try (Response resp = http.newCall(req).execute()) {
            if (!resp.isSuccessful() || resp.body() == null) {
                throw new IllegalStateException("create_session failed: " + resp.code());
            }
            session = ProtocolCodec.parseSession(resp.body().string());
        }
        Request.Builder wsBuilder = new Request.Builder()
                .url(session.wsUrl)
                .header("Authorization", "Bearer " + session.accessToken);
        Request wsReq = wsBuilder.build();
        ws = http.newWebSocket(wsReq, new SocketListener());
    }

    private void addApiKey(Request.Builder builder) {
        if (config.apiKey != null && !config.apiKey.isEmpty()) {
            builder.header("x-api-key", config.apiKey);
        }
    }

    public void sendPcm(byte[] pcm) {
        if (pcm.length > ProtocolCodec.BINARY_MAX_BYTES) {
            throw new IllegalArgumentException("PCM frame exceeds 32768 bytes");
        }
        WebSocket socket = ws;
        if (socket != null) {
            socket.send(ByteString.of(pcm));
        }
    }

    public void sendFrontendState(String trustLevel, String wakeState, String vad, int ttlMs) throws Exception {
        frontendSeq++;
        sendJson(ProtocolCodec.frontendState(session, frontendSeq, trustLevel, wakeState, vad, ttlMs));
    }

    public void sendCmdAck(JSONObject cmd, String status, String code) throws Exception {
        sendJson(ProtocolCodec.cmdAck(cmd, status, code));
    }

    public void sendCmdAck(CommandEvent event, String status, String code, String message) throws Exception {
        sendJson(ProtocolCodec.cmdAck(event, status, code, message));
    }

    public void sendCmdResult(JSONObject cmd, String status, String code) throws Exception {
        sendJson(ProtocolCodec.cmdResult(cmd, status, code));
    }

    public void sendCmdResult(CommandEvent event, String status, String code, String message, Boolean retryable)
            throws Exception {
        sendJson(ProtocolCodec.cmdResult(event, status, code, message, retryable));
    }

    public void close() {
        WebSocket socket = ws;
        ws = null;
        if (socket != null) {
            socket.close(1000, "client close");
        }
        callbackExecutor.shutdown();
    }

    private void sendJson(String text) {
        WebSocket socket = ws;
        if (socket != null) {
            socket.send(text);
            Log.d(TAG, "sendJson: "+text);
        }
    }

    private void dispatch(Runnable task) {
        try {
            callbackExecutor.execute(() -> {
                try {
                    task.run();
                } catch (Throwable t) {
                    Log.w(TAG, "listener callback threw", t);
                    try {
                        listener.onFailure(t);
                    } catch (Throwable ignored) {
                    }
                }
            });
        } catch (RejectedExecutionException ignored) {
            Log.d(TAG, "callback executor rejected");
        }
    }

    private final class SocketListener extends WebSocketListener {
        @Override
        public void onOpen(WebSocket webSocket, Response response) {
            try {
                sendJson(ProtocolCodec.hello(config, session));
            } catch (Exception e) {
                dispatch(() -> listener.onFailure(e));
            }
        }

        @Override
        public void onMessage(WebSocket webSocket, String text) {
            Log.d(TAG, "onMessage: "+text);
            try {
                JSONObject payload = new JSONObject(text);
                String type = payload.optString("type", "unknown");
                switch (type) {
                    case "ctrl.ready": {
                        ReadyEvent event = ReadyEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onReadyEvent(event);
                            listener.onReady(event.sampleRate);
                        });
                        break;
                    }
                    case "ctrl.clear": {
                        Log.d(TAG, " ctrl.clear: " + text);
                        ClearEvent event = ClearEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onClear(event);
                        });
                        break;
                    }
                    case "ctrl.state": {
                        StateEvent event = StateEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onState(event);
                        });
                        break;
                    }
                    case "data.stt": {
                        Log.d(TAG, "data.stt: " + text);
                        SttEvent event = SttEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onStt(event);
                        });
                        break;
                    }
                    case "data.reply": {
                        Log.d(TAG, "data.reply: " + text);
                        ReplyEvent event = ReplyEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onReply(event);
                        });
                        break;
                    }
                    case "data.cmd": {
                        CommandEvent event = CommandEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onCommand(event);
                        });
                        break;
                    }
                    case "data.error": {
                        ErrorEvent event = ErrorEvent.fromJson(payload);
                        dispatch(() -> {
                            listener.onJson(payload);
                            listener.onError(event);
                        });
                        break;
                    }
                    default:
                        dispatch(() -> listener.onJson(payload));
                        break;
                }
            } catch (JSONException | IllegalArgumentException e) {
                dispatch(() -> {
                    JSONObject raw = null;
                    try {
                        raw = new JSONObject(text);
                    } catch (Exception ignored) {
                    }
                    String messageType = raw == null ? "unknown" : raw.optString("type", "unknown");
                    listener.onProtocolError(new ProtocolErrorEvent(e.getMessage(), messageType, raw));
                });
            } catch (Exception e) {
                dispatch(() -> listener.onFailure(e));
            }
        }

        @Override
        public void onMessage(WebSocket webSocket, ByteString bytes) {
            Log.d(TAG, "onMessage:onAudio ");
            byte[] pcm = bytes.toByteArray();
            dispatch(() -> listener.onAudio(pcm));
        }

        @Override
        public void onClosed(WebSocket webSocket, int code, String reason) {
            callbackExecutor.shutdown();
        }

        @Override
        public void onFailure(WebSocket webSocket, Throwable t, Response response) {
            dispatch(() -> listener.onFailure(t));
            callbackExecutor.shutdown();
        }
    }
}
