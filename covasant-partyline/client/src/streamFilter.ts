import type { Message } from "./types";

export type StreamFilter = { ftype: string; fq: string };

/** The message-stream filter, extracted pure so it's unit-testable: type first, then a
 *  case-insensitive text match across payload, agent_id, and recipient. Newest-first by
 *  id, not by array position: the seed and the after_id poll can append rows out of
 *  id-order (e.g. a mount-race poll that fetched from id 0), so a bare .reverse() could
 *  surface old messages on top. Sorting by id is deterministic. Input is not mutated. */
export function filterMessages(messages: Message[], { ftype, fq }: StreamFilter): Message[] {
  const needle = fq.trim().toLowerCase();
  return messages
    .slice()
    .sort((a, b) => b.id - a.id)
    .filter((m) => ftype === "all" || m.message_type === ftype)
    .filter(
      (m) =>
        !needle ||
        (m.payload || "").toLowerCase().includes(needle) ||
        m.agent_id.toLowerCase().includes(needle) ||
        (m.recipient || "").toLowerCase().includes(needle)
    );
}

/** The stream window is the newest 500 rows by id, so a conversation that started
 *  earlier may be partially missing. For a full thread the stream view fetches
 *  /api/conversations/{id} like Search does; this helper backs the local fallback:
 *  keep only the given conversation, oldest first (reading order). */
export function conversationThread(messages: Message[], conversationId: string): Message[] {
  return messages
    .filter((m) => m.conversation_id === conversationId)
    .sort((a, b) => a.id - b.id);
}
