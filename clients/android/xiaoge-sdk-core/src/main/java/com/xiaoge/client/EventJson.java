package com.xiaoge.client;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Iterator;
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

    static void requireOnlyKeys(JSONObject payload, String... allowedKeys) throws JSONException {
        Set<String> allowed = set(allowedKeys);
        Iterator<String> keys = payload.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            if (!allowed.contains(key)) {
                throw new JSONException("unexpected field: " + key);
            }
        }
    }

    static String requiredString(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing string field: " + key);
        }
        Object raw = payload.get(key);
        if (!(raw instanceof String)) {
            throw new JSONException("field must be string: " + key);
        }
        String value = (String) raw;
        if (value.isEmpty()) {
            throw new JSONException("empty string field: " + key);
        }
        return value;
    }

    static String optionalString(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            return null;
        }
        Object raw = payload.get(key);
        if (!(raw instanceof String)) {
            throw new JSONException("field must be string: " + key);
        }
        String value = (String) raw;
        if (value.isEmpty()) {
            throw new JSONException("empty string field: " + key);
        }
        return value;
    }

    static int requiredInt(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing int field: " + key);
        }
        Object raw = payload.get(key);
        if (!(raw instanceof Number) || raw instanceof Double || raw instanceof Float) {
            throw new JSONException("field must be integer: " + key);
        }
        long value = ((Number) raw).longValue();
        if (value < Integer.MIN_VALUE || value > Integer.MAX_VALUE) {
            throw new JSONException("field is outside int range: " + key);
        }
        return (int) value;
    }

    static int requiredIntAtLeast(JSONObject payload, String key, int minimum) throws JSONException {
        int value = requiredInt(payload, key);
        if (value < minimum) {
            throw new JSONException("field must be >= " + minimum + ": " + key);
        }
        return value;
    }

    static int requiredIntEquals(JSONObject payload, String key, int expected) throws JSONException {
        int value = requiredInt(payload, key);
        if (value != expected) {
            throw new JSONException("field must equal " + expected + ": " + key);
        }
        return value;
    }

    static long requiredLong(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing long field: " + key);
        }
        Object raw = payload.get(key);
        if (!(raw instanceof Number) || raw instanceof Double || raw instanceof Float) {
            throw new JSONException("field must be integer: " + key);
        }
        return ((Number) raw).longValue();
    }

    static long requiredLongAtLeast(JSONObject payload, String key, long minimum) throws JSONException {
        long value = requiredLong(payload, key);
        if (value < minimum) {
            throw new JSONException("field must be >= " + minimum + ": " + key);
        }
        return value;
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

    static String[] requiredStringArray(JSONObject payload, String key) throws JSONException {
        if (!payload.has(key) || payload.isNull(key)) {
            throw new JSONException("missing array field: " + key);
        }
        JSONArray array = payload.getJSONArray(key);
        String[] out = new String[array.length()];
        for (int i = 0; i < array.length(); i++) {
            out[i] = array.getString(i);
        }
        return out;
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
