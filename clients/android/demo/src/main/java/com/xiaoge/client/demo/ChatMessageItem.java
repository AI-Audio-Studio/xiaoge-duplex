package com.xiaoge.client.demo;

public final class ChatMessageItem {
    public static final String ROLE_ASSISTANT = "assistant";
    public static final String ROLE_USER = "user";

    private final String role;
    private String text;
    private boolean pending;

    private ChatMessageItem(String role, String text, boolean pending) {
        this.role = role;
        this.text = text;
        this.pending = pending;
    }

    public static ChatMessageItem assistant(String text) {
        return new ChatMessageItem(ROLE_ASSISTANT, text, false);
    }

    public static ChatMessageItem pendingUser(String text) {
        return new ChatMessageItem(ROLE_USER, text, true);
    }

    public static ChatMessageItem finalUser(String text) {
        return new ChatMessageItem(ROLE_USER, text, false);
    }

    public String role() {
        return role;
    }

    public String text() {
        return text;
    }

    public void setText(String text) {
        this.text = text;
    }

    public boolean pending() {
        return pending;
    }

    public void setPending(boolean pending) {
        this.pending = pending;
    }
}
