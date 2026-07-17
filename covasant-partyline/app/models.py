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

from pydantic import BaseModel, Field


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


class RegisterAgent(BaseModel):
    """Register (or refresh) an agent instance (§6, §10)."""

    agent_id: str = Field(..., description="Unique instance name, e.g. 'alan_aura_3'")
    owner: str = Field(..., description="Developer or team identity (self-declared)")
    repo: str = Field(..., description="Repository the agent works against")
    branch: str | None = None
    display_name: str | None = None


class Agent(RegisterAgent):
    registered_at: datetime | None = None
    last_heartbeat: datetime | None = None
    # Most recent `status`-type message this agent posted (for the agent status grid in
    # the dashboard and VS Code extension). Null if the agent has never posted a status.
    last_status: str | None = None
    last_status_at: datetime | None = None


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
    reply_to_id: int | None = None
    event_time: datetime | None = Field(
        None, description="Agent emit time (ISO-8601). Defaults server-side to now() when omitted."
    )


class Message(PostMessage):
    id: int
    author: str | None = Field(None, description="OBO-authenticated principal (audit)")
    conversation_id: str | None = Field(
        None, description="Thread id: inherited from reply_to_id's message, else new"
    )
    created_at: datetime
    received_at: datetime | None = None


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
    """Input for claiming a resource (FR5)."""

    agent_id: str
    repo: str
    resource_path: str
    branch: str | None = None
    resource_type: str | None = Field(None, description="file|directory|module|service|migration")
    claim_scope: ClaimScope = ClaimScope.exclusive
    reason: str | None = None
    ttl_seconds: int | None = None


class ReleaseRequest(BaseModel):
    agent_id: str
    repo: str
    resource_path: str
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
