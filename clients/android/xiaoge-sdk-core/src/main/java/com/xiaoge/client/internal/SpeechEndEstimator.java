package com.xiaoge.client.internal;

import java.util.function.LongSupplier;

/**
 * Local "the user just stopped speaking" estimate from mic PCM, used to arm the
 * playback pipeline pre-warm ahead of the server's STT final.
 *
 * <p>State machine (ported from the verified vnine v5 tuning, 2026-09-24 duplex6.log:
 * 15/15 turns at 127-165ms write-to-first-presented): counts speech milliseconds with
 * a permissive absolute RMS threshold, and fires once per utterance after
 * {@code >= MIN_SPEECH_MS} of speech followed by {@code >= SILENCE_MS} without a
 * speech frame. A resumed utterance re-arms the estimate.
 *
 * <p>The threshold is deliberately a plain absolute floor, with no noise-floor factor:
 * deciding "the user may have finished" only costs a few seconds of low-duty pre-warm
 * silence on a false fire, while deciding "definitely speaking" (conservative VAD)
 * missed ~50% of utterances in the reference measurements (speech RMS 616-768 barely
 * above 600, digital-zero frames collapsing the rolling noise floor). Keep this
 * threshold independent of any noise estimate.
 *
 * <p>Pure logic, no Android dependencies; the clock is injected for JVM unit tests.
 * Not thread-safe: feed it from one thread (the capture thread).
 */
final class SpeechEndEstimator {
    /** Permissive speech floor for the end estimate; tune here if quiet speech misses. */
    static final double SPEECH_END_ABS_RMS = 250.0;
    /** Minimum accumulated speech before an end estimate can fire. */
    static final double SPEECH_END_MIN_SPEECH_MS = 250.0;
    /** Silence after the last speech frame before an end estimate fires. */
    static final long SPEECH_END_SILENCE_MS = 300L;
    private static final long MS_TO_NANOS = 1_000_000L;
    /** 16 samples per millisecond at 16 kHz. */
    private static final double SAMPLES_PER_MS = 16.0;

    private final LongSupplier clock;

    private double speechMs;
    private long lastSpeechAtNanos;
    private double lastSpeechRms;
    /** True until the first speech frame and again after a fire; guards one fire per utterance. */
    private boolean fired = true;

    SpeechEndEstimator() {
        this(System::nanoTime);
    }

    SpeechEndEstimator(LongSupplier clock) {
        this.clock = clock;
    }

    /** Feeds one mic frame using the injected clock. */
    boolean onFrame(byte[] pcm) {
        return onFrame(pcm, clock.getAsLong());
    }

    /**
     * Feeds one mic frame (16 kHz/16-bit/mono little-endian PCM, any length) with an
     * explicit clock reading. Returns true exactly once per utterance.
     */
    boolean onFrame(byte[] pcm, long nowNanos) {
        if (pcm == null || pcm.length < 2) {
            return false;
        }
        int sampleCount = pcm.length / 2;
        double energy = 0.0;
        for (int i = 1; i < pcm.length; i += 2) {
            int sample = (short) ((pcm[i - 1] & 0xFF) | (pcm[i] << 8));
            energy += sample * (double) sample;
        }
        double rms = Math.sqrt(energy / sampleCount);
        if (rms > SPEECH_END_ABS_RMS) {
            speechMs += sampleCount / SAMPLES_PER_MS;
            lastSpeechAtNanos = nowNanos;
            lastSpeechRms = rms;
            // A resumed utterance re-arms its end estimate.
            fired = false;
            return false;
        }
        if (lastSpeechAtNanos == 0L || fired) {
            return false;
        }
        if (nowNanos - lastSpeechAtNanos >= SPEECH_END_SILENCE_MS * MS_TO_NANOS
                && speechMs >= SPEECH_END_MIN_SPEECH_MS) {
            fired = true;
            speechMs = 0.0;
            return true;
        }
        return false;
    }

    /** RMS of the last speech frame, for the mic_speech_end log line. */
    double lastSpeechRms() {
        return lastSpeechRms;
    }

    /** Clears the accumulated utterance state; call on engine restart. */
    void reset() {
        speechMs = 0.0;
        lastSpeechAtNanos = 0L;
        lastSpeechRms = 0.0;
        fired = true;
    }
}
