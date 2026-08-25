package com.xiaoge.client;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

final class EventJson {
    private EventJson() {}

    static JSONObject copy(JSONObject payload) throws JSONException {
        return new JSONObject(payload.toString());
    }

    static void requireType(JSONObject payload, String type) throws JSONException {
        if (!type.equals(payload.optString("type", ""))) {
            throw new JSONException("expected type " + type);
        }
    }

    static String requiredString(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing string field: " + key);
        }
        String value = payload.getString(key);
        if (value.isEmpty()) {
            throw new JSONException("empty string field: " + key);
        }
        return value;
    }

    static String optionalString(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            return null;
        }
        return payload.getString(key);
    }

    static int requiredInt(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing int field: " + key);
        }
        return payload.getInt(key);
    }

    static long requiredLong(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing long field: " + key);
        }
        return payload.getLong(key);
    }

    static boolean requiredBoolean(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing boolean field: " + key);
        }
        Object value = payload.get(key);
        if (!(value instanceof Boolean)) {
            throw new JSONException("field must be boolean: " + key);
        }
        return (Boolean) value;
    }

    static JSONObject requiredObject(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing object field: " + key);
        }
        return copy(payload.getJSONObject(key));
    }

    static String requiredEnum(JSONObject payload, String key, Set<String> valid) throws JSONException {
        String value = requiredString(payload, key);
        if (!valid.contains(value)) {
            throw new JSONException("invalid " + key + ": " + value);
        }
        return value;
    }

    static String optionalEnum(JSONObject payload, String key, Set<String> valid) throws JSONException {
        String value = optionalString(payload, key);
        if (value != null && !valid.contains(value)) {
            throw new JSONException("invalid " + key + ": " + value);
        }
        return value;
    }

    static String[] optionalStringArray(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            return new String[0];
        }
        JSONArray array = payload.getJSONArray(key);
        String[] out = new String[array.length()];
        for (int i = 0; i < array.length(); i++) {
            out[i] = array.getString(i);
        }
        return out;
    }

    static Set<String> set(String... values) {
        return new HashSet<>(Arrays.asList(values));
    }
}
