package com.xiaoge.client;

/** High-level voice session state. */
public enum SessionState {
    /** Created but not started. */
    IDLE,
    /** Establishing transport and session handshake. */
    CONNECTING,
    /** Handshake is complete. */
    READY,
    /** Agent is speaking and downstream audio is arriving. */
    AGENT_SPEAKING,
    /** Audio is live and listening for the user. */
    LISTENING,
    /** Transport failed and reconnect is scheduled or in progress. */
    RECONNECTING,
    /** Session has been stopped by the caller or permanent audio-focus loss. */
    STOPPED,
    /** An error was reported; reconnect may still continue. */
    ERROR
}
