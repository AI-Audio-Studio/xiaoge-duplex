package com.xiaoge.client;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Set;

/**
 * Lenient extraction helpers for downlink event payloads.
 *
 * <p>Extraction never throws for field-level deviations (missing fields, unexpected fields,
 * type mismatches, unknown enum values): the event is always built with type defaults and each
 * deviation is recorded in the caller's warnings list instead. Only {@link #requireType} still
 * fails, and dispatch guarantees it matches before extraction runs. Unexpected fields are
 * reported but ignored; the full payload stays available through each event's {@code raw}.
 */
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

    /** Reports fields outside the known set; never rejects the payload. */
    static void warnUnknownKeys(JSONObject payload, List<String> warnings, String... knownKeys) {
        Set<String> known = set(knownKeys);
        Iterator<String> keys = payload.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            if (!known.contains(key)) {
                warnings.add("unexpected field: " + key);
            }
        }
    }

    /** Missing or null fields extract as {@code ""}; non-string values are stringified. */
    static String string(JSONObject payload, String key, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            warnings.add("missing field: " + key);
            return "";
        }
        if (!(raw instanceof String)) {
            warnings.add("field is not a string: " + key);
        }
        return payload.optString(key, "");
    }

    /** Optional string: missing or null extracts as {@code null} without warning. */
    static String optionalString(JSONObject payload, String key, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            return null;
        }
        if (!(raw instanceof String)) {
            warnings.add("field is not a string: " + key);
        }
        return payload.optString(key, "");
    }

    /** Missing or unparseable fields extract as {@code 0}; numeric strings are coerced. */
    static long longValue(JSONObject payload, String key, List<String> warnings) {
        Long value = optLongOrNull(payload, key, warnings);
        return value == null ? 0L : value;
    }

    /** Like {@link #longValue} but missing or unparseable fields fall back to {@code fallback}. */
    static int intOrDefault(JSONObject payload, String key, int fallback, List<String> warnings) {
        Long value = optLongOrNull(payload, key, warnings);
        if (value == null) {
            return fallback;
        }
        if (value < Integer.MIN_VALUE || value > Integer.MAX_VALUE) {
            warnings.add("field is outside int range: " + key);
            return fallback;
        }
        return (int) (long) value;
    }

    /** Missing or unparseable fields extract as {@code false}; "true"/"false" strings coerce. */
    static boolean bool(JSONObject payload, String key, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            warnings.add("missing field: " + key);
            return false;
        }
        if (raw instanceof Boolean) {
            return (Boolean) raw;
        }
        if (raw instanceof String) {
            warnings.add("field is coerced from string: " + key);
        } else {
            warnings.add("field is not a boolean: " + key);
        }
        return payload.optBoolean(key, false);
    }

    /** Missing or non-object fields extract as an empty object copy. */
    static JSONObject object(JSONObject payload, String key, List<String> warnings) throws JSONException {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            warnings.add("missing field: " + key);
            return new JSONObject();
        }
        JSONObject value = payload.optJSONObject(key);
        if (value == null) {
            warnings.add("field is not an object: " + key);
            return new JSONObject();
        }
        return copy(value);
    }

    /** Optional object: missing, null, or non-object fields extract as {@code null}. */
    static JSONObject optionalObject(JSONObject payload, String key, List<String> warnings)
            throws JSONException {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            return null;
        }
        JSONObject value = payload.optJSONObject(key);
        if (value == null) {
            warnings.add("field is not an object: " + key);
            return null;
        }
        return copy(value);
    }

    /** Unknown enum values are passed through with a warning instead of rejected. */
    static String enumValue(JSONObject payload, String key, Set<String> valid, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            warnings.add("missing field: " + key);
            return "";
        }
        String value = payload.optString(key, "");
        if (!valid.contains(value)) {
            warnings.add("unknown " + key + ": " + value);
        }
        return value;
    }

    /** Optional enum: missing or null extracts as {@code null}; unknown values pass through. */
    static String optionalEnum(JSONObject payload, String key, Set<String> valid, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            return null;
        }
        String value = payload.optString(key, "");
        if (!valid.contains(value)) {
            warnings.add("unknown " + key + ": " + value);
        }
        return value;
    }

    /** Missing or non-array fields extract as an empty array; entries are stringified. */
    static String[] stringArray(JSONObject payload, String key, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            warnings.add("missing field: " + key);
            return new String[0];
        }
        JSONArray array = payload.optJSONArray(key);
        if (array == null) {
            warnings.add("field is not an array: " + key);
            return new String[0];
        }
        String[] out = new String[array.length()];
        for (int i = 0; i < array.length(); i++) {
            out[i] = array.optString(i, "");
        }
        return out;
    }

    static Set<String> set(String... values) {
        return new HashSet<>(Arrays.asList(values));
    }

    private static Long optLongOrNull(JSONObject payload, String key, List<String> warnings) {
        Object raw = payload.opt(key);
        if (raw == null || raw == JSONObject.NULL) {
            warnings.add("missing field: " + key);
            return null;
        }
        if (raw instanceof Number) {
            if (raw instanceof Double || raw instanceof Float) {
                warnings.add("field is not an integer: " + key);
            }
            return ((Number) raw).longValue();
        }
        if (raw instanceof String) {
            try {
                long parsed = Long.parseLong(((String) raw).trim());
                warnings.add("field is coerced from string: " + key);
                return parsed;
            } catch (NumberFormatException ignored) {
                warnings.add("field is not a number: " + key);
                return null;
            }
        }
        warnings.add("field is not a number: " + key);
        return null;
    }
}
