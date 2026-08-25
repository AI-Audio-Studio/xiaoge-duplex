package com.xiaoge.client;

import java.util.List;

public final class SessionInfo {
    public final String traceId;
    public final String sessionId;
    public final String accessToken;
    public final long expiresInMs;
    public final String wsUrl;
    public final List<String> grantedCaps;
    public final String configVersion;

    public SessionInfo(String traceId, String sessionId, String accessToken,
                       long expiresInMs, String wsUrl, List<String> grantedCaps, String configVersion) {
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.accessToken = accessToken;
        this.expiresInMs = expiresInMs;
        this.wsUrl = wsUrl;
        this.grantedCaps = grantedCaps;
        this.configVersion = configVersion;
    }
}
