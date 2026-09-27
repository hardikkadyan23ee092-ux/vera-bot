"""Small formatting + language helpers. Everything here is pure and deterministic."""
from __future__ import annotations

import re
from datetime import datetime, timezone

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pct(x, signed: bool = False) -> str:
    """0.21 -> '21%', -0.5 -> '50%' (magnitude) unless signed."""
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return ""
    r = round(v, 1)
    s = f"{r:.1f}".rstrip("0").rstrip(".")
    if signed:
        return ("+" if r > 0 else "") + s + "%"
    return s.lstrip("-") + "%"


def money(x) -> str:
    """4999 -> '₹4,999'."""
    try:
        v = int(round(float(x)))
    except (TypeError, ValueError):
        return str(x)
    return "₹" + indian_commas(v)


def indian_commas(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + s


def num(n) -> str:
    try:
        return indian_commas(int(n))
    except (TypeError, ValueError):
        return str(n)


def parse_dt(s):
    if not s or not isinstance(s, str):
        return None
    try:
        s2 = s.strip().replace("Z", "+00:00")
        if len(s2) == 10:
            return datetime.fromisoformat(s2).replace(tzinfo=timezone.utc)
        d = datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def nice_date(s, with_year: bool = False) -> str:
    """'2026-05-12' -> '12 May' (or '12 May 2026')."""
    d = parse_dt(s)
    if not d:
        return str(s)
    out = f"{d.day} {MONTHS[d.month - 1]}"
    return out + f" {d.year}" if with_year else out


def humanize(token: str) -> str:
    """'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    if not token:
        return ""
    t = str(token).replace("_", " ").replace("-", " ").strip()
    t = re.sub(r"\s+", " ", t)
    return t


ABBREV = re.compile(r"\b(Dr|Mr|Mrs|Ms|St|No|vs|approx|Mfr|p)\.\s")


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    protected = ABBREV.sub(lambda m: m.group(1) + "\u00a7 ", text.strip())
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace("\u00a7", ".").strip() for p in parts if p.strip()]


def first_sentence(text: str) -> str:
    s = split_sentences(text)
    return s[0] if s else ""


def ensure_period(s: str) -> str:
    s = s.strip()
    if s and s[-1] not in ".!?":
        s += "."
    return s


def lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


# ---------------------------------------------------------------- language

HINDI_MARKERS = {
    "hai", "hain", "kya", "nahi", "nahin", "karo", "kar", "karna", "karein", "aap", "aapka", "aapki",
    "mujhe", "mera", "meri", "haan", "ji", "theek", "thik", "accha", "acha", "bhai", "bhejo", "bhej",
    "abhi", "kal", "baad", "mein", "se", "ko", "ka", "ki", "ke", "hum", "humara", "chahiye", "wala",
    "kaise", "kitna", "kab", "kyun", "dijiye", "batao", "bataiye", "chalo", "sahi", "matlab", "yeh", "woh",
}


def is_hindi(text: str) -> bool:
    if not text:
        return False
    if re.search(r"[ऀ-ॿ]", text):
        return True
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in HINDI_MARKERS)
    return hits >= 2 or (hits >= 1 and len(words) <= 4)


def customer_lang(pref: str | None) -> str:
    """Normalise customer language_pref -> 'hi' | 'hinglish' | 'en' (+ regional greeting key)."""
    p = (pref or "en").lower()
    if p in ("hi", "hindi"):
        return "hi"
    if "hi" in p and "mix" in p:
        return "hinglish"
    return "en"


REGIONAL_GREETING = {"ta": "Vanakkam", "te": "Namaskaram", "kn": "Namaskara", "mr": "Namaskar"}


def regional_greeting(pref: str | None) -> str | None:
    p = (pref or "").lower()
    for code, g in REGIONAL_GREETING.items():
        if p.startswith(code + "-") or p == code:
            return g
    return None


def numbers_in(text: str) -> list[str]:
    """Extract numeric tokens (normalised, commas removed) for grounding checks."""
    toks = re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")
    out = []
    for t in toks:
        t = t.replace(",", "")
        if t.endswith("."):
            t = t[:-1]
        out.append(t)
    return out
