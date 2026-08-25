package com.xiaoge.client;

import org.json.JSONObject;

/**
 * High-level voice-session callbacks.
 *
 * <p>Callbacks run on SDK internal threads; switch to the main thread for UI work.
 */
public interface XiaogeSessionListener {
    default void onState(SessionState state) {}

    default void onReady(int sampleRate, boolean reconnected) {}

    default void onReadyEvent(ReadyEvent event, boolean reconnected) {}

    default void onClear(ClearEvent event) {}

    default void onServerState(StateEvent event) {}

    default void onStt(SttEvent event) {}

    default void onReply(ReplyEvent event) {}

    default void onCommand(CommandEvent event) {}

    default void onError(ErrorEvent event) {}

    default void onJson(JSONObject payload) {}

    default void onProtocolError(ProtocolErrorEvent event) {}

    default void onFailure(Throwable error) {}
}
