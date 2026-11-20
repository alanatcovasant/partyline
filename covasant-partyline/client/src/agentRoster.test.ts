import { describe, expect, it } from "vitest";
import { splitRoster } from "./agentRoster";

const a = (agent_id: string, status?: string) => ({ agent_id, status });

describe("splitRoster", () => {
  it("separates stale agents from live/idle ones", () => {
    const { active, stale } = splitRoster([
      a("live_1", "live"),
      a("idle_1", "idle"),
      a("stale_1", "stale"),
      a("stale_2", "stale"),
    ]);
    expect(active.map((x) => x.agent_id)).toEqual(["live_1", "idle_1"]);
    expect(stale.map((x) => x.agent_id)).toEqual(["stale_1", "stale_2"]);
  });

  it("returns an empty stale list when everyone is active", () => {
    const { active, stale } = splitRoster([a("x", "live")]);
    expect(active).toHaveLength(1);
    expect(stale).toEqual([]);
  });

  it("treats a missing status as active — never hide a row silently", () => {
    const { active, stale } = splitRoster([a("mystery")]);
    expect(active.map((x) => x.agent_id)).toEqual(["mystery"]);
    expect(stale).toEqual([]);
  });

  it("does not mutate the input array", () => {
    const input = [a("s", "stale"), a("l", "live")];
    splitRoster(input);
    expect(input.map((x) => x.agent_id)).toEqual(["s", "l"]);
  });
});
