package com.xiaoge.client.internal;

import android.annotation.SuppressLint;
import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTimestamp;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.util.Log;

import com.xiaoge.client.ProtocolCodec;

import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Full-duplex 16 kHz mono PCM capture/playback pipeline with Android voice AEC support.
 *
 * <p>Playback keep-warm: between turns the output path (audio HAL, and the A2DP stream
 * behind it) suspends after roughly 0.65-3.3s without data, so the next reply's first
 * chunk pays a reopen cost of several hundred ms (verified on-device baseline:
 * write-to-first-presented median 736ms). Modeled on the verified vnine v5 design
 * (xiaoge-duplex-latency-analysis.md: 736ms reduced to 127-165ms on 15/15 turns),
 * the playback loop, while a pre-warm is armed, feeds one small silence frame per
 * 100ms poll straight into the resident track until the first real frame arrives,
 * keeping the pipeline hot through the reply-anticipation window. The arm is a
 * volatile deadline set:
 * <ul>
 *   <li>on the local mic speech-end estimate ({@link SpeechEndEstimator}, leads the
 *       server's STT final by 1.7-3.7s),</li>
 *   <li>at engine start (covers the greeting turn and reconnects),</li>
 *   <li>by {@link #requestStreamPrewarm()} from the session layer on STT final as a
 *       fallback (also the only arm in external audio input mode).</li>
 * </ul>
 * Silence never enters {@code playQueue}; a false arm only streams low-duty silence
 * for {@link #PREWARM_SUSTAIN_MS}.
 */
public final class AudioEngine {
    private static final String TAG = "XiaogeAudio";
    private static final int FRAME_BYTES = 640;

    // --- Playback keep-warm constants (see class header). ---

    /** How long an arm keeps feeding silence without real audio before giving up. */
    private static final long PREWARM_SUSTAIN_MS = 8000L;
    /** 640B = 20ms of silence at 16 kHz/16-bit/mono, per 100ms poll. */
    private static final byte[] PREWARM_SILENCE = new byte[FRAME_BYTES];
    /** 100ms polls with an empty queue that mark the end of an utterance (~800ms total). */
    private static final int IDLE_POLLS_FOR_NEW_UTTERANCE = 8;
    /**
     * Background heartbeat: does not prevent HAL dormancy by itself, but keeps the
     * output stream mostly PLAYING so a pre-warm often starts from a shallower state.
     * Off by default (production posture per the latency analysis: the pre-warm alone
     * keeps the fast lane; the heartbeat costs ~8% duty-cycle silence while idle).
     */
    private static final boolean KEEPALIVE_ENABLED = false;
    private static final long KEEPALIVE_INTERVAL_MS = 2000L;
    /** 2560B = 80ms of silence. */
    private static final byte[] KEEPALIVE_SILENCE = new byte[FRAME_BYTES * 4];

    public interface FrameSink {
        void onMicFrame(byte[] pcm);
    }

    private final Context appContext;
    private final AudioManager audioManager;
    private final FrameSink sink;
    private final boolean captureEnabled;
    private final BlockingQueue<byte[]> playQueue = new LinkedBlockingQueue<>();
    private final Object playLock = new Object();
    private final SpeechEndEstimator speechEndEstimator = new SpeechEndEstimator();

    private AudioRecord record;
    private AudioTrack track;
    private AcousticEchoCanceler aec;
    private NoiseSuppressor ns;
    private AutomaticGainControl agc;
    private Thread captureThread;
    private Thread playbackThread;
    private volatile boolean running;
    private int savedMode = AudioManager.MODE_NORMAL;

    /** Armed pre-warm deadline (wall clock ms); real frames clear it. 0 = not armed. */
    private volatile long prewarmActiveUntilMs;
    /**
     * True between {@link #pause()} and {@link #resume()}: the paused track consumes
     * nothing, so any write would fill its buffer and block the playback thread while
     * it holds playLock. All track writes (silence and real frames) are gated on this.
     */
    private volatile boolean paused;
    /** Set by {@link #clear()}: the next frame taken starts a new utterance. */
    private volatile boolean forceNewUtteranceMarker;

    // Latency instrumentation for the first-word latency analysis, modeled on the vnine
    // instrumentation. All of this state belongs to the playback thread alone.
    private final AudioTimestamp audioTimestamp = new AudioTimestamp();
    /** True from the utterance's first write until a sample shows frames presented past the baseline. */
    private boolean awaitingFirstPresented;
    /** Frame position frozen while the track was starved; presentation past it = audible. -1 = not captured. */
    private long utteranceBaselineFrames = -1L;
    private long utteranceFirstWriteAtMs;

    public AudioEngine(Context context, FrameSink sink) {
        this(context, sink, true);
    }

    public AudioEngine(Context context, FrameSink sink, boolean captureEnabled) {
        appContext = context.getApplicationContext();
        audioManager = (AudioManager) appContext.getSystemService(Context.AUDIO_SERVICE);
        this.sink = sink;
        this.captureEnabled = captureEnabled;
    }

    @SuppressLint("MissingPermission")
    public synchronized void start() {
        if (running) {
            return;
        }
        savedMode = audioManager.getMode();
        audioManager.setMode(AudioManager.MODE_NORMAL);

        AudioRecord rec = null;
        if (captureEnabled) {
            int recMin = AudioRecord.getMinBufferSize(
                    ProtocolCodec.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT);
            int recBuf = Math.max(recMin, FRAME_BYTES * 4);
            rec = new AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    ProtocolCodec.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    recBuf);
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
                rec.release();
                audioManager.setMode(savedMode);
                throw new IllegalStateException("AudioRecord initialization failed");
            }
            record = rec;
            attachEffects(rec.getAudioSessionId());
        }

        int trkMin = AudioTrack.getMinBufferSize(
                ProtocolCodec.SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        int trkBuf = Math.max(trkMin, FRAME_BYTES * 8);
        AudioTrack trk = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                .setAudioFormat(new AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(ProtocolCodec.SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build())
                .setBufferSizeInBytes(trkBuf)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build();
        track = trk;
        trk.play();
        if (rec != null) {
            rec.startRecording();
        }

        running = true;
        paused = false;
        forceNewUtteranceMarker = false;
        speechEndEstimator.reset();
        awaitingFirstPresented = false;
        utteranceBaselineFrames = -1L;
        playQueue.clear();

        if (rec != null) {
            captureThread = new Thread(this::captureLoop, "xiaoge-mic");
            captureThread.setPriority(Thread.MAX_PRIORITY);
            captureThread.start();
        }

        playbackThread = new Thread(this::playbackLoop, "xiaoge-spk");
        playbackThread.setPriority(Thread.MAX_PRIORITY);
        playbackThread.start();

        // Arm the pre-warm right away: the greeting turn's first chunk (and the first
        // turn after a reconnect) then lands on a warm pipeline too.
        requestStreamPrewarm();

        Log.i(TAG, "audio engine started (capture=" + captureEnabled
                + ", sessionId=" + (rec != null ? rec.getAudioSessionId() : -1)
                + ", aec=" + (aec != null) + ")");
    }

    private void attachEffects(int sessionId) {
        if (AcousticEchoCanceler.isAvailable()) {
            try {
                aec = AcousticEchoCanceler.create(sessionId);
                if (aec != null) {
                    aec.setEnabled(true);
                }
            } catch (Exception e) {
                Log.w(TAG, "failed to create AEC", e);
            }
        } else {
            Log.w(TAG, "system AEC unavailable");
        }
        if (NoiseSuppressor.isAvailable()) {
            try {
                ns = NoiseSuppressor.create(sessionId);
                if (ns != null) {
                    ns.setEnabled(true);
                }
            } catch (Exception ignored) {
            }
        }
        if (AutomaticGainControl.isAvailable()) {
            try {
                agc = AutomaticGainControl.create(sessionId);
                if (agc != null) {
                    agc.setEnabled(true);
                }
            } catch (Exception ignored) {
            }
        }
    }

    public void enqueuePlayback(byte[] pcm) {
        if (running && pcm != null && pcm.length > 0) {
            playQueue.offer(pcm);
        }
    }

    /**
     * Arms a sustained pre-warm: the playback loop feeds silence every 100ms poll
     * until the first real frame arrives or the arm times out, keeping the mixer/HAL
     * output path hot through the reply-anticipation window. Armed automatically on
     * the local mic speech-end estimate (well before the server's STT final) and at
     * engine start; the session layer arms it again on STT final as a fallback.
     * Thread-safe: a single volatile write plus one log line.
     */
    public void requestStreamPrewarm() {
        prewarmActiveUntilMs = System.currentTimeMillis() + PREWARM_SUSTAIN_MS;
        Log.d(TAG, "latency_node=stream_prewarm_armed ts=" + System.currentTimeMillis()
                + " sustainMs=" + PREWARM_SUSTAIN_MS);
    }

    public void clear() {
        playQueue.clear();
        forceNewUtteranceMarker = true;
        synchronized (playLock) {
            AudioTrack t = track;
            if (t != null) {
                try {
                    t.pause();
                    t.flush();
                    if (!paused) {
                        t.play();
                    }
                } catch (IllegalStateException e) {
                    Log.w(TAG, "clear playback failed", e);
                }
            }
        }
    }

    private void captureLoop() {
        byte[] buf = new byte[FRAME_BYTES];
        AudioRecord rec = record;
        while (running && rec != null) {
            int n = rec.read(buf, 0, buf.length);
            if (n > 0) {
                byte[] frame = new byte[n];
                System.arraycopy(buf, 0, frame, 0, n);
                // Local speech-end estimate on the frames we ship anyway: it leads the
                // server's STT final by 1.7-3.7s, giving the pre-warm enough runway.
                if (speechEndEstimator.onFrame(frame)) {
                    Log.d(TAG, "latency_node=mic_speech_end ts=" + System.currentTimeMillis()
                            + " lastSpeechRms=" + (int) speechEndEstimator.lastSpeechRms());
                    requestStreamPrewarm();
                }
                try {
                    sink.onMicFrame(frame);
                } catch (Throwable t) {
                    Log.w(TAG, "mic sink threw", t);
                }
            } else if (n < 0) {
                Log.w(TAG, "AudioRecord.read error: " + n);
                break;
            }
        }
    }

    private void playbackLoop() {
        // Consecutive 100ms polls with an empty queue; reaching the threshold means
        // the utterance ended, so the next frame taken is a new one's first write.
        // Starts saturated so the session's first frame counts as a first write.
        int idlePolls = IDLE_POLLS_FOR_NEW_UTTERANCE;
        // Wall clock of the last write, real or keep-alive; 0 = heartbeat at once.
        long lastWriteAtMs = 0L;
        while (running) {
            byte[] frame;
            try {
                frame = playQueue.poll(100, TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            if (frame == null) {
                if (idlePolls < IDLE_POLLS_FOR_NEW_UTTERANCE) {
                    idlePolls++;
                }
                long now = System.currentTimeMillis();
                if (!paused && now < prewarmActiveUntilMs) {
                    // Sustained pre-warm: one small silence frame per poll keeps data
                    // flowing (the output path only sleeps after ~650ms+ idle) without
                    // queueing silence ahead of the reply's first frame. Not logged
                    // per write.
                    writeSilence(PREWARM_SILENCE, null);
                    lastWriteAtMs = now;
                } else if (KEEPALIVE_ENABLED && !paused
                        && (lastWriteAtMs == 0L || now - lastWriteAtMs >= KEEPALIVE_INTERVAL_MS)) {
                    writeSilence(KEEPALIVE_SILENCE, "keepalive_silence");
                    lastWriteAtMs = now;
                }
                continue;
            }
            if (paused) {
                // Focus pause: the track consumes nothing, and pause() already drops
                // queued frames, so frames arriving during the pause are dropped too
                // instead of blocking the loop on a full track buffer.
                continue;
            }
            // Real data arrived: end the pre-warm so silence never queues ahead of TTS.
            prewarmActiveUntilMs = 0L;
            boolean firstWriteOfUtterance = idlePolls >= IDLE_POLLS_FOR_NEW_UTTERANCE
                    || forceNewUtteranceMarker;
            forceNewUtteranceMarker = false;
            idlePolls = 0;
            synchronized (playLock) {
                AudioTrack t = track;
                if (t != null && running) {
                    if (firstWriteOfUtterance) {
                        logUtteranceFirstWrite(t, frame.length);
                    }
                    try {
                        t.write(frame, 0, frame.length);
                    } catch (Exception e) {
                        Log.w(TAG, "AudioTrack.write error", e);
                    }
                    if (awaitingFirstPresented) {
                        sampleFirstPresented(t);
                    }
                }
            }
            lastWriteAtMs = System.currentTimeMillis();
        }
    }

    /**
     * Writes silence straight into the resident track, bypassing playQueue (queue
     * semantics unchanged). Runs the mixer and pushes data through the output path.
     * Pass a null node for the per-write-logging-free path used by the sustained
     * pre-warm.
     */
    private void writeSilence(byte[] silence, String node) {
        synchronized (playLock) {
            AudioTrack t = track;
            if (t != null && running) {
                try {
                    t.write(silence, 0, silence.length);
                } catch (Exception e) {
                    Log.w(TAG, (node != null ? node : "silence") + " write error", e);
                }
            }
        }
        if (node != null) {
            Log.d(TAG, "latency_node=" + node + " ts=" + System.currentTimeMillis()
                    + " bytes=" + silence.length);
        }
    }

    /** Logs the utterance's first write and freezes the presented-frames baseline. */
    private void logUtteranceFirstWrite(AudioTrack t, int bytes) {
        utteranceFirstWriteAtMs = System.currentTimeMillis();
        // Baseline BEFORE the write: while starved the track presents nothing, so the
        // frozen framePosition marks the last presented frame; any growth past it
        // means this utterance has become audible. Pre-warm silence may have advanced
        // the position since, but the baseline is captured fresh here either way.
        utteranceBaselineFrames = sampleFramePosition(t);
        awaitingFirstPresented = true;
        Log.d(TAG, "latency_node=tts_first_write ts=" + utteranceFirstWriteAtMs
                + " bytes=" + bytes
                + " baselineFrames=" + utteranceBaselineFrames);
    }

    /** Last-known frame position, or -1 when the track cannot report one right now. */
    private long sampleFramePosition(AudioTrack t) {
        try {
            return t.getTimestamp(audioTimestamp) ? audioTimestamp.framePosition : -1L;
        } catch (IllegalStateException e) {
            return -1L;
        }
    }

    /**
     * Samples the track timestamp after a write; the first sample whose framePosition
     * exceeds the utterance baseline marks the first frame handed to the output path.
     * getTimestamp is a quick non-blocking native call, safe on the playback thread; a
     * failed sample just retries on the next write and the utterance going idle
     * silently gives up.
     */
    private void sampleFirstPresented(AudioTrack t) {
        try {
            if (!t.getTimestamp(audioTimestamp)) {
                return;
            }
            long presentedFrames = audioTimestamp.framePosition;
            long presentedAtNanos = audioTimestamp.nanoTime;
            // Same time base as AudioTimestamp.nanoTime. Do NOT use
            // SystemClock.elapsedRealtimeNanos(): it includes suspend time and diverges
            // from AudioTimestamp on devices that slept, breaking the wall-clock mapping.
            long monoNowNanos = System.nanoTime();
            long sampleAtMs = System.currentTimeMillis();
            if (utteranceBaselineFrames < 0L) {
                // Baseline capture failed before the first write; take it now. It may
                // already include a few presented frames of this utterance, making the
                // logged first-presented an upper bound.
                utteranceBaselineFrames = presentedFrames;
                return;
            }
            if (presentedFrames <= utteranceBaselineFrames) {
                return;
            }
            awaitingFirstPresented = false;
            long presentedAtMs = sampleAtMs
                    - TimeUnit.NANOSECONDS.toMillis(monoNowNanos - presentedAtNanos);
            Log.d(TAG, "latency_node=playback_first_presented"
                    + ", presentedAt=" + presentedAtMs
                    + ", firstWriteAt=" + utteranceFirstWriteAtMs
                    + ", firstWriteToFirstPresentedMs=" + (presentedAtMs - utteranceFirstWriteAtMs)
                    + ", baselineFrames=" + utteranceBaselineFrames
                    + ", presentedFrames=" + presentedFrames
                    + ", sampleRate=" + ProtocolCodec.SAMPLE_RATE);
        } catch (IllegalStateException e) {
            // Track released between write and timestamp sampling; skip this sample.
        }
    }

    public synchronized void pause() {
        paused = true;
        AudioRecord rec = record;
        AudioTrack t = track;
        try {
            if (rec != null && rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                rec.stop();
            }
        } catch (IllegalStateException ignored) {
        }
        synchronized (playLock) {
            if (t != null) {
                try {
                    t.pause();
                    t.flush();
                } catch (IllegalStateException ignored) {
                }
            }
        }
        playQueue.clear();
    }

    @SuppressLint("MissingPermission")
    public synchronized void resume() {
        paused = false;
        AudioRecord rec = record;
        AudioTrack t = track;
        try {
            if (rec != null && rec.getState() == AudioRecord.STATE_INITIALIZED) {
                rec.startRecording();
            }
        } catch (IllegalStateException ignored) {
        }
        synchronized (playLock) {
            if (t != null) {
                try {
                    t.play();
                } catch (IllegalStateException ignored) {
                }
            }
        }
    }

    public synchronized void stop() {
        if (!running && record == null) {
            return;
        }
        running = false;
        prewarmActiveUntilMs = 0L;
        paused = false;

        Thread cap = captureThread;
        Thread play = playbackThread;
        captureThread = null;
        playbackThread = null;
        if (play != null) {
            play.interrupt();
        }
        joinQuietly(cap);
        joinQuietly(play);

        releaseEffects();

        AudioRecord rec = record;
        record = null;
        if (rec != null) {
            try {
                if (rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    rec.stop();
                }
            } catch (IllegalStateException ignored) {
            }
            rec.release();
        }

        AudioTrack t = track;
        track = null;
        if (t != null) {
            try {
                t.pause();
                t.flush();
                t.stop();
            } catch (IllegalStateException ignored) {
            }
            t.release();
        }

        playQueue.clear();
        try {
            audioManager.setMode(savedMode);
        } catch (Exception ignored) {
        }
        Log.i(TAG, "audio engine stopped");
    }

    private void releaseEffects() {
        if (aec != null) {
            try {
                aec.release();
            } catch (Exception ignored) {
            }
            aec = null;
        }
        if (ns != null) {
            try {
                ns.release();
            } catch (Exception ignored) {
            }
            ns = null;
        }
        if (agc != null) {
            try {
                agc.release();
            } catch (Exception ignored) {
            }
            agc = null;
        }
    }

    private static void joinQuietly(Thread t) {
        if (t != null) {
            try {
                t.join(500);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }
}
