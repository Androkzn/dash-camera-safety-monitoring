/**
 * CopilotPanel — LLM chat widget. State + send/receive lives in useChat.
 */

import { useRef, useEffect, type FormEvent, type KeyboardEvent } from "react";

import { useChat } from "../hooks/useChat";

import styles from "./CopilotPanel.module.css";

const CHIPS = [
  { label: "Any high-risk pedestrian events?", query: "Any high-risk pedestrian events in the last 2 minutes?" },
  { label: "Medium-risk SLA?", query: "What's our SLA for medium-risk events?" },
  { label: "Summarize last 10 events", query: "Summarize the last 10 events." },
];

export function CopilotPanel() {
  const { messages, loading, send } = useChat();

  const chatRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    chatRef.current?.scrollTo(0, chatRef.current.scrollHeight);
  }, [messages]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = textareaRef.current?.value ?? "";
    if (q.trim()) {
      send(q);
      if (textareaRef.current) textareaRef.current.value = "";
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e as unknown as FormEvent);
    }
  };

  const handleChipClick = (query: string) => {
    if (textareaRef.current) {
      textareaRef.current.value = query;
      textareaRef.current.focus();
    }
  };

  return (
    <section className={styles.panel}>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>
            Copilot{" "}
            <span className={styles.sub}>RAG over statutes + live events</span>
          </h2>
        </div>
      </div>

      <div className={styles.chat} ref={chatRef}>
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`${styles.msg} ${styles[msg.role]} ${msg.isError ? styles.err : ""}`}
          >
            {msg.text}
          </div>
        ))}
      </div>

      <div className={styles.chips}>
        {CHIPS.map((chip) => (
          <button
            key={chip.label}
            className={styles.chip}
            type="button"
            onClick={() => handleChipClick(chip.query)}
          >
            {chip.label}
          </button>
        ))}
      </div>

      <form className={styles.compose} onSubmit={handleSubmit} autoComplete="off">
        <textarea
          ref={textareaRef}
          placeholder="Ask Copilot… (Enter to send, Shift+Enter = newline)"
          onKeyDown={handleKeyDown}
        />
        <button type="submit" disabled={loading}>
          {loading ? "Thinking…" : "Ask"}
        </button>
      </form>
    </section>
  );
}
