package com.xiaoge.client;

import java.util.Arrays;
import java.util.List;

public final class XiaogeConfig {
    public static final String DEFAULT_API_KEY = "";

    public final String createSessionUrl;
    public final String deviceId;
    public final String credentialJson;
    public final String apiKey;
    public final List<String> caps;
    public final String clientVersion;
    public final String prefsJson;

    public XiaogeConfig(String createSessionUrl, String deviceId, String credentialJson) {
        this(createSessionUrl, deviceId, credentialJson,
                DEFAULT_API_KEY,
                Arrays.asList("audio", "text", "cmd", "state"),
                "xiaoge-android-sdk-r5.2.2", "{}");
    }

    public XiaogeConfig(String createSessionUrl, String deviceId, String credentialJson,
                        List<String> caps, String clientVersion, String prefsJson) {
        this(createSessionUrl, deviceId, credentialJson, DEFAULT_API_KEY, caps, clientVersion, prefsJson);
    }

    public XiaogeConfig(String createSessionUrl, String deviceId, String credentialJson,
                        String apiKey, List<String> caps, String clientVersion, String prefsJson) {
        this.createSessionUrl = createSessionUrl;
        this.deviceId = deviceId;
        this.credentialJson = credentialJson;
        this.apiKey = apiKey == null ? "" : apiKey;
        this.caps = caps;
        this.clientVersion = clientVersion;
        this.prefsJson = prefsJson == null ? "{}" : prefsJson;
        ProtocolCodec.validateCaps(caps);
    }
}
