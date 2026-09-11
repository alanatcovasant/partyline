"""Pydantic models and enums for Covasant Partyline.

These mirror the schema in db.py (agents / messages / checkpoints / claims) and form the
wire contract for both the MCP tools and the REST API. Field set follows PRD §8 (FR1/FR5)
and the §10 schema detail: agents are explicitly registered (owner/repo/branch), messages
carry repo/branch/resource_path/issue_ref context plus a freeform payload and a jsonb
metadata escape hatch, and claims are keyed on (repo, branch, resource_path).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .targets import InvalidTarget, validate_target


def _canonical(value: str) -> str:
    """Reusable validator body: refuse a non-canonical claim target.

    Raising ValueError makes FastAPI answer 422 with the hint in the response body — an
    old client sending repo-relative paths fails loudly and self-explainingly rather than
    quietly claiming a key nobody else will ever match.
    """
    try:
        validate_target(value)
    except InvalidTarget as exc:
        raise ValueError(f"{exc.message} — {exc.hint}") from exc
    return value


class MessageType(str, Enum):
    """Structured message types (PRD §7.3 / §10)."""

    status = "status"
    question = "question"
    claim = "claim"
    release = "release"
    blocked = "blocked"
    done = "done"
    ack = "ack"
    escalation = "escalation"
    summary = "summary"


class ClaimScope(str, Enum):
    exclusive = "exclusive"
    shared_read = "shared_read"
    intent = "intent"


# --------------------------------------------------------------------------- #
# Agents (explicit registration + presence)
# --------------------------------------------------------------------------- #


def _blank_to_none(value: str | None) -> str | None:
    """Optional freeform fields: whitespace-only → null (unknown / not reported)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class RegisterAgent(BaseModel):
    """Register (or refresh) an agent instance (§6, §10)."""

    agent_id: str = Field(..., description="Unique instance name, e.g. 'alan_web_1'")
    owner: str = Field(..., description="Developer or team identity (self-declared)")
    repo: str = Field(..., description="Repository the agent works against")
    branch: str | None = None
    display_name: str | None = None
    # Optional client fingerprint. Not every harness can report these — omit when unknown.
    # Re-register with omit/null preserves previously stored values (COALESCE); use
    # PATCH /api/agents/{id}/runtime to change or clear mid-session.
    harness: str | None = Field(
        None,
        description="Agent harness/runtime, e.g. 'cursor', 'claude-code', 'codex' (optional)",
    )
    model: str | None = Field(
        None,
        description="LLM / model id the session is running, freeform (optional)",
    )
    workdirs: list[str] = Field(
        default_factory=list,
        description="Absolute directories this session owns; claimed as a side effect of "
                    "registering. Overlap is reported, never fatal — presence must not fail.",
    )

    @field_validator("workdirs")
    @classmethod
    def _check_workdirs(cls, v: list[str]) -> list[str]:
        return [_canonical(p) for p in v]

    @field_validator("harness", "model", "display_name")
    @classmethod
    def _optional_text(cls, v: str | None) -> str | None:
        return _blank_to_none(v)


class UpdateAgentRuntime(BaseModel):
    """Mid-session update of optional harness/model (PATCH /api/agents/{id}/runtime).

    Only fields present in the request body are written — omit to leave unchanged,
    send null to clear. Empty body is rejected (422).
    """

    harness: str | None = Field(
        None,
        description="Agent harness/runtime (optional; send null to clear)",
    )
    model: str | None = Field(
        None,
        description="LLM / model id (optional; send null to clear)",
    )

    @field_validator("harness", "model")
    @classmethod
    def _optional_text(cls, v: str | None) -> str | None:
        return _blank_to_none(v)


class Agent(RegisterAgent):
    registered_at: datetime | None = None
    last_heartbeat: datetime | None = None
    # Per-workdir claim outcome from registration: {path, acquired, claim, holder}. A
    # refused workdir is reported here, not raised — registration must never fail on a
    # claim conflict or the agent is invisible to the line.
    #
    # Deliberately NOT named `workdirs`: this model inherits RegisterAgent's
    # `workdirs: list[str]` and its validator, which would then run _canonical() over
    # these dicts. Request and response are different shapes; they get different names.
    workdir_claims: list[dict] = Field(default_factory=list)
    # Most recent `status`-type message this agent posted (for the agent status grid in
    # the dashboard and VS Code extension). Null if the agent has never posted a status.
    last_status: str | None = None
    last_status_at: datetime | None = None
    # Presence (see presence.py): liveness decays, identity never does. `status` is the
    # heartbeat clock ("is this session alive"); `reading` is the checkpoint clock ("is it
    # consuming the line"). They differ, and the difference is what tells a sender whether
    # to expect an answer.
    status: str = Field("stale", description="live | idle | stale (heartbeat clock)")
    reading: bool = Field(False, description="Acked a checkpoint recently (read clock)")
    heartbeat_age_seconds: int | None = None
    read_age_seconds: int | None = Field(
        None, description="Seconds since last ack; null means this agent has never read"
    )


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


class PostMessage(BaseModel):
    """Input for posting a message (FR1)."""

    agent_id: str
    message_type: MessageType = MessageType.status
    recipient: str | None = Field(None, description="Primary addressee ('To:'); still broadcast")
    repo: str | None = None
    branch: str | None = None
    resource_path: str | None = None
    issue_ref: str | None = Field(None, description="GH issue/PR number or URL")
    summary: str | None = Field(
        None,
        description="Concise one-line 'what' (the digest other agents read by default). "
        "Put full detail in payload; readers fetch that on demand.",
    )
    payload: str | None = Field(None, description="Full detail (the 'why/how'): log, prose, context")
    metadata: dict = Field(default_factory=dict, description="Opaque escape hatch (NFR7)")
    mentions: list[str] = Field(default_factory=list)
    tag_everyone: bool = Field(
        False,
        description="Mention every non-stale agent except the sender at write time",
    )
    reply_to_id: int | None = None
    event_time: datetime | None = Field(
        None, description="Agent emit time (ISO-8601). Defaults server-side to now() when omitted."
    )
    allow_unregistered_recipient: bool = Field(
        False,
        description="Permit a recipient that has never registered. Off by default: an "
        "unknown id is a typo, and a typo'd handoff is undeliverable forever. Set it only "
        "when deliberately addressing a session that has not started yet.",
    )


class Message(PostMessage):
    id: int
    author: str | None = Field(None, description="OBO-authenticated principal (audit)")
    conversation_id: str | None = Field(
        None, description="Thread id: inherited from reply_to_id's message, else new"
    )
    created_at: datetime
    received_at: datetime | None = None
    # Delivery feedback, reported at write time rather than left to be inferred from a
    # later silence. The message is durable regardless — these say who was listening.
    recipient_presence: dict | None = Field(
        None, description="Recipient's presence at send time (null when unaddressed)"
    )
    delivery_warnings: list[str] = Field(
        default_factory=list,
        description="Reasons this may not be read soon (stale recipient, unknown mentions)",
    )


# --------------------------------------------------------------------------- #
# Checkpoints
# --------------------------------------------------------------------------- #


class Checkpoint(BaseModel):
    """Per-agent cursor into the message log (FR3/FR4)."""

    agent_id: str
    last_read_message_id: int
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Claims
# --------------------------------------------------------------------------- #


class ClaimRequest(BaseModel):
    """Input for claiming one target (FR5): a directory, a work item, or source."""

    agent_id: str
    repo: str
    resource_path: str = Field(
        ...,
        description="Absolute filesystem path (this machine), issue:owner/repo#N / "
        "pr:owner/repo#N (the task), or src:owner/repo/path (the source, in every clone)",
    )
    branch: str | None = None
    resource_type: str | None = Field(None, description="issue|pr|source|directory|workdir|file")
    claim_scope: ClaimScope = ClaimScope.exclusive
    reason: str | None = None
    ttl_seconds: int | None = None

    @field_validator("resource_path")
    @classmethod
    def _check_resource_path(cls, v: str) -> str:
        return _canonical(v)


class ClaimWorkItemRequest(BaseModel):
    """Lease a work item and the directories it touches, all-or-nothing."""

    agent_id: str
    repo: str
    work_item: str = Field(..., description="issue:owner/repo#N or pr:owner/repo#N")
    paths: list[str] = Field(default_factory=list, description="Absolute directories")
    branch: str | None = None
    claim_scope: ClaimScope = ClaimScope.exclusive
    reason: str | None = None
    ttl_seconds: int | None = None

    @field_validator("work_item")
    @classmethod
    def _check_work_item(cls, v: str) -> str:
        return _canonical(v)

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, v: list[str]) -> list[str]:
        return [_canonical(p) for p in v]


class ReleaseRequest(BaseModel):
    agent_id: str
    repo: str
    resource_path: str

    @field_validator("resource_path")
    @classmethod
    def _check_resource_path(cls, v: str) -> str:
        return _canonical(v)
    branch: str | None = None


class Claim(BaseModel):
    id: UUID
    agent_id: str
    repo: str
    branch: str | None = None
    resource_path: str
    resource_type: str | None = None
    claim_scope: str = "exclusive"
    reason: str | None = None
    created_at: datetime
    expires_at: datetime
