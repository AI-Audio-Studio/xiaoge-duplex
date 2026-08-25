package com.xiaoge.client;

import android.content.Context;
import android.util.Log;

import com.xiaoge.client.internal.AudioEngine;
import com.xiaoge.client.internal.AudioFocusController;

import org.json.JSONObject;

import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.OkHttpClient;

/**
 * Mobile voice convenience layer over {@link XiaogeClient}.
 *
 * <p>Owns audio capture/playback, AEC, audio focus, and reconnect. Use {@link XiaogeClient}
 * directly only when the app wants to manage audio itself.
 */
public final class XiaogeVoiceSession {
    private static final String TAG = "XiaogeSession";
    private static final long[] BACKOFF_SEC = {1, 2, 3, 5, 5, 5};
    private static final long SPEAKING_IDLE_MS = 500;

    public enum AudioInputMode {
        /** The SDK captures microphone audio with AudioEngine and sends it upstream. */
        SDK_CAPTURE,
        /** The app pushes upstream PCM with {@link XiaogeVoiceSession#sendPcm(byte[])}. */
        EXTERNAL
    }

    private final Context appContext;
    private final XiaogeConfig config;
    private final XiaogeSessionListener listener;
    private final OkHttpClient http;
    private final AudioInputMode audioInputMode;
    private final ScheduledExecutorService control = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "xiaoge-session");
        t.setDaemon(true);
        return t;
    });
    private final AudioFocusController focus;
    private final AtomicBoolean started = new AtomicBoolean(false);

    private volatile boolean stopped;
    private volatile boolean hasConnectedBefore;
    private volatile SessionState state = SessionState.IDLE;
    private volatile XiaogeClient client;
    private volatile AudioEngine engine;
    private ScheduledFuture<?> pendingReconnect;
    private ScheduledFuture<?> speakingTimer;
    private int backoffIndex;
    private int transportGeneration;
    private int terminalGeneration = -1;

    public XiaogeVoiceSession(Context context, XiaogeConfig config, XiaogeSessionListener listener) {
        this(context, config, listener, new OkHttpClient(), AudioInputMode.SDK_CAPTURE);
    }

    public XiaogeVoiceSession(
            Context context, XiaogeConfig config, XiaogeSessionListener listener, OkHttpClient http) {
        this(context, config, listener, http, AudioInputMode.SDK_CAPTURE);
    }

    public XiaogeVoiceSession(
            Context context,
            XiaogeConfig config,
            XiaogeSessionListener listener,
            AudioInputMode audioInputMode) {
        this(context, config, listener, new OkHttpClient(), audioInputMode);
    }

    public XiaogeVoiceSession(
            Context context,
            XiaogeConfig config,
            XiaogeSessionListener listener,
            OkHttpClient http,
            AudioInputMode audioInputMode) {
        appContext = context.getApplicationContext();
        this.config = config;
        this.listener = listener;
        this.http = http;
        this.audioInputMode = audioInputMode == null ? AudioInputMode.SDK_CAPTURE : audioInputMode;
        focus = new AudioFocusController(appContext, new FocusCallback());
    }

    public SessionState state() {
        return state;
    }

    /** Starts the session. Only callable once. */
    public void start() {
        if (!started.compareAndSet(false, true)) {
            throw new IllegalStateException("session already started");
        }
        executeOnControl(() -> {
            if (!focus.request()) {
                dispatchFailure(new IllegalStateException("failed to gain audio focus"));
            }
            connectOnce();
        });
    }

    /** Stops the session and releases all resources. Idempotent. */
    public void stop() {
        if (stopped) {
            return;
        }
        stopped = true;
        executeOnControl(() -> {
            cancelReconnect();
            cancelSpeakingTimer();
            closeTransport();
            teardownAudio();
            focus.abandon();
            setState(SessionState.STOPPED);
            control.shutdown();
        });
    }

    /** Sends one external upstream PCM frame when using {@link AudioInputMode#EXTERNAL}. */
    public void sendPcm(byte[] pcm) {
        if (audioInputMode != AudioInputMode.EXTERNAL) {
            throw new IllegalStateException("sendPcm is only available in EXTERNAL audio input mode");
        }
        sendPcmToTransport(pcm);
    }

    /** Sends frontend state through the active transport, if connected. */
    public void sendFrontendState(String trustLevel, String wakeState, String vad, int ttlMs) {
        executeOnControl(() -> {
            XiaogeClient c = client;
            if (c != null) {
                try {
                    c.sendFrontendState(trustLevel, wakeState, vad, ttlMs);
                } catch (Exception e) {
                    dispatchFailure(e);
                }
            }
        });
    }

    /** Sends a command acknowledgement for a received {@code data.cmd} payload. */
    public void sendCmdAck(JSONObject cmd, String status, String code) {
        executeOnControl(() -> {
            XiaogeClient c = client;
            if (c != null) {
                try {
                    c.sendCmdAck(cmd, status, code);
                } catch (Exception e) {
                    dispatchFailure(e);
                }
            }
        });
    }

    /** Sends a command acknowledgement for a received command event. */
    public void sendCmdAck(CommandEvent event, String status, String code, String message) {
        executeOnControl(() -> {
            XiaogeClient c = client;
            if (c != null) {
                try {
                    c.sendCmdAck(event, status, code, message);
                } catch (Exception e) {
                    dispatchFailure(e);
                }
            }
        });
    }

    /** Sends a command result for a received {@code data.cmd} payload. */
    public void sendCmdResult(JSONObject cmd, String status, String code) {
        executeOnControl(() -> {
            XiaogeClient c = client;
            if (c != null) {
                try {
                    c.sendCmdResult(cmd, status, code);
                } catch (Exception e) {
                    dispatchFailure(e);
                }
            }
        });
    }

    /** Sends a command result for a received command event. */
    public void sendCmdResult(CommandEvent event, String status, String code, String message, Boolean retryable) {
        executeOnControl(() -> {
            XiaogeClient c = client;
            if (c != null) {
                try {
                    c.sendCmdResult(event, status, code, message, retryable);
                } catch (Exception e) {
                    dispatchFailure(e);
                }
            }
        });
    }

    private void connectOnce() {
        if (stopped) {
            return;
        }
        setState(hasConnectedBefore ? SessionState.RECONNECTING : SessionState.CONNECTING);
        int generation = ++transportGeneration;
        terminalGeneration = -1;
        XiaogeClient c = new XiaogeClient(config, new TransportListener(generation), http);
        client = c;
        try {
            c.start();
        } catch (Exception e) {
            closeTransport();
            dispatchFailure(e);
            scheduleReconnect();
        }
    }

    private void scheduleReconnect() {
        if (stopped) {
            return;
        }
        cancelReconnect();
        long delay = BACKOFF_SEC[Math.min(backoffIndex, BACKOFF_SEC.length - 1)];
        backoffIndex++;
        setState(SessionState.RECONNECTING);
        Log.i(TAG, "reconnect in " + delay + "s (attempt " + backoffIndex + ")");
        pendingReconnect = scheduleOnControl(this::connectOnce, delay, TimeUnit.SECONDS);
    }

    private void cancelReconnect() {
        ScheduledFuture<?> f = pendingReconnect;
        pendingReconnect = null;
        if (f != null) {
            f.cancel(false);
        }
    }

    private void startAudioForNewConnection() {
        teardownAudio();
        AudioEngine e = new AudioEngine(
                appContext,
                this::onMicFrame,
                audioInputMode == AudioInputMode.SDK_CAPTURE);
        try {
            e.start();
            engine = e;
        } catch (Exception ex) {
            dispatchFailure(ex);
        }
    }

    private void teardownAudio() {
        AudioEngine e = engine;
        engine = null;
        if (e != null) {
            e.stop();
        }
    }

    private void onMicFrame(byte[] pcm) {
        sendPcmToTransport(pcm);
    }

    private void sendPcmToTransport(byte[] pcm) {
        XiaogeClient c = client;
        if (c != null) {
            try {
                c.sendPcm(pcm);
            } catch (RuntimeException e) {
                dispatchFailure(e);
            }
        }
    }

    private final class TransportListener implements XiaogeClient.Listener {
        private final int generation;

        private TransportListener(int generation) {
            this.generation = generation;
        }

        @Override
        public void onReady(int sampleRate) {
            executeOnControl(() -> {
                if (stopped || generation != transportGeneration) {
                    return;
                }
                backoffIndex = 0;
                boolean reconnected = hasConnectedBefore;
                hasConnectedBefore = true;
                if (reconnected) {
                    focus.request();
                }
                startAudioForNewConnection();
                setState(SessionState.LISTENING);
                try {
                    listener.onReady(sampleRate, reconnected);
                } catch (Throwable t) {
                    Log.w(TAG, "onReady listener threw", t);
                }
            });
        }

        @Override
        public void onReadyEvent(ReadyEvent event) {
            executeOnControl(() -> {
                if (stopped || generation != transportGeneration) {
                    return;
                }
                boolean reconnected = hasConnectedBefore;
                try {
                    listener.onReadyEvent(event, reconnected);
                } catch (Throwable t) {
                    Log.w(TAG, "onReadyEvent listener threw", t);
                }
            });
        }

        @Override
        public void onAudio(byte[] pcm) {
            AudioEngine e = engine;
            if (e != null) {
                e.enqueuePlayback(pcm);
            }
            markSpeaking();
        }

        @Override
        public void onClear(ClearEvent event) {
            AudioEngine e = engine;
            if (e != null) {
                e.clear();
            }
            executeOnControl(() -> {
                if (!stopped && generation == transportGeneration && isLive()) {
                    setState(SessionState.LISTENING);
                }
                try {
                    listener.onClear(event);
                } catch (Throwable t) {
                    Log.w(TAG, "onClear listener threw", t);
                }
            });
        }

        @Override
        public void onState(StateEvent event) {
            try {
                listener.onServerState(event);
            } catch (Throwable t) {
                Log.w(TAG, "onServerState listener threw", t);
            }
        }

        @Override
        public void onStt(SttEvent event) {
            executeOnControl(() -> {
                if (!stopped && generation == transportGeneration && isLive()) {
                    setState(SessionState.LISTENING);
                }
            });
            try {
                listener.onStt(event);
            } catch (Throwable t) {
                Log.w(TAG, "onStt listener threw", t);
            }
        }

        @Override
        public void onReply(ReplyEvent event) {
            markSpeaking();
            try {
                listener.onReply(event);
            } catch (Throwable t) {
                Log.w(TAG, "onReply listener threw", t);
            }
        }

        @Override
        public void onCommand(CommandEvent event) {
            try {
                listener.onCommand(event);
            } catch (Throwable t) {
                Log.w(TAG, "onCommand listener threw", t);
            }
        }

        @Override
        public void onError(ErrorEvent event) {
            setState(SessionState.ERROR);
            try {
                listener.onError(event);
            } catch (Throwable t) {
                Log.w(TAG, "onError listener threw", t);
            }
        }

        @Override
        public void onJson(JSONObject payload) {
            try {
                listener.onJson(payload);
            } catch (Throwable t) {
                Log.w(TAG, "onJson listener threw", t);
            }
        }

        @Override
        public void onProtocolError(ProtocolErrorEvent event) {
            try {
                listener.onProtocolError(event);
            } catch (Throwable t) {
                Log.w(TAG, "onProtocolError listener threw", t);
            }
        }

        @Override
        public void onFailure(Throwable error) {
            handleTransportFailure(generation, error);
        }
    }

    private void markSpeaking() {
        executeOnControl(() -> {
            if (stopped || !isLive()) {
                return;
            }
            setState(SessionState.AGENT_SPEAKING);
            cancelSpeakingTimer();
            speakingTimer = scheduleOnControl(() -> {
                if (!stopped && state == SessionState.AGENT_SPEAKING) {
                    setState(SessionState.LISTENING);
                }
            }, SPEAKING_IDLE_MS, TimeUnit.MILLISECONDS);
        });
    }

    private void cancelSpeakingTimer() {
        ScheduledFuture<?> f = speakingTimer;
        speakingTimer = null;
        if (f != null) {
            f.cancel(false);
        }
    }

    private boolean isLive() {
        return state == SessionState.READY
                || state == SessionState.LISTENING
                || state == SessionState.AGENT_SPEAKING;
    }

    private final class FocusCallback implements AudioFocusController.Callback {
        @Override
        public void onFocusPaused() {
            AudioEngine e = engine;
            if (e != null) {
                e.pause();
            }
        }

        @Override
        public void onFocusResumed() {
            AudioEngine e = engine;
            if (e != null) {
                e.resume();
            }
        }

        @Override
        public void onFocusLost() {
            Log.i(TAG, "audio focus permanently lost - stop session");
            stop();
        }
    }

    private void setState(SessionState next) {
        if (state == next) {
            return;
        }
        state = next;
        try {
            listener.onState(next);
        } catch (Throwable t) {
            Log.w(TAG, "onState listener threw", t);
        }
    }

    private void dispatchFailure(Throwable t) {
        Log.w(TAG, "session failure", t);
        setState(SessionState.ERROR);
        try {
            listener.onFailure(t);
        } catch (Throwable ignored) {
        }
    }

    private void handleTransportFailure(int generation, Throwable error) {
        executeOnControl(() -> {
            if (stopped || generation != transportGeneration || terminalGeneration == generation) {
                return;
            }
            terminalGeneration = generation;
            closeTransport();
            teardownAudio();
            focus.abandon();
            dispatchFailure(error);
            scheduleReconnect();
        });
    }

    private void closeTransport() {
        XiaogeClient c = client;
        client = null;
        if (c != null) {
            c.close();
        }
    }

    private void executeOnControl(Runnable task) {
        try {
            control.execute(task);
        } catch (RejectedExecutionException rejected) {
            if (!stopped) {
                dispatchFailure(rejected);
            } else {
                Log.d(TAG, "ignore control task after stop");
            }
        }
    }

    private ScheduledFuture<?> scheduleOnControl(Runnable task, long delay, TimeUnit unit) {
        try {
            return control.schedule(task, delay, unit);
        } catch (RejectedExecutionException rejected) {
            if (!stopped) {
                dispatchFailure(rejected);
            } else {
                Log.d(TAG, "ignore control timer after stop");
            }
            return null;
        }
    }
}
