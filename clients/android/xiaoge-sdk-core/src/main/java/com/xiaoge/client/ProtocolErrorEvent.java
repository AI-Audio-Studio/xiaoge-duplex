package com.xiaoge.client;

import org.json.JSONObject;

public final class ProtocolErrorEvent {
    public final String message;
    public final String messageType;
    public final JSONObject raw;

    public ProtocolErrorEvent(String message, String messageType, JSONObject raw) {
        this.message = message;
        this.messageType = messageType;
        this.raw = raw;
    }
}
