package com.xiaoge.client.internal;

import android.annotation.SuppressLint;
import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
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

/** Full-duplex 16 kHz mono PCM capture/playback pipeline with Android voice AEC support. */
public final class AudioEngine {
    private static final String TAG = "XiaogeAudio";
    private static final int FRAME_BYTES = 640;

    public interface FrameSink {
        void onMicFrame(byte[] pcm);
    }

    private final Context appContext;
    private final AudioManager audioManager;
    private final FrameSink sink;
    private final boolean captureEnabled;
    private final BlockingQueue<byte[]> playQueue = new LinkedBlockingQueue<>();
    private final Object playLock = new Object();

    private AudioRecord record;
    private AudioTrack track;
    private AcousticEchoCanceler aec;
    private NoiseSuppressor ns;
    private AutomaticGainControl agc;
    private Thread captureThread;
    private Thread playbackThread;
    private volatile boolean running;
    private int savedMode = AudioManager.MODE_NORMAL;

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
        playQueue.clear();

        if (rec != null) {
            captureThread = new Thread(this::captureLoop, "xiaoge-mic");
            captureThread.setPriority(Thread.MAX_PRIORITY);
            captureThread.start();
        }

        playbackThread = new Thread(this::playbackLoop, "xiaoge-spk");
        playbackThread.setPriority(Thread.MAX_PRIORITY);
        playbackThread.start();

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

    public void clear() {
        playQueue.clear();
        synchronized (playLock) {
            AudioTrack t = track;
            if (t != null) {
                try {
                    t.pause();
                    t.flush();
                    t.play();
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
        while (running) {
            byte[] frame;
            try {
                frame = playQueue.poll(100, TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            if (frame == null) {
                continue;
            }
            synchronized (playLock) {
                AudioTrack t = track;
                if (t != null && running) {
                    try {
                        t.write(frame, 0, frame.length);
                    } catch (Exception e) {
                        Log.w(TAG, "AudioTrack.write error", e);
                    }
                }
            }
        }
    }

    public synchronized void pause() {
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
