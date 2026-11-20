import { describe, expect, it } from "vitest";
import type { Message } from "./types";
import { conversationThread, filterMessages } from "./streamFilter";

const m = (id: number, over: Partial<Message> = {}): Message => ({
  id,
  agent_id: "alan_partyline_1",
  message_type: "status",
  created_at: "2026-11-20T00:00:00Z",
  ...over,
});

describe("filterMessages", () => {
  const messages = [
    m(1, { message_type: "status", payload: "starting work" }),
    m(2, { message_type: "question", payload: "who owns db.py?", recipient: "grok_partyline_1", agent_id: "bob_1" }),
    m(3, { message_type: "done", payload: "finished the split" }),
  ];

  it("returns all messages for the default filter", () => {
    expect(filterMessages(messages, { ftype: "all", fq: "" })).toHaveLength(3);
  });

  it("filters by message type", () => {
    const out = filterMessages(messages, { ftype: "question", fq: "" });
    expect(out.map((x) => x.id)).toEqual([2]);
  });

  it("filters by text across payload, agent_id, and recipient", () => {
    expect(filterMessages(messages, { ftype: "all", fq: "owns" }).map((x) => x.id)).toEqual([2]);
    expect(filterMessages(messages, { ftype: "all", fq: "bob" }).map((x) => x.id)).toEqual([2]);
    expect(filterMessages(messages, { ftype: "all", fq: "grok" }).map((x) => x.id)).toEqual([2]);
  });

  it("combines type and text filters", () => {
    expect(filterMessages(messages, { ftype: "status", fq: "owns" })).toHaveLength(0);
    expect(filterMessages(messages, { ftype: "question", fq: "owns" }).map((x) => x.id)).toEqual([2]);
  });

  it("sorts newest-first by id regardless of input order", () => {
    const out = filterMessages([m(3), m(1), m(2)], { ftype: "all", fq: "" });
    expect(out.map((x) => x.id)).toEqual([3, 2, 1]);
  });

  it("does not mutate the input array", () => {
    const input = [m(1), m(2)];
    filterMessages(input, { ftype: "all", fq: "" });
    expect(input.map((x) => x.id)).toEqual([1, 2]);
  });
});

describe("conversationThread", () => {
  const messages = [
    m(9, { conversation_id: "abc", payload: "newest reply" }),
    m(4, { conversation_id: "abc", payload: "thread root" }),
    m(5, { conversation_id: "other" }),
    m(7, { conversation_id: "abc", payload: "middle" }),
    m(8),
  ];

  it("keeps only the given conversation, oldest first", () => {
    expect(conversationThread(messages, "abc").map((x) => x.id)).toEqual([4, 7, 9]);
  });

  it("returns an empty list for an unknown conversation", () => {
    expect(conversationThread(messages, "nope")).toEqual([]);
  });

  it("ignores messages with no conversation_id", () => {
    expect(conversationThread(messages, "abc").some((x) => x.id === 8)).toBe(false);
  });
});
