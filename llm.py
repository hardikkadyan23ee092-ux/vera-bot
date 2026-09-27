"""Optional LLM layer (stdlib only). Off unless VERA_LLM_API_KEY is set.

Used for two things, both gated by the validator so a bad generation can never ship:
  1. polish()  — rephrase a deterministic draft (only when VERA_LLM_POLISH=1)
  2. answer()  — answer an on-topic merchant question mid-conversation
If the call fails, times out, or the output fails validation, callers fall back to the
deterministic text.
"""
from __future__ import annotations

import json
import os
import re
from urllib import request as urlrequest

PROVIDER = os.environ.get("VERA_LLM_PROVIDER", "anthropic").lower()
API_KEY = os.environ.get("VERA_LLM_API_KEY", "")
MODEL = os.environ.get("VERA_LLM_MODEL", "")
POLISH = os.environ.get("VERA_LLM_POLISH", "0") == "1"
TIMEOUT = float(os.environ.get("VERA_LLM_TIMEOUT", "7"))

_cache: dict[str, str] = {}


def enabled() -> bool:
    return bool(API_KEY)


def model_name() -> str:
    if not enabled():
        return "deterministic-playbooks (no LLM)"
    return MODEL or {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-4o-mini"}.get(PROVIDER, "unknown")


def complete(system: str, user: str, max_tokens: int = 500) -> str | None:
    if not enabled():
        return None
    key = f"{system}\n---\n{user}"
    if key in _cache:
        return _cache[key]
    try:
        if PROVIDER == "anthropic":
            body = {"model": model_name(), "max_tokens": max_tokens, "temperature": 0,
                    "system": system, "messages": [{"role": "user", "content": user}]}
            req = urlrequest.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                     headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
            data = json.loads(urlrequest.urlopen(req, timeout=TIMEOUT).read())
            text = data["content"][0]["text"]
        else:  # openai-compatible
            base = os.environ.get("VERA_LLM_BASE_URL", "https://api.openai.com/v1")
            body = {"model": model_name(), "max_tokens": max_tokens, "temperature": 0,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            req = urlrequest.Request(f"{base}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
            data = json.loads(urlrequest.urlopen(req, timeout=TIMEOUT).read())
            text = data["choices"][0]["message"]["content"]
        _cache[key] = text
        return text
    except Exception:
        return None


def _extract_json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


POLISH_SYSTEM = """You edit WhatsApp messages written by Vera, magicpin's merchant assistant.
Rewrite the DRAFT so it reads more naturally, keeping it the same length or shorter.
HARD RULES:
- Keep every fact, number, name, date, price and source exactly as in the draft. Add NO new numbers or facts.
- Keep the salutation at the start and the single call-to-action as the last sentence.
- Keep the same language mix (English / Hinglish) as the draft.
- No URLs, no preamble, no self-introduction.
- Never use these words: {taboos}
Return JSON only: {{"body": "..."}}"""


def polish(draft: str, taboos: list[str]) -> str | None:
    if not (enabled() and POLISH and draft):
        return None
    out = _extract_json(complete(POLISH_SYSTEM.format(taboos=", ".join(taboos) or "none"), f"DRAFT:\n{draft}"))
    return (out or {}).get("body")


ANSWER_SYSTEM = """You are Vera, magicpin's merchant assistant on WhatsApp, mid-conversation with a merchant.
Answer the merchant's question in 1-3 short sentences using ONLY the FACTS given. If the facts don't
contain the answer, say you'll check and get back — never guess numbers. Then end with one short
question that moves the original task forward. Match the merchant's language (Hinglish if they wrote Hinglish).
No URLs. Never use: {taboos}.
Return JSON only: {{"body": "..."}}"""


def answer(question: str, facts: dict, taboos: list[str]) -> str | None:
    if not enabled():
        return None
    user = f"FACTS:\n{json.dumps(facts, ensure_ascii=False)[:6000]}\n\nMERCHANT QUESTION:\n{question}"
    out = _extract_json(complete(ANSWER_SYSTEM.format(taboos=", ".join(taboos) or "none"), user, 300))
    return (out or {}).get("body")
