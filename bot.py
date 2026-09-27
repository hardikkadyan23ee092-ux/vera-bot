"""Submission entry point: compose(category, merchant, trigger, customer) -> dict.

Deterministic for the same inputs (no LLM by default; if VERA_LLM_API_KEY is set, the LLM
runs at temperature 0 and its output is only kept if it passes the grounding validator).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import compose_final  # noqa: E402


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    out = compose_final(category, merchant, trigger, customer)
    body = out.get("body") or ""
    return {
        "body": body,
        "cta": out.get("cta") or ("none" if not body else "open_ended"),
        "send_as": out.get("send_as") or ("merchant_on_behalf" if customer else "vera"),
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('kind')}:{merchant.get('merchant_id')}",
        "rationale": out.get("rationale", ""),
        "template_name": out.get("template_name"),
        "template_params": out.get("template_params") or [],
    }
