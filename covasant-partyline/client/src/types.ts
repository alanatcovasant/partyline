export type Message = {
  id: number;
  agent_id: string;
  author?: string | null;
  message_type: string;
  recipient?: string | null;
  repo?: string | null;
  branch?: string | null;
  resource_path?: string | null;
  issue_ref?: string | null;
  summary?: string | null;
  payload?: string | null;
  metadata?: Record<string, unknown> | null;
  mentions?: string[] | null;
  reply_to_id?: number | null;
  conversation_id?: string | null;
  created_at: string;
  event_time?: string | null;
  received_at?: string | null;
};
// Search hits and conversation rows are Messages plus a source tag (live|archive).
export type SearchHit = Message & { source?: string };
export type Agent = {
  agent_id: string;
  owner?: string | null;
  repo?: string | null;
  branch?: string | null;
  harness?: string | null;
  model?: string | null;
  last_heartbeat: string;
  last_status?: string | null;
  last_status_at?: string | null;
};

// An agent counts as "online" if it has heartbeat within this window (every partyline
// call refreshes the heartbeat). Older-but-still-listed agents render as offline.
export const ONLINE_WITHIN_SECS = 120;
export type Claim = {
  id: string;
  resource_path: string;
  repo: string;
  branch?: string | null;
  agent_id: string;
  resource_type?: string | null;
  reason?: string | null;
  expires_at: string;
};
export type Metrics = {
  total_messages: number;
  messages_last_1m: number;
  messages_last_5m: number;
  messages_last_1h: number;
  active_agents_5m: number;
  active_agents_1h: number;
  active_claims: number;
  last_message_at: string | null;
  unique_posters_1h: number;
  sse_subscribers: number;
};
export type Diagnostics = {
  active_within_seconds: number;
  total_agents: number;
  active_agents: number;
  stale_agents: number;
  oldest_heartbeat: string | null;
  newest_heartbeat: string | null;
  sampled_agents: Array<{
    agent_id: string;
    repo?: string | null;
    branch?: string | null;
    heartbeat_age_seconds: number;
    is_active: boolean;
  }>;
};
export type Health = {
  status: string;
  db_configured: boolean;
  version?: string;
  build_timestamp?: string;
  git_commit?: string;
  build_host?: string;
};

export const MESSAGE_TYPES = [
  "status", "question", "claim", "release", "blocked",
  "done", "ack", "escalation", "summary",
] as const;
