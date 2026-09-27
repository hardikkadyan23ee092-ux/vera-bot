"""Post-composition checks. Used on every outgoing message (and to gate optional LLM rewrites)."""
from __future__ import annotations

import re

from util import numbers_in, parse_dt

URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


def _walk(obj, out: set):
    if isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        v = float(obj)
        for x in (v, abs(v), v * 100, abs(v * 100)):
            out.add(_norm(x))
            out.add(_norm(round(x)))
            out.add(_norm(round(x, 1)))
    elif isinstance(obj, str):
        for n in numbers_in(obj):
            out.add(_norm(float(n)))
        d = parse_dt(obj) if re.match(r"^\d{4}-\d{2}-\d{2}", obj) else None
        if d:
            out.update({_norm(d.day), _norm(d.year), _norm(d.hour % 12 or 12), _norm(d.month)})
            if d.minute:
                out.add(_norm(d.minute))


def _norm(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else f"{f:.1f}".rstrip("0").rstrip(".")


def allowed_numbers(*contexts) -> set[str]:
    out: set[str] = set()
    for c in contexts:
        if c:
            _walk(c, out)
    nums = sorted({float(x) for x in out if re.match(r"^-?\d+(\.\d+)?$", x) and float(x) < 100000})
    # simple derived arithmetic (gaps, price differences) between context numbers
    small = [n for n in nums if n <= 20000][:400]
    for i, a in enumerate(small):
        for b in small[i + 1:]:
            if 0 < b - a <= 5000:
                out.add(_norm(b - a))
    return out


def check(body: str, *, contexts=(), taboos=(), must_contain=None, extra_allowed=()) -> list[str]:
    errs = []
    if not body or not body.strip():
        return ["empty body"]
    if URL_RE.search(body):
        errs.append("contains URL")
    low = body.lower()
    for t in taboos or []:
        t_clean = re.sub(r"\(.*?\)", "", t).strip().lower()
        if t_clean and t_clean in low:
            errs.append(f"taboo word: {t_clean}")
    if contexts:
        allowed = allowed_numbers(*contexts) | {_norm(x) for x in extra_allowed}
        for n in numbers_in(body):
            if _norm(float(n)) not in allowed:
                errs.append(f"ungrounded number: {n}")
    if must_contain and must_contain.lower() not in low:
        errs.append(f"missing '{must_contain}'")
    return errs
