/**
 * Chat (sub-chat) list store: the orchestrator Chat page holds many independent
 * conversations, each keyed by a uuid (conversation id). This module persists the
 * conversation INDEX (id, title, updatedAt) to localStorage so the list survives
 * reloads and the sidebar + the Chat page share one source of truth.
 *
 * The message history per conversation lives on the backend (chat.py keeps it
 * keyed by conversation_id); this store only tracks the list + titles so the UI
 * can show "Chats" like Codex's sidebar and switch between them by id.
 */
import { useCallback, useEffect, useState } from 'react';

export interface ChatMeta {
  id: string;
  title: string;
  updatedAt: number;
}

const KEY = 'agentcore.console.chats';
const EVENT = 'agentcore:chats:changed';

export function newConversationId(): string {
  return `conv_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function read(): ChatMeta[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const list = JSON.parse(raw) as ChatMeta[];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function write(list: ChatMeta[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch { /* private mode: in-memory only for this tab */ }
  // Notify other mounted consumers (sidebar + page) in this tab.
  window.dispatchEvent(new Event(EVENT));
}

/** Create or update a chat's title/timestamp (called on the first user message). */
export function touchChat(id: string, title: string): void {
  const list = read();
  const i = list.findIndex((c) => c.id === id);
  const now = Date.now();
  if (i >= 0) {
    // Keep the first title (the opening message); just bump the timestamp.
    list[i] = { ...list[i]!, title: list[i]!.title || title, updatedAt: now };
  } else {
    list.unshift({ id, title: title || 'New chat', updatedAt: now });
  }
  write(list);
}

export function removeChat(id: string): void {
  write(read().filter((c) => c.id !== id));
  try { localStorage.removeItem(`${KEY}.t.${id}`); } catch { /* ignore */ }
}

// --- Per-chat transcript persistence (R23): a chat's messages survive a full
// page reload (Cmd+R), not just navigation. Stored per conversation id. The type
// is intentionally `unknown[]` here so this store has no dependency on the page's
// ChatItem shape; the page casts it back. ---------------------------------------
export function loadTranscript(id: string): unknown[] {
  try {
    const raw = localStorage.getItem(`${KEY}.t.${id}`);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

export function saveTranscript(id: string, items: unknown[]): void {
  try {
    // Bound the stored size: keep the last 200 items so a very long chat can't
    // blow the localStorage quota.
    localStorage.setItem(`${KEY}.t.${id}`, JSON.stringify(items.slice(-200)));
  } catch { /* quota / private mode: in-memory only */ }
}

/** React hook: the live, sorted (newest first) chat list. */
export function useChats(): { chats: ChatMeta[]; refresh: () => void } {
  const [chats, setChats] = useState<ChatMeta[]>(() =>
    read().slice().sort((a, b) => b.updatedAt - a.updatedAt));

  const refresh = useCallback(() => {
    setChats(read().slice().sort((a, b) => b.updatedAt - a.updatedAt));
  }, []);

  useEffect(() => {
    const on = () => refresh();
    window.addEventListener(EVENT, on);
    window.addEventListener('storage', on);  // cross-tab
    return () => { window.removeEventListener(EVENT, on); window.removeEventListener('storage', on); };
  }, [refresh]);

  return { chats, refresh };
}
