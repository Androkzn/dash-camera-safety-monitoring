/**
 * useChat — small state machine for the dashboard Copilot chat panel.
 * Holds the rolling message list, a `loading` flag, and a `send(text)`
 * function that appends a user turn, hits the chat API, then appends
 * the bot reply (or an error bubble on failure).
 */
import { useState, useCallback } from "react";

import { dashboardApi } from "../api";

interface ChatMessage {
  id: string;
  role: "user" | "bot";
  text: string;
  isError?: boolean;
}

let msgCounter = 0;

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "bot",
      text: "Ask about recent events, risk patterns, or road safety policy.",
    },
  ]);
  const [loading, setLoading] = useState(false);

  const send = useCallback(
    async (query: string) => {
      const trimmed = query.trim();
      if (!trimmed || loading) return;

      const userId = `msg-${++msgCounter}`;
      const botId = `msg-${++msgCounter}`;

      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", text: trimmed },
      ]);
      setLoading(true);

      try {
        const { answer } = await dashboardApi.chat(trimmed);
        setMessages((prev) => [
          ...prev,
          { id: botId, role: "bot", text: answer || "(no answer)" },
        ]);
      } catch (err) {
        setMessages((prev) => [
          ...prev,
          {
            id: botId,
            role: "bot",
            text: `(error: ${err instanceof Error ? err.message : err})`,
            isError: true,
          },
        ]);
      } finally {
        setLoading(false);
      }
    },
    [loading],
  );

  return { messages, loading, send };
}
