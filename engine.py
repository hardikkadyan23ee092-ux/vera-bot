"""Stateful engine: versioned context store, tick decisioning, conversation registry."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, timezone

import llm
from composer import compose_raw
from facts import trigger_is_expired
from util import parse_dt
from validator import check

SCOPES = ("category", "merchant", "customer", "trigger")
MAX_ACTIONS_PER_TICK = 20
TICK_BUDGET_S = 8.0


def compose_final(category, merchant, trigger, customer=None, extra=None) -> dict:
    """Deterministic composition + validation + optional LLM polish (kept only if it validates)."""
    out = compose_raw(category, merchant, trigger, customer, extra)
    F = out.pop("_facts")
    if out.get("skip_reason") or not out.get("body"):
        return out
    ctxs = (category, merchant, trigger, customer)
    errs = check(out["body"], contexts=ctxs, taboos=F.taboos)
    out["validation"] = errs
    if llm.enabled() and llm.POLISH:
        cand = llm.polish(out["body"], F.taboos)
        if cand and not check(cand, contexts=ctxs, taboos=F.taboos) and len(cand) <= len(out["body"]) * 1.25:
            first = F.cust_name if customer else F.first_name
            if not first or first.lower() in cand.lower():
                out["body"] = cand
                out["rationale"] += " [LLM-polished; validated]"
    return out


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.started = time.time()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict] = {}
            self.sent_suppression: set[str] = set()
            self.sent_triggers: set[str] = set()
            self.deferred: dict[str, str] = {}         # trigger_id -> reason deferred
            self.conversations: dict[str, dict] = {}
            self.merchant_state: dict[str, dict] = {}
            self.bodies_by_merchant: dict[str, set] = {}
            self.decision_log: list[dict] = []

    # ------------------------------------------------------------ contexts
    def get(self, scope: str, cid: str | None):
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def push_context(self, scope: str, cid: str, version: int, payload: dict):
        with self.lock:
            key = (scope, cid)
            cur = self.contexts.get(key)
            if cur and cur["version"] >= version:
                return False, cur["version"]
            self.contexts[key] = {"version": version, "payload": payload,
                                  "stored_at": datetime.now(timezone.utc).isoformat()}
            # a re-pushed trigger with a higher version is a fresh event
            if scope == "trigger":
                self.sent_triggers.discard(cid)
            return True, version

    def counts(self) -> dict:
        c = {s: 0 for s in SCOPES}
        for (s, _) in self.contexts:
            c[s] = c.get(s, 0) + 1
        return c

    def mstate(self, mid: str) -> dict:
        return self.merchant_state.setdefault(mid or "_unknown", {
            "opted_out": False, "auto_count": 0, "last_inbound": None, "wait_until": None,
            "last_conv_id": None,
        })

    # ------------------------------------------------------------ tick
    def _category_for(self, merchant: dict, trigger: dict):
        slug = (merchant or {}).get("category_slug") or (trigger.get("payload") or {}).get("category")
        return self.get("category", slug)

    def _candidate(self, tid: str, now: str):
        trg = self.get("trigger", tid)
        if not trg:
            return None, "trigger not in store"
        if tid in self.sent_triggers:
            return None, "already sent"
        sk = trg.get("suppression_key")
        if sk and sk in self.sent_suppression:
            return None, f"suppressed ({sk})"
        if trigger_is_expired(trg, now):
            return None, "expired"
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = self.get("merchant", mid)
        if not merchant:
            return None, f"merchant {mid} not loaded"
        category = self._category_for(merchant, trg)
        if not category:
            return None, "category not loaded"
        ms = self.mstate(mid)
        if ms["opted_out"]:
            return None, "merchant opted out"
        wu = parse_dt(ms.get("wait_until"))
        nw = parse_dt(now)
        if wu and nw and nw < wu and trg.get("scope") != "customer":
            return None, "backing off (auto-reply wait)"
        customer = None
        if trg.get("scope") == "customer" or trg.get("customer_id"):
            customer = self.get("customer", trg.get("customer_id"))
            if not customer:
                return None, "customer context missing"
            prefs, consent = customer.get("preferences") or {}, customer.get("consent") or {}
            if prefs.get("reminder_opt_in") is False or not consent.get("opted_in_at"):
                return None, "no customer consent"
        if trg.get("kind") == "renewal_due":
            days = (trg.get("payload") or {}).get("days_remaining", (merchant.get("subscription") or {}).get("days_remaining"))
            status = (merchant.get("subscription") or {}).get("status")
            if isinstance(days, (int, float)) and days > 45 and status == "active":
                return None, f"renewal {days}d away — not worth a nudge yet"
        return {"tid": tid, "trg": trg, "merchant": merchant, "category": category, "customer": customer,
                "urgency": trg.get("urgency") or 1}, None

    def tick(self, now: str, available: list[str]) -> list[dict]:
        t0 = time.time()
        with self.lock:
            ids = list(dict.fromkeys(list(available or []) + list(self.deferred.keys())))
            cands = []
            for tid in ids:
                c, why = self._candidate(tid, now)
                if c:
                    cands.append(c)
                else:
                    self.deferred.pop(tid, None)
                    self.decision_log.append({"now": now, "trigger": tid, "decision": "skip", "why": why})
            cands.sort(key=lambda c: (-int(c["urgency"]), c["tid"]))

            chosen, used_merchant, used_customer = [], set(), set()
            for c in cands:
                mid = c["merchant"]["merchant_id"]
                if c["customer"]:
                    key = c["customer"].get("customer_id")
                    if key in used_customer:
                        self.deferred[c["tid"]] = "one per customer per tick"
                        continue
                    used_customer.add(key)
                else:
                    if mid in used_merchant:
                        self.deferred[c["tid"]] = "one merchant-facing message per merchant per tick"
                        continue
                    used_merchant.add(mid)
                if len(chosen) >= MAX_ACTIONS_PER_TICK:
                    self.deferred[c["tid"]] = "tick action cap"
                    continue
                chosen.append(c)

        # compose (parallel so optional LLM calls fit the budget)
        def _do(c):
            extra = {"now": now}
            return compose_final(c["category"], c["merchant"], c["trg"], c["customer"], extra)

        results = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [(c, ex.submit(_do, c)) for c in chosen]
            for c, f in futs:
                remaining = max(0.5, TICK_BUDGET_S - (time.time() - t0))
                try:
                    results.append((c, f.result(timeout=remaining)))
                except (FutTimeout, Exception):
                    llm_off = compose_raw(c["category"], c["merchant"], c["trg"], c["customer"])
                    llm_off.pop("_facts", None)
                    results.append((c, llm_off))

        actions = []
        with self.lock:
            for c, out in results:
                tid, trg = c["tid"], c["trg"]
                mid = c["merchant"]["merchant_id"]
                self.deferred.pop(tid, None)
                if out.get("skip_reason") or not out.get("body"):
                    self.sent_triggers.add(tid)  # decided not to send; don't re-evaluate every tick
                    self.decision_log.append({"now": now, "trigger": tid, "decision": "skip", "why": out.get("skip_reason")})
                    continue
                bodies = self.bodies_by_merchant.setdefault(mid, set())
                if out["body"] in bodies:
                    self.decision_log.append({"now": now, "trigger": tid, "decision": "skip", "why": "duplicate body"})
                    continue
                cust = c["customer"]
                conv_id = self._new_conv_id(mid, cust, trg)
                action = {
                    "conversation_id": conv_id,
                    "merchant_id": mid,
                    "customer_id": cust.get("customer_id") if cust else None,
                    "send_as": out["send_as"],
                    "trigger_id": tid,
                    "template_name": out.get("template_name"),
                    "template_params": out.get("template_params") or [],
                    "body": out["body"],
                    "cta": out.get("cta") or "open_ended",
                    "suppression_key": trg.get("suppression_key") or f"{trg.get('kind')}:{mid}:{tid}",
                    "rationale": out.get("rationale", ""),
                }
                actions.append(action)
                bodies.add(out["body"])
                self.sent_triggers.add(tid)
                if trg.get("suppression_key"):
                    self.sent_suppression.add(trg["suppression_key"])
                self.conversations[conv_id] = {
                    "conversation_id": conv_id, "merchant_id": mid,
                    "customer_id": action["customer_id"], "trigger_id": tid, "kind": trg.get("kind"),
                    "send_as": action["send_as"], "status": "active", "bot_turns": 1, "commit_count": 0,
                    "turns": [{"from": "bot", "body": out["body"], "ts": now}],
                    "first_body": out["body"],
                }
                if not cust:
                    self.mstate(mid)["last_conv_id"] = conv_id
                self.decision_log.append({"now": now, "trigger": tid, "decision": "send", "conv": conv_id})
        return actions

    def _new_conv_id(self, mid, cust, trg) -> str:
        who = (cust or {}).get("customer_id") or mid
        base = f"conv_{who}_{trg.get('kind', 'msg')}"
        cid, i = base, 2
        while cid in self.conversations:
            cid = f"{base}_{i}"
            i += 1
        return cid


ENGINE = Engine()
