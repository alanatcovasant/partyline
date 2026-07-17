"""Token/byte cost diagnostics for the bridge.

The bridge sees BYTES, not tokens (it never calls a model), so token counts are ESTIMATES
(always prefixed est_). The startup footprint report — the always-on schema+instructions
tax paid every turn — is logged unconditionally; per-call metrics are gated by
PARTYLINE_METRICS=1. See docs/superpowers/specs/2026-07-16-*.
"""
from __future__ import annotations

import json
import logging
import math
import os

log = logging.getLogger("partyline.metrics")

try:
    import tiktoken  # optional
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None


def est_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    return math.ceil(len(text) / 4)


def metrics_enabled() -> bool:
    return os.getenv("PARTYLINE_METRICS", "").strip().lower() in ("1", "true", "yes", "on")


def _tool_text(tool) -> str:
    name = getattr(tool, "name", "") or ""
    desc = getattr(tool, "description", "") or ""
    schema = getattr(tool, "inputSchema", None)
    schema_s = json.dumps(schema, default=str) if schema else ""
    return f"{name}\n{desc}\n{schema_s}"


def footprint(tools: list, instructions: str | None) -> dict:
    per_tool = []
    total = 0
    for t in tools:
        text = _tool_text(t)
        b = len(text.encode("utf-8"))
        per_tool.append({"name": getattr(t, "name", "?"), "bytes": b, "est_tokens": est_tokens(text)})
        total += b
    instr = instructions or ""
    instr_b = len(instr.encode("utf-8"))
    return {
        "total_bytes": total + instr_b,
        "est_total_tokens": est_tokens(instr) + sum(t["est_tokens"] for t in per_tool),
        "instructions_bytes": instr_b,
        "instructions_tokens": est_tokens(instr),
        "per_tool": sorted(per_tool, key=lambda x: x["est_tokens"], reverse=True),
    }


def log_footprint(tools: list, instructions: str | None) -> dict:
    """Always logged (not gated): the per-turn, per-agent floor."""
    fp = footprint(tools, instructions)
    log.info("partyline_footprint %s", json.dumps(fp))
    return fp


def est_tokens_from_bytes(n: int) -> int:
    """Estimate tokens from a byte count without materializing the text (chars/4 proxy)."""
    return math.ceil(n / 4) if n > 0 else 0


def log_call(tool: str, in_bytes: int, out_content_bytes: int, out_structured_bytes: int,
             duration_ms: float, outcome: str) -> None:
    if not metrics_enabled():
        return
    log.info(json.dumps({
        "tool": tool, "in_bytes": in_bytes,
        "out_content_bytes": out_content_bytes, "out_structured_bytes": out_structured_bytes,
        "est_tokens": est_tokens_from_bytes(out_content_bytes + out_structured_bytes),
        "duration_ms": round(duration_ms, 1), "outcome": outcome,
    }))
