package com.xiaoge.client.demo;

import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.TextView;

import androidx.recyclerview.widget.RecyclerView;

import java.util.List;

final class ChatMessageAdapter extends RecyclerView.Adapter<ChatMessageAdapter.MessageViewHolder> {
    static final Object PAYLOAD_TEXT_ONLY = new Object();

    private static final int TYPE_ASSISTANT = 1;
    private static final int TYPE_USER = 2;

    private final List<ChatMessageItem> items;

    ChatMessageAdapter(List<ChatMessageItem> items) {
        this.items = items;
    }

    @Override
    public int getItemViewType(int position) {
        ChatMessageItem item = items.get(position);
        if (ChatMessageItem.ROLE_ASSISTANT.equals(item.role())) {
            return TYPE_ASSISTANT;
        }
        return TYPE_USER;
    }

    @Override
    public MessageViewHolder onCreateViewHolder(ViewGroup parent, int viewType) {
        int layoutId = viewType == TYPE_ASSISTANT
                ? R.layout.item_message_assistant
                : R.layout.item_message_user;
        View view = LayoutInflater.from(parent.getContext()).inflate(layoutId, parent, false);
        return new MessageViewHolder(view);
    }

    @Override
    public void onBindViewHolder(MessageViewHolder holder, int position) {
        holder.messageText.setText(items.get(position).text());
    }

    @Override
    public void onBindViewHolder(
            MessageViewHolder holder,
            int position,
            List<Object> payloads) {
        if (!payloads.isEmpty()) {
            holder.messageText.setText(items.get(position).text());
            return;
        }
        onBindViewHolder(holder, position);
    }

    @Override
    public int getItemCount() {
        return items.size();
    }

    static final class MessageViewHolder extends RecyclerView.ViewHolder {
        private final TextView messageText;

        MessageViewHolder(View itemView) {
            super(itemView);
            messageText = itemView.findViewById(R.id.messageText);
        }
    }
}
