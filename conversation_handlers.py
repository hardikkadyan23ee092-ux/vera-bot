"""Optional multi-turn entry point: respond(state, merchant_message) -> dict.

`state` is a plain dict:
    {
      "conversation_id": "...", "from_role": "merchant" | "customer",
      "category": {...}, "merchant": {...}, "trigger": {...}, "customer": {... or None},
      "turns": [{"from": "bot"|"merchant"|"customer", "body": "..."}, ...]
    }
Returns {"action": "send"|"wait"|"end", "body"?, "cta"?, "wait_seconds"?, "rationale"}.
The same state machine backs the live /v1/reply endpoint.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import replies  # noqa: E402
from engine import Engine  # noqa: E402


def respond(state: dict, merchant_message: str) -> dict:
    eng = Engine()
    m = state.get("merchant") or {}
    t = state.get("trigger") or {}
    c = state.get("customer")
    cat = state.get("category") or {}
    mid = m.get("merchant_id", "m_unknown")
    eng.push_context("category", cat.get("slug") or m.get("category_slug") or "unknown", 1, cat)
    eng.push_context("merchant", mid, 1, m)
    if t:
        eng.push_context("trigger", t.get("id", "trg_unknown"), 1, t)
    if c:
        eng.push_context("customer", c.get("customer_id", "c_unknown"), 1, c)
    conv_id = state.get("conversation_id", "conv_state")
    turns = list(state.get("turns") or [])
    first_bot = next((x.get("body") for x in turns if x.get("from") == "bot"), None)
    eng.conversations[conv_id] = {
        "conversation_id": conv_id, "merchant_id": mid, "customer_id": (c or {}).get("customer_id"),
        "trigger_id": t.get("id"), "kind": t.get("kind"), "send_as": "merchant_on_behalf" if c else "vera",
        "status": "active", "bot_turns": sum(1 for x in turns if x.get("from") == "bot"), "commit_count": 0,
        "turns": turns, "first_body": first_bot,
    }
    # replay prior inbound turns so auto-reply / commitment counters are correct
    ms = eng.mstate(mid)
    inbound = [x.get("body", "") for x in turns if x.get("from") in ("merchant", "customer")]
    for prev in inbound:
        if replies.classify(prev, ms.get("last_inbound")) == "auto_reply":
            ms["auto_count"] += 1
        else:
            ms["auto_count"] = 0
        ms["last_inbound"] = prev
    return replies.handle(eng, {
        "conversation_id": conv_id, "merchant_id": mid, "customer_id": (c or {}).get("customer_id"),
        "from_role": state.get("from_role", "customer" if c else "merchant"), "message": merchant_message,
        "received_at": state.get("received_at"), "turn_number": len(turns) + 1,
    })
