package com.xiaoge.client.internal;

import org.junit.Test;

import java.util.function.LongSupplier;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class SpeechEndEstimatorTest {
    /** 640B = 20ms of 16 kHz/16-bit/mono PCM. */
    private static final int FRAME_BYTES = 640;
    private static final long MS_TO_NANOS = 1_000_000L;

    private static final class MutableClock implements LongSupplier {
        long nanos = 1_000_000_000L;

        void advanceMs(long ms) {
            nanos += ms * MS_TO_NANOS;
        }

        @Override
        public long getAsLong() {
            return nanos;
        }
    }

    private final MutableClock clock = new MutableClock();
    private final SpeechEndEstimator estimator = new SpeechEndEstimator(clock);

    /** Constant-amplitude frame; its RMS equals the amplitude. */
    private static byte[] toneFrame(int amplitude) {
        byte[] pcm = new byte[FRAME_BYTES];
        for (int i = 0; i < pcm.length; i += 2) {
            pcm[i] = (byte) (amplitude & 0xFF);
            pcm[i + 1] = (byte) (amplitude >> 8);
        }
        return pcm;
    }

    private static byte[] silenceFrame() {
        return new byte[FRAME_BYTES];
    }

    /** Feeds {@code frames} speech frames of 20ms each, advancing the clock. */
    private void feedSpeech(int frames, int amplitude) {
        for (int i = 0; i < frames; i++) {
            clock.advanceMs(20);
            assertFalse(estimator.onFrame(toneFrame(amplitude), clock.nanos));
        }
    }

    /** Feeds silence frames every 100ms for {@code ms} total; none of them may fire. */
    private void feedSilence(long ms) {
        for (long fed = 0; fed < ms; fed += 100) {
            clock.advanceMs(100);
            assertFalse(estimator.onFrame(silenceFrame(), clock.nanos));
        }
    }

    @Test
    public void silenceNeverFires() {
        for (int i = 0; i < 20; i++) {
            clock.advanceMs(100);
            assertFalse(estimator.onFrame(silenceFrame(), clock.nanos));
        }
    }

    @Test
    public void thresholdIsStrictlyGreaterThanFloor() {
        // RMS exactly at the 250 floor is silence; 251 is speech.
        feedSpeech(13, 250);
        clock.advanceMs(350);
        assertFalse(estimator.onFrame(silenceFrame(), clock.nanos));

        feedSpeech(13, 251);
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
    }

    @Test
    public void speechThenSilenceFiresExactlyOnce() {
        feedSpeech(13, 1000); // 260ms >= 250ms minimum speech
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
        // Fires once per utterance only.
        for (int i = 0; i < 5; i++) {
            clock.advanceMs(100);
            assertFalse(estimator.onFrame(silenceFrame(), clock.nanos));
        }
    }

    @Test
    public void shortSpeechDoesNotFire() {
        feedSpeech(5, 1000); // 100ms < 250ms minimum speech
        feedSilence(600);
        // No fire so far; the next full utterance still works.
        feedSpeech(13, 1000);
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
    }

    @Test
    public void shortPauseMidSpeechDoesNotFire() {
        feedSpeech(7, 1000); // 140ms
        clock.advanceMs(200); // pause well under the 300ms silence threshold
        assertFalse(estimator.onFrame(silenceFrame(), clock.nanos));
        feedSpeech(7, 1000); // resumed speech; accumulated 280ms
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
    }

    @Test
    public void secondUtteranceFiresAgain() {
        feedSpeech(13, 1000);
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));

        feedSpeech(13, 1000);
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
    }

    @Test
    public void nullAndTinyFramesIgnored() {
        assertFalse(estimator.onFrame(null, clock.nanos));
        assertFalse(estimator.onFrame(new byte[0], clock.nanos));
        assertFalse(estimator.onFrame(new byte[1], clock.nanos));
        // State untouched: a normal utterance afterwards still fires.
        feedSpeech(13, 1000);
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
    }

    @Test
    public void resetClearsUtteranceState() {
        feedSpeech(13, 1000);
        estimator.reset();
        feedSilence(600); // no fire: the utterance was dropped by reset
        feedSpeech(13, 1000);
        clock.advanceMs(350);
        assertTrue(estimator.onFrame(silenceFrame(), clock.nanos));
    }

    @Test
    public void lastSpeechRmsTracksLastSpeechFrame() {
        feedSpeech(13, 1000);
        feedSpeech(1, 500);
        assertEquals(500.0, estimator.lastSpeechRms(), 0.1);
    }
}
