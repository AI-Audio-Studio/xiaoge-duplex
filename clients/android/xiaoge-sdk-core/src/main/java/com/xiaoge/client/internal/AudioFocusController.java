package com.xiaoge.client.internal;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.os.Build;
import android.util.Log;

/** Manages voice-call audio focus and reports focus changes to the session layer. */
public final class AudioFocusController {
    private static final String TAG = "XiaogeFocus";

    public interface Callback {
        void onFocusPaused();

        void onFocusResumed();

        void onFocusLost();
    }

    private final AudioManager audioManager;
    private final Callback callback;
    private final AudioManager.OnAudioFocusChangeListener listener;
    private AudioFocusRequest focusRequest;

    public AudioFocusController(Context context, Callback callback) {
        audioManager = (AudioManager) context.getApplicationContext().getSystemService(Context.AUDIO_SERVICE);
        this.callback = callback;
        listener = change -> {
            switch (change) {
                case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT:
                case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK:
                    this.callback.onFocusPaused();
                    break;
                case AudioManager.AUDIOFOCUS_GAIN:
                    this.callback.onFocusResumed();
                    break;
                case AudioManager.AUDIOFOCUS_LOSS:
                    this.callback.onFocusLost();
                    break;
                default:
                    break;
            }
        };
    }

    public boolean request() {
        int result;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            AudioFocusRequest req = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build())
                    .setOnAudioFocusChangeListener(listener)
                    .build();
            focusRequest = req;
            result = audioManager.requestAudioFocus(req);
        } else {
            result = requestLegacy();
        }
        boolean granted = result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        Log.i(TAG, "request audio focus granted=" + granted);
        return granted;
    }

    @SuppressWarnings("deprecation")
    private int requestLegacy() {
        return audioManager.requestAudioFocus(
                listener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN);
    }

    public void abandon() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (focusRequest != null) {
                audioManager.abandonAudioFocusRequest(focusRequest);
                focusRequest = null;
            }
        } else {
            abandonLegacy();
        }
    }

    @SuppressWarnings("deprecation")
    private void abandonLegacy() {
        audioManager.abandonAudioFocus(listener);
    }
}
