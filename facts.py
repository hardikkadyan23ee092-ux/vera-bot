"""Fact pack: resolves the 4 contexts into clean, verifiable facts.

Nothing in here invents data. Every value is read (or arithmetically derived) from
category / merchant / trigger / customer payloads, so anything the composer puts in a
message can be traced back to a context field.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from util import customer_lang, humanize, num, parse_dt, pct, regional_greeting, MONTHS


def _g(d: Any, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
        if cur is None:
            return default
    return cur


KNOWN_CITIES = ("delhi", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai", "pune", "kolkata",
                "chandigarh", "jaipur", "lucknow", "ahmedabad", "noida", "gurgaon", "gurugram")

# Human labels for metrics in trigger payloads
METRIC_LABEL = {
    "calls": "calls", "views": "profile views", "ctr": "click-through rate", "directions": "direction requests",
    "leads": "leads", "review_count": "reviews",
}

CATEGORY_NOUN = {
    "dentists": ("clinic", "patients", "dental practice"),
    "salons": ("salon", "clients", "salon"),
    "restaurants": ("restaurant", "customers", "restaurant"),
    "gyms": ("studio", "members", "gym"),
    "pharmacies": ("pharmacy", "customers", "pharmacy"),
}


@dataclass
class FactPack:
    category: dict
    merchant: dict
    trigger: dict
    customer: dict | None = None
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------ identity
    @property
    def slug(self) -> str:
        return self.merchant.get("category_slug") or self.category.get("slug") or ""

    @property
    def mid(self) -> str:
        return self.merchant.get("merchant_id", "")

    @property
    def biz(self) -> str:
        return _g(self.merchant, "identity", "name", default="your business")

    @property
    def locality(self) -> str:
        return _g(self.merchant, "identity", "locality", default="") or ""

    @property
    def city(self) -> str:
        return _g(self.merchant, "identity", "city", default="") or ""

    @property
    def first_name(self) -> str:
        n = _g(self.merchant, "identity", "owner_first_name", default="") or ""
        n = re.sub(r"^(dr\.?\s*)", "", n.strip(), flags=re.I)
        return n.strip()

    @property
    def salutation(self) -> str:
        n = self.first_name
        if self.slug == "dentists":
            return f"Dr. {n}" if n else "Doc"
        return n or f"{self.biz} team"

    @property
    def owner_display(self) -> str:
        """How the owner is named when a customer-facing message speaks as the merchant."""
        n = self.first_name
        if self.slug == "dentists" and n:
            return f"Dr. {n}"
        return n

    @property
    def languages(self) -> list:
        return _g(self.merchant, "identity", "languages", default=["en"]) or ["en"]

    @property
    def hinglish(self) -> bool:
        cm = (_g(self.category, "voice", "code_mix", default="") or "").lower()
        return "hi" in self.languages and ("hindi" in cm or not cm)

    @property
    def verified(self):
        return _g(self.merchant, "identity", "verified")

    @property
    def nouns(self):
        return CATEGORY_NOUN.get(self.slug, ("business", "customers", "business"))

    # ------------------------------------------------------------ performance
    @property
    def perf(self) -> dict:
        return self.merchant.get("performance") or {}

    @property
    def peer(self) -> dict:
        return self.category.get("peer_stats") or {}

    def peer_compare(self, metric: str) -> dict | None:
        """Returns {mine, peer, below, text} for calls/views/ctr/directions when both exist."""
        mine = self.perf.get(metric)
        peer_key = {"calls": "avg_calls_30d", "views": "avg_views_30d", "ctr": "avg_ctr",
                    "directions": "avg_directions_30d"}.get(metric)
        peer = self.peer.get(peer_key) if peer_key else None
        if mine is None or peer is None:
            return None
        if metric == "ctr":
            txt = f"CTR {pct(mine)} vs {pct(peer)} peer average"
        else:
            txt = f"{num(mine)} {METRIC_LABEL.get(metric, metric)} in 30 days vs {num(peer)} peer average"
        return {"mine": mine, "peer": peer, "below": float(mine) < float(peer), "text": txt}

    def weakest_vs_peer(self) -> dict | None:
        best = None
        for m in ("calls", "ctr", "views"):
            c = self.peer_compare(m)
            if not c or not c["below"]:
                continue
            gap = 1 - float(c["mine"]) / float(c["peer"]) if float(c["peer"]) else 0
            if best is None or gap > best["gap"]:
                best = dict(c, metric=m, gap=gap)
        return best

    def strongest_vs_peer(self) -> dict | None:
        best = None
        for m in ("calls", "ctr", "views"):
            c = self.peer_compare(m)
            if not c or c["below"]:
                continue
            lift = float(c["mine"]) / float(c["peer"]) - 1 if float(c["peer"]) else 0
            if best is None or lift > best["lift"]:
                best = dict(c, metric=m, lift=lift)
        return best

    def delta_7d(self, metric: str):
        return _g(self.perf, "delta_7d", f"{metric}_pct")

    def worst_delta(self):
        d = self.perf.get("delta_7d") or {}
        items = [(k.replace("_pct", ""), v) for k, v in d.items() if isinstance(v, (int, float))]
        if not items:
            return None
        k, v = min(items, key=lambda kv: kv[1])
        return k, v

    def best_delta(self):
        d = self.perf.get("delta_7d") or {}
        items = [(k.replace("_pct", ""), v) for k, v in d.items() if isinstance(v, (int, float))]
        if not items:
            return None
        k, v = max(items, key=lambda kv: kv[1])
        return k, v

    # ------------------------------------------------------------ offers
    @property
    def active_offers(self) -> list[str]:
        return [o.get("title") for o in (self.merchant.get("offers") or [])
                if o.get("status") == "active" and o.get("title")]

    @property
    def expired_offers(self) -> list[str]:
        return [o.get("title") for o in (self.merchant.get("offers") or [])
                if o.get("status") in ("expired", "paused") and o.get("title")]

    def catalog_offer(self, prefer_types=("service_at_price", "free_service", "free_trial", "free_addon"),
                      audience=None, keyword=None) -> str | None:
        cat = self.category.get("offer_catalog") or []
        if keyword:
            for o in cat:
                if keyword.lower() in (o.get("title") or "").lower():
                    return o.get("title")
        for t in prefer_types:
            for o in cat:
                if o.get("type") == t and (audience is None or o.get("audience") in (audience, "all")):
                    return o.get("title")
        return cat[0].get("title") if cat else None

    def lead_offer(self, keyword=None) -> tuple[str | None, bool]:
        """(offer_title, is_merchant_own). Prefers the merchant's live offer; else a catalog suggestion."""
        if keyword:
            for t in self.active_offers:
                if keyword.lower() in t.lower():
                    return t, True
        if self.active_offers:
            # Prefer service+price style over % discounts
            for t in self.active_offers:
                if "%" not in t:
                    return t, True
            return self.active_offers[0], True
        return self.catalog_offer(keyword=keyword), False

    # ------------------------------------------------------------ digest
    def digest_item(self, item_id: str | None) -> dict | None:
        if not item_id:
            return None
        for d in self.category.get("digest") or []:
            if d.get("id") == item_id:
                return d
        return None

    def digest_by_kind(self, *kinds) -> dict | None:
        for k in kinds:
            for d in self.category.get("digest") or []:
                if d.get("kind") == k:
                    return d
        return None

    def digest_matching(self, *words) -> dict | None:
        for d in self.category.get("digest") or []:
            blob = " ".join(str(d.get(k, "")) for k in ("title", "summary", "id")).lower()
            if all(w.lower() in blob for w in words):
                return d
        return None

    def content_item(self, *words) -> dict | None:
        for c in self.category.get("patient_content_library") or []:
            blob = (c.get("title", "") + " " + c.get("id", "")).lower()
            if any(w.lower() in blob for w in words):
                return c
        return None

    def _trend_ok(self, t: dict) -> bool:
        """Drop trends scoped to a different city (e.g. 'men's haircut delhi' for a Hyderabad salon)."""
        q = (t.get("query") or "").lower()
        mine = (self.city or "").lower()
        return not any(c in q and c != mine for c in KNOWN_CITIES)

    def top_trend(self, exclude_words=()) -> dict | None:
        ts = [t for t in (self.category.get("trend_signals") or [])
              if not any(w in (t.get("query") or "") for w in exclude_words) and self._trend_ok(t)]
        if not ts:
            return None
        return max(ts, key=lambda t: t.get("delta_yoy") or 0)

    def trend_matching(self, *words) -> dict | None:
        for t in self.category.get("trend_signals") or []:
            q = (t.get("query") or "").lower()
            if any(w.lower() in q for w in words) and self._trend_ok(t):
                return t
        return None

    def seasonal_beat_for_month(self, month_idx: int) -> dict | None:
        for b in self.category.get("seasonal_beats") or []:
            if month_in_range(month_idx, b.get("month_range", "")):
                return b
        return None

    def seasonal_beat_matching(self, *words) -> dict | None:
        for b in self.category.get("seasonal_beats") or []:
            if any(w.lower() in (b.get("note") or "").lower() for w in words):
                return b
        return None

    @property
    def taboos(self) -> list[str]:
        v = self.category.get("voice") or {}
        return list(v.get("vocab_taboo") or v.get("taboos") or [])

    # ------------------------------------------------------------ merchant state
    @property
    def signals(self) -> list[str]:
        return [str(s) for s in (self.merchant.get("signals") or [])]

    def has_signal(self, prefix: str) -> bool:
        return any(s.startswith(prefix) for s in self.signals)

    def signal_days(self, prefix: str):
        for s in self.signals:
            if s.startswith(prefix):
                m = re.search(r"(\d+)d", s)
                if m:
                    return int(m.group(1))
        return None

    @property
    def agg(self) -> dict:
        return self.merchant.get("customer_aggregate") or {}

    @property
    def sub(self) -> dict:
        return self.merchant.get("subscription") or {}

    @property
    def reviews(self) -> list[dict]:
        return self.merchant.get("review_themes") or []

    def review(self, sentiment: str) -> dict | None:
        rs = [r for r in self.reviews if r.get("sentiment") == sentiment]
        if not rs:
            return None
        return max(rs, key=lambda r: r.get("occurrences_30d") or 0)

    def review_theme(self, theme: str) -> dict | None:
        for r in self.reviews:
            if r.get("theme") == theme:
                return r
        return None

    @property
    def history(self) -> list[dict]:
        return self.merchant.get("conversation_history") or []

    def last_merchant_msg(self) -> dict | None:
        for h in reversed(self.history):
            if h.get("from") == "merchant":
                return h
        return None

    def last_vera_msg(self) -> dict | None:
        for h in reversed(self.history):
            if h.get("from") == "vera":
                return h
        return None

    # ------------------------------------------------------------ customer
    @property
    def cust_name(self) -> str | None:
        if not self.customer:
            return None
        n = _g(self.customer, "identity", "name", default="") or ""
        if not n or n.startswith("("):
            return None
        return re.sub(r"\s*\(.*?\)\s*", "", n).strip() or None

    @property
    def cust_parent(self) -> str | None:
        n = _g(self.customer, "identity", "name", default="") or ""
        m = re.search(r"parent:\s*([A-Za-z]+)", n)
        return m.group(1) if m else None

    @property
    def cust_lang(self) -> str:
        return customer_lang(_g(self.customer, "identity", "language_pref"))

    @property
    def cust_greeting(self) -> str | None:
        return regional_greeting(_g(self.customer, "identity", "language_pref"))

    @property
    def cust_pref_slot(self) -> str | None:
        s = _g(self.customer, "preferences", "preferred_slots")
        return humanize(s) if s else None

    @property
    def cust_last_visit(self) -> str | None:
        return _g(self.customer, "relationship", "last_visit")

    @property
    def cust_services(self) -> list[str]:
        return [s for s in (_g(self.customer, "relationship", "services_received", default=[]) or []) if s != "..."]

    def cust_consented(self) -> tuple[bool, str]:
        if not self.customer:
            return False, "no customer context"
        prefs = self.customer.get("preferences") or {}
        consent = self.customer.get("consent") or {}
        if prefs.get("reminder_opt_in") is False:
            return False, "customer has reminder_opt_in=false"
        if not consent.get("opted_in_at") or not consent.get("scope"):
            return False, "no recorded consent"
        if not self.cust_name:
            return False, "no customer name/profile to address"
        return True, "consent on file (" + ", ".join(consent.get("scope") or []) + ")"


def month_in_range(m: int, rng: str) -> bool:
    """m is 1-12. rng like 'Apr-Jun', 'Nov-Feb', 'Jan', 'Feb 14'."""
    if not rng:
        return False
    parts = re.findall(r"[A-Za-z]{3}", rng)
    idx = [MONTHS.index(p.title()) + 1 for p in parts if p.title() in MONTHS]
    if not idx:
        return False
    if len(idx) == 1:
        return m == idx[0]
    a, b = idx[0], idx[-1]
    if a <= b:
        return a <= m <= b
    return m >= a or m <= b


def build(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
          extra: dict | None = None) -> FactPack:
    return FactPack(category or {}, merchant or {}, trigger or {}, customer, extra or {})


def trigger_is_expired(trigger: dict, now_iso: str | None) -> bool:
    """Expired relative to the harness clock.

    If the tick clock is far (>45 days) past the trigger's expiry, the clock is almost
    certainly a wall clock unrelated to the simulated dataset time (e.g. the local judge
    simulator uses datetime.utcnow()). In that case we trust the judge's
    `available_triggers` list as the source of truth for what's active.
    """
    exp = parse_dt(trigger.get("expires_at"))
    now = parse_dt(now_iso)
    if not exp or not now:
        return False
    if now <= exp:
        return False
    return (now - exp).days <= 45
