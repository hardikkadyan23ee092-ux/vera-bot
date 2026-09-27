"""Deterministic, grounded composer — one playbook per trigger kind.

Design: code decides *what* to say (facts, angle, recommendation, CTA) from the four
contexts; wording is fixed per playbook so every number is traceable to a context field.
An optional LLM pass (see llm.py) may re-phrase, but only survives if it passes the same
validator; otherwise this output is used as-is.
"""
from __future__ import annotations

import re

import facts as factmod
from facts import FactPack, METRIC_LABEL
from util import (ensure_period, first_sentence, humanize, lower_first, money, nice_date, num,
                   parse_dt, pct)

COMPOSER_VERSION = "composer_v3"

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ============================================================ helpers

def _when(iso: str) -> str:
    """'2026-05-02T19:00:00+05:30' -> 'Sat 2 May, 7pm'."""
    d = parse_dt(iso)
    if not d:
        return str(iso)
    h = d.hour
    ampm = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    t = f"{h12}{ampm}" if d.minute == 0 else f"{h12}:{d.minute:02d}{ampm}"
    return f"{WEEKDAYS[d.weekday()]} {d.day} {nice_date(iso)[len(str(d.day)) + 1:]}, {t}"


def _dates_nice(text: str) -> str:
    return re.sub(r"\b(\d{4}-\d{2}-\d{2})\b", lambda m: nice_date(m.group(1), with_year=True), text or "")


def _cta(F: FactPack, en: str, hi: str | None = None, yes: bool = True) -> str:
    use_hi = bool(hi) and F.hinglish and F.slug != "dentists"
    q = hi if use_hi else en
    return q + (" Reply YES." if yes else "")


def _join(*parts) -> str:
    out = " ".join(p.strip() for p in parts if p and p.strip())
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def _offer_core(title: str) -> str:
    """'Dental Cleaning @ ₹299' -> 'Dental Cleaning'."""
    return (title or "").split("@")[0].split("(")[0].strip()


def _pref(txt: str | None) -> str:
    if not txt:
        return ""
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "weekday"):
        txt = re.sub(rf"\b{d}\b", d.title(), txt)
    return txt


def _possessive(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _program_name(token: str) -> str:
    """'skin_prep_program_30day' -> '30-day skin-prep program'."""
    t = token or ""
    m = re.search(r"(\d+)\s*_?day", t)
    rest = humanize(re.sub(r"_?\d+\s*_?day", "", t)).replace("skin prep", "skin-prep")
    return (f"{m.group(1)}-day " if m else "") + rest


def _offer_for_beat(F: FactPack, note: str):
    words = [w for w in re.findall(r"[a-zA-Z]{5,}", note or "") if w.lower() not in ("season", "window", "baseline", "segment", "dominant", "primary", "bookings")]
    for w in words:
        for o in F.active_offers:
            if w.lower() in o.lower():
                return o, True
    for w in words:
        c = F.catalog_offer(keyword=w)
        if c and w.lower() in c.lower():
            return c, False
    # highest-priced active offer suits a package better than the cheapest hook
    priced = [(int(m.group(1).replace(",", "")), o) for o in F.active_offers for m in [re.search(r"₹\s?([\d,]+)", o)] if m]
    if priced:
        return max(priced)[1], True
    return F.lead_offer()


SERVICE_WORD = {
    "dentists": ("check-up", "consultation"), "salons": ("next appointment", "trial session"),
    "gyms": ("next session", "trial class"), "pharmacies": ("next refill", "first visit"),
    "restaurants": ("next visit", "first visit"),
}


def _signal_causes(F: FactPack) -> list[str]:
    causes = []
    if F.verified is False or F.has_signal("unverified_gbp"):
        causes.append("your Google profile is still unverified")
    d = F.signal_days("stale_posts")
    if d:
        causes.append(f"your last Google post was {d} days ago")
    elif F.has_signal("no_recent_post"):
        causes.append("there's no recent Google post")
    if not F.active_offers:
        causes.append("there's no live offer on the listing")
    return causes


def _result(body, cta, rationale, params, send_as="vera", template=None, skip=None, facts_used=None):
    return {
        "body": body.strip(),
        "cta": cta,
        "rationale": rationale.strip(),
        "template_params": [p for p in params if p],
        "send_as": send_as,
        "template_name": template,
        "skip_reason": skip,
        "composer_version": COMPOSER_VERSION,
    }


# ============================================================ merchant-facing playbooks

def research_digest(F: FactPack):
    p = F.trigger.get("payload") or {}
    item = (F.digest_item(p.get("top_item_id") or p.get("digest_item_id"))
            or (p.get("top_item") if isinstance(p.get("top_item"), dict) else None)
            or F.digest_by_kind("research", "trend", "tech"))
    if not item:
        return generic_merchant(F)
    src = _dates_nice(item.get("source", ""))
    title = _dates_nice(item.get("title", ""))
    trial = f" ({num(item['trial_n'])}-patient trial)" if item.get("trial_n") else ""
    summ = first_sentence(item.get("summary", ""))
    seg = item.get("patient_segment") or ""
    tie = ""
    hr = F.agg.get("high_risk_adult_count")
    if "high_risk" in seg and hr:
        tie = f"That maps straight onto your {num(hr)} high-risk adult patients."
    elif "chronic" in (item.get("title", "") + item.get("summary", "")).lower() and F.agg.get("chronic_rx_count"):
        tie = f"You have {num(F.agg['chronic_rx_count'])} chronic-Rx customers this applies to."
    act = ensure_period(item.get("actionable", "")) if item.get("actionable") else ""
    if F.slug == "dentists":
        cta = _cta(F, "Want me to pull the abstract and draft a patient-ed WhatsApp you can send?")
    elif item.get("kind") == "research":
        cta = _cta(F, "Want me to turn this into a short post for your customers?",
                   "Iska ek short customer post bana doon?")
    else:
        cta = _cta(F, "Want me to set this up for you this week?", "Yeh is hafte aapke liye set up kar doon?")
    body = _join(f"{F.salutation}, new in {src}: \"{title}\"{trial}.", ensure_period(summ), tie, act, cta)
    return _result(body, "binary_yes_no",
                   f"External research digest ({item.get('id')}); anchored on source + trial numbers and tied to "
                   f"the merchant's own cohort ({'high_risk_adult_count' if tie else 'segment'}). Lever: curiosity + "
                   f"reciprocity (Vera does the pull + draft). Single YES CTA.",
                   [F.salutation, f"{src}: {title}{trial}", tie or summ, cta])


def regulation_change(F: FactPack):
    p = F.trigger.get("payload") or {}
    item = F.digest_item(p.get("top_item_id") or p.get("digest_item_id")) or F.digest_by_kind("compliance")
    if not item:
        return generic_merchant(F)
    title = _dates_nice(item.get("title", ""))
    src = _dates_nice(item.get("source", ""))
    summ = item.get("summary", "")
    from util import split_sentences
    sents = split_sentences(summ)
    summ2 = " ".join(sents[:3])
    deadline = p.get("deadline_iso")
    dl = ""
    if deadline and nice_date(deadline, True) not in title:
        dl = f"Deadline: {nice_date(deadline, True)}."
    act = ensure_period(item.get("actionable", ""))
    cta = _cta(F, "Want me to send a one-page audit checklist your team can file with the SOPs?",
               "Team ke liye ek one-page audit checklist bhej doon?")
    body = _join(f"{F.salutation}, compliance heads-up — {title} ({src}).", summ2, dl, act, cta)
    return _result(body, "binary_yes_no",
                   f"Regulation change ({item.get('id')}), urgency {F.trigger.get('urgency')}. States the exact limit "
                   f"change + deadline and the concrete action. Lever: loss aversion (non-compliance) + effort "
                   f"externalization (checklist).",
                   [F.salutation, title, dl or act, cta])


def cde_opportunity(F: FactPack):
    p = F.trigger.get("payload") or {}
    item = F.digest_item(p.get("digest_item_id") or p.get("top_item_id")) or F.digest_by_kind("cde")
    if not item:
        return generic_merchant(F)
    when = _when(item["date"]) if item.get("date") and "T" in str(item.get("date")) else nice_date(item.get("date"))
    credits = p.get("credits") or item.get("credits")
    cr = f", {credits} CDE credits" if credits else ""
    summ = ensure_period(item.get("summary", ""))
    fee = ensure_period(item.get("actionable", "")) if item.get("actionable") else ""
    cta = _cta(F, "Want me to send the registration details and a reminder on the day?",
               "Registration details + us din ka reminder bhej doon?")
    body = _join(f"{F.salutation}, {item.get('source')}: \"{item.get('title')}\" — {when}{cr}.", summ, fee, cta)
    return _result(body, "binary_yes_no",
                   f"CDE opportunity ({item.get('id')}); low-urgency, so framed as a useful heads-up with date, "
                   f"credits and fee from the digest. Lever: reciprocity + low-friction YES.",
                   [F.salutation, item.get("title"), when, cta])


def competitor_opened(F: FactPack):
    p = F.trigger.get("payload") or {}
    name = p.get("competitor_name")
    if not name or p.get("placeholder"):
        # No competitor details in context: acknowledge the event without inventing specifics.
        strong = F.strongest_vs_peer()
        pos = F.review("pos")
        offer, own = F.lead_offer()
        edge = []
        if strong:
            edge.append(strong["text"])
        if pos and pos.get("occurrences_30d"):
            edge.append(f"{pos['occurrences_30d']} reviews this month praising {humanize(pos['theme'])}")
        edge_txt = ("You go in with an edge: " + "; ".join(edge) + ".") if edge else \
            f"Right now you're at {num(F.perf.get('views', 0))} views and {num(F.perf.get('calls', 0))} calls in 30 days."
        action = (f"pin '{offer}' and your best review quotes to the top of your profile" if own else
                  f"put a '{offer}' offer live and pin your best review quotes")
        action_hi = (f"'{offer}' aur best review quotes profile ke top pe pin" if own else
                     f"'{offer}' offer live karke best review quotes pin")
        cta = _cta(F, f"Want me to {action} this week?", f"Is hafte {action_hi} kar doon?")
        body = _join(f"{F.salutation}, a new {F.nouns[2]} listing has come up near {F.locality or F.city}.",
                     edge_txt, "Best defence is making that advantage visible before they build reviews.", cta)
        return _result(body, "binary_yes_no",
                       "Competitor-opened event without competitor details in context — deliberately not naming or "
                       "pricing a rival. Anchors on the merchant's own peer-beating numbers/reviews and recommends "
                       "defending on visibility, not price.",
                       [F.salutation, edge_txt, cta])
    dist = p.get("distance_km")
    their = p.get("their_offer")
    opened = p.get("opened_date")
    line1 = f"{F.salutation}, {name} opened {dist} km away" + (f" on {nice_date(opened)}" if opened else "") + \
            (f" with {their}" if their else "") + "."
    # Compare to merchant's own price for the same service
    cmp_line = ""
    if their:
        core = _offer_core(their).lower()
        mine = next((o for o in F.active_offers if _offer_core(o).lower() == core), None)
        m1 = re.search(r"₹\s?([\d,]+)", their)
        m2 = re.search(r"₹\s?([\d,]+)", mine or "")
        if mine and m1 and m2:
            diff = int(m2.group(1).replace(",", "")) - int(m1.group(1).replace(",", ""))
            if diff > 0:
                cmp_line = f"That's ₹{num(diff)} under your {mine}."
    pos = F.review("pos")
    rec = "I wouldn't price-match."
    if pos and pos.get("common_quote"):
        rec += (f" Your edge is trust — {pos.get('occurrences_30d')} reviews this month say "
                f"\"{pos['common_quote']}\". Let's make that the first thing people see.")
    else:
        strong = F.strongest_vs_peer()
        if strong:
            rec += f" Your edge is already visible in the numbers ({strong['text']}); let's make it the headline."
    cta = _cta(F, "Want me to add those review quotes to your profile and draft a post on what your cleaning includes?"
               if F.slug == "dentists" else "Want me to pin your review highlights and refresh your offer post this week?",
               "Review highlights pin karke offer post refresh kar doon?")
    body = _join(line1, cmp_line, rec, cta)
    return _result(body, "binary_yes_no",
                   f"Competitor {name} ({dist} km, {their}). Decision: do NOT price-match — defend on trust using the "
                   f"merchant's own positive review theme. Lever: loss aversion + curiosity; single YES.",
                   [F.salutation, line1, cta])


def perf_dip(F: FactPack):
    p = F.trigger.get("payload") or {}
    metric = p.get("metric")
    delta = p.get("delta_pct")
    if metric is None or delta is None:
        wd = F.worst_delta()
        if wd:
            metric, delta = wd
    label = METRIC_LABEL.get(metric or "", humanize(metric or "calls"))
    causes = _signal_causes(F)
    weak = F.weakest_vs_peer()
    if delta is not None and float(delta) < 0:
        base = p.get("vs_baseline")
        line1 = f"{F.salutation}, your {label} are down {pct(delta)} this week" + \
                (f" against your usual {num(base)}" if base else "") + "."
    else:
        line1 = f"{F.salutation}, flagging your {label}: " + (weak["text"] + "." if weak else "they've gone flat this week.")
        weak = None
    why = ""
    if causes:
        why = ("Likely drivers: " if len(causes[:2]) > 1 else "Likely driver: ") + "; ".join(causes[:2]) + "."
    if weak and len(causes) < 2:
        why = _join(why, f"You're also below peers — {weak['text']}.")
    fixes, fixes_hi = [], []
    if F.verified is False or F.has_signal("unverified_gbp"):
        fixes.append("start Google verification")
        fixes_hi.append("Google verification shuru")
    offer, own = F.lead_offer()
    if offer and not own:
        fixes.append(f"put '{offer}' live on your listing")
        fixes_hi.append(f"'{offer}' offer listing pe live")
    elif offer:
        fixes.append(f"push your '{offer}' in a fresh post")
        fixes_hi.append(f"'{offer}' ka fresh post live")
    if not fixes:
        fixes.append("publish one fresh post this week")
        fixes_hi.append("ek fresh post live")
    fix_txt = " and ".join(fixes[:2])
    cta = _cta(F, f"Want me to {fix_txt} today?", "Aaj hi " + " aur ".join(fixes_hi[:2]) + " kar doon?")
    body = _join(line1, why, cta)
    return _result(body, "binary_yes_no",
                   f"Perf dip on {metric} ({pct(delta, True) if delta is not None else 'n/a'}). Diagnosed from the "
                   f"merchant's own signals ({', '.join(causes[:2]) or 'peer gap'}) and proposed the fix Vera can do "
                   f"now. Lever: loss aversion + effort externalization.",
                   [F.salutation, line1, why, cta])


def perf_spike(F: FactPack):
    p = F.trigger.get("payload") or {}
    metric, delta = p.get("metric"), p.get("delta_pct")
    if metric is None or delta is None:
        bd = F.best_delta()
        if bd:
            metric, delta = bd
    label = METRIC_LABEL.get(metric or "", humanize(metric or "calls"))
    driver = humanize(p.get("likely_driver") or "")
    offer, own = F.lead_offer()
    strong = F.strongest_vs_peer()
    if delta is not None and float(delta) > 0:
        line1 = f"{F.salutation}, your {label} are up {pct(delta)} this week" + \
                (f" (usual: {num(p['vs_baseline'])})" if p.get("vs_baseline") else "") + \
                (f" — looks like the {driver} is pulling" if driver else "") + "."
    else:
        line1 = f"{F.salutation}, quick read on {F.biz}: no spike in the last 7 days, but the base is solid."
    proof = f"You're already ahead of peers: {strong['text']}." if strong else ""
    unver = "One multiplier still open: your Google profile is unverified." if (F.verified is False) else ""
    spiking = delta is not None and float(delta) > 0
    move = (("Best move while it's warm: a follow-up post on the same theme" if (spiking and driver) else
             "Best move while it's warm: a fresh post" if spiking else "Worth putting that lead to work: a fresh post")
            + (f" with '{offer}' pinned" if offer else "") + ".")
    cta = _cta(F, "Want me to draft that post now?", "Post abhi draft kar doon?")
    body = _join(line1, proof, unver, move, cta)
    return _result(body, "binary_yes_no",
                   f"Perf spike on {metric} ({pct(delta, True) if delta is not None else 'n/a'}); rides momentum with a "
                   f"follow-up on the likely driver ({driver or 'recent activity'}). Lever: social proof (vs peers) + "
                   f"effort externalization.",
                   [F.salutation, line1, move, cta])


def milestone_reached(F: FactPack):
    p = F.trigger.get("payload") or {}
    metric = p.get("metric")
    now_v, target = p.get("value_now"), p.get("milestone_value")
    peer_reviews = F.peer.get("avg_review_count")
    pos = F.review("pos")
    if now_v is not None and target is not None:
        gap = int(target) - int(now_v)
        label = METRIC_LABEL.get(metric or "", humanize(metric or ""))
        if gap > 0:
            line1 = f"{F.salutation}, you're at {num(now_v)} {label} — just {gap} away from {num(target)}."
        else:
            line1 = f"{F.salutation}, you just crossed {num(target)} {label} 🎉"
        peer_line = ""
        if metric == "review_count" and peer_reviews:
            peer_line = (f"That's already above the {num(peer_reviews)}-review peer average for your area."
                         if int(now_v) >= int(peer_reviews) else
                         f"Peer average is {num(peer_reviews)}, so this closes the gap.")
        proof = (f"And {pos['occurrences_30d']} reviews this month mention {humanize(pos['theme'])} — "
                 f"your happiest customers are right there.") if pos else ""
        cta = _cta(F, "Want me to draft a short review-request line to send to today's customers?",
                   "Aaj ke customers ke liye short review-request line draft kar doon?")
        body = _join(line1, peer_line, proof, cta)
    else:
        strong = F.strongest_vs_peer()
        if strong and strong["metric"] == "views":
            strong = F.peer_compare("ctr") if F.peer_compare("ctr") and not F.peer_compare("ctr")["below"] else None
        line1 = f"{F.salutation}, {F.biz} has logged {num(F.perf.get('views', 0))} profile views and " \
                f"{num(F.perf.get('calls', 0))} calls in the last 30 days."
        if strong:
            peer_line = f"Your {strong['text']} — you're converting better than most."
        else:
            c = F.peer_compare("calls")
            peer_line = f"Next milestone to chase: calls ({c['text']})." if c and c["below"] else ""
        cta = _cta(F, "Want me to turn this into a milestone post and a review-request line for regulars?",
                   "Isse milestone post + regulars ke liye review-request line bana doon?")
        body = _join(line1, peer_line, cta)
    return _result(body, "binary_yes_no",
                   "Milestone moment; uses exact counts and the peer benchmark, converts momentum into a review "
                   "ask. Lever: progress/social proof + low-effort action.",
                   [F.salutation, line1, cta])


def dormant_with_vera(F: FactPack):
    p = F.trigger.get("payload") or {}
    days = p.get("days_since_last_merchant_message")
    open_ = f"{F.salutation}, it's been {days} days since we last spoke." if days else \
        f"{F.salutation}, quick pulse-check on {F.biz}."
    moves = []
    for m in ("calls", "views"):
        d = F.delta_7d(m)
        if isinstance(d, (int, float)) and d != 0:
            moves.append(f"{METRIC_LABEL[m]} {'down' if d < 0 else 'up'} {pct(d)}")
    state = ("This week: " + ", ".join(moves) + ".") if moves else ""
    hook = ""
    item = F.digest_by_kind("seasonal", "trend")
    if item:
        hook = f"Also worth knowing ({item.get('source')}): {first_sentence(item.get('summary', '')) or item.get('title')}"
        hook = ensure_period(hook)
    cta = _cta(F, "Want a 2-line summary of what changed and the one thing to fix first?",
               "2-line summary bhej doon — kya badla aur pehle kya fix karein?")
    body = _join(open_, state, hook, cta)
    return _result(body, "binary_yes_no",
                   "Dormant merchant: re-open with their own numbers + one fresh category insight, not a sales ask. "
                   "Lever: reciprocity + curiosity; tiny commitment.",
                   [F.salutation, state or open_, cta])


def winback_eligible(F: FactPack):
    p = F.trigger.get("payload") or {}
    days = p.get("days_since_expiry") or F.sub.get("days_since_expiry")
    dip = p.get("perf_dip_pct")
    lapsed = p.get("lapsed_customers_added_since_expiry")
    bits = []
    if dip is not None:
        bits.append(f"calls are down {pct(dip)}")
    if lapsed:
        bits.append(f"{lapsed} more {F.nouns[1]} have lapsed")
    line1 = f"{F.salutation}, since your plan paused {days} days ago, " + " and ".join(bits) + "." if bits else \
        f"{F.salutation}, your plan has been paused for {days} days."
    item = F.digest_by_kind("seasonal")
    hook = ensure_period(f"And the timing matters: {first_sentence(item.get('summary', '')) or item['title']}") if item else ""
    cta = _cta(F, "Want me to show you exactly what slipped and what reactivating would restore first?",
               "Dikhaun kya slip hua aur reactivate karne se pehle kya wapas aayega?")
    body = _join(line1, hook, cta)
    return _result(body, "binary_yes_no",
                   "Win-back: quantifies the loss since expiry from the trigger payload (loss aversion) and adds a "
                   "timely seasonal reason; asks for a low-commitment look, not a payment.",
                   [F.salutation, line1, cta])


def renewal_due(F: FactPack):
    p = F.trigger.get("payload") or {}
    days = p.get("days_remaining", F.sub.get("days_remaining"))
    plan = p.get("plan") or F.sub.get("plan") or "current"
    amt = p.get("renewal_amount")
    status = F.sub.get("status")
    if status == "expired" and not p.get("days_remaining"):
        line1 = f"{F.salutation}, your {plan} plan expired {F.sub.get('days_since_expiry')} days ago."
    elif (F.sub.get("status") == "trial" or str(plan).lower() == "trial"):
        line1 = f"{F.salutation}, your trial ends in {days} days" + (f" (Pro renewal {money(amt)})" if amt else "") + "."
    else:
        line1 = f"{F.salutation}, your {plan} plan renews in {days} days" + (f" ({money(amt)})" if amt else "") + "."
    honest = []
    wd = F.worst_delta()
    if wd and wd[1] < 0:
        honest.append(f"{METRIC_LABEL.get(wd[0], wd[0])} are down {pct(wd[1])} this week")
    if F.verified is False:
        honest.append("your profile is still unverified")
    if honest:
        mid = "Before you decide, straight talk: " + " and ".join(honest) + \
              (" — both fixable" if len(honest) > 1 else " — fixable") + " while the plan is active."
        cta = _cta(F, "Want me to fix those first so you see the impact before renewing?",
                   "Renew se pehle yeh fix kar doon taaki impact dikh jaaye?")
    else:
        strong = F.strongest_vs_peer()
        mid = f"What the plan is delivering: {strong['text']}." if strong else ""
        cta = _cta(F, "Want me to lock in the renewal so nothing pauses?", "Renewal lock kar doon taaki kuch pause na ho?")
    body = _join(line1, mid, cta)
    return _result(body, "binary_yes_no",
                   "Renewal due: states days + amount, then gives an honest performance read and offers to fix issues "
                   "before asking for money (trust > pressure). Lever: loss aversion.",
                   [F.salutation, line1, cta])


def festival_upcoming(F: FactPack):
    p = F.trigger.get("payload") or {}
    fest = p.get("festival")
    date = p.get("date")
    days = p.get("days_until")
    if fest and date:
        line1 = f"{F.salutation}, {fest} falls on {nice_date(date, True)}" + (f" — {days} days out" if days else "") + "."
        d = parse_dt(date)
        beat = F.seasonal_beat_for_month(d.month) if d else None
    else:
        beat = F.seasonal_beat_matching("festival", "diwali", "wedding", "christmas")
        line1 = f"{F.salutation}, festive season planning time for {F.biz}."
    beat_line = f"The {beat['month_range']} pattern for {F.slug}: {beat['note']}." if beat else ""
    offer, own = _offer_for_beat(F, (beat or {}).get("note", ""))
    if days and int(days) > 60:
        plan = (f"No need to discount yet — the smart move is building a festive package around "
                f"{'your ' if own else ''}'{offer}' now so it's live before bookings peak.")
    elif days:
        plan = f"Suggest a festive push around {'your ' if own else ''}'{offer}' starting this week."
    else:
        plan = (f"Plan it now so it's ready when the window opens: a festive package around "
                f"{'your ' if own else ''}'{offer}'.")
    cta = _cta(F, "Want me to draft the package + the first post?", "Package aur pehla post draft kar doon?")
    body = _join(line1, beat_line, plan, cta)
    return _result(body, "binary_yes_no",
                   f"Festival trigger ({fest or 'seasonal window'}). Decision: {'too early to discount; plan the package' if days and int(days) > 60 else 'push now'}, "
                   f"grounded in the category seasonal beat. Lever: effort externalization + timing.",
                   [F.salutation, line1, plan, cta])


def curious_ask_due(F: FactPack):
    cands = [_offer_core(o) for o in F.active_offers][:2]
    pos = F.review("pos")
    trend = None
    for o in F.active_offers:
        for w in re.findall(r"[a-zA-Z]{4,}", _offer_core(o)):
            trend = trend or F.trend_matching(w)
    trend = trend or F.top_trend()
    fact = ""
    if trend:
        fact = f"'{trend['query']}' searches are up {pct(trend['delta_yoy'])} YoY right now."
    rec = "Reply in one line and I'll turn it into a Google post plus a ready reply for price enquiries."
    if cands:
        opts = " or ".join(cands)
        q = f"What's been most asked-for at {F.biz} this week — {opts}, or something else?"
    elif pos:
        q = f"What's been most asked-for at {F.biz} this week — is it {humanize(pos['theme'])} again?"
    else:
        q = f"What's been most asked-for at {F.biz} this week?"
    if F.hinglish and F.slug != "dentists":
        rec = "Ek line mein bata dijiye — main usse Google post + price-enquiry ka ready reply bana dungi."
    body = _join(f"Hi {F.salutation}!" if F.slug != "dentists" else f"{F.salutation},", fact, rec, q)
    return _result(body, "open_ended",
                   "Curious-ask cadence: low-stakes question (asking-the-merchant lever) with reciprocity offered up "
                   "front and a live category trend as the hook. Question lands last.",
                   [F.salutation, fact, q])


def active_planning_intent(F: FactPack):
    p = F.trigger.get("payload") or {}
    topic = p.get("intent_topic", "")
    last = p.get("merchant_last_message") or (F.last_merchant_msg() or {}).get("body", "")
    t = topic.lower()
    lines = []
    if "thali" in t or "corporate" in t:
        thali = next((o for o in F.active_offers if "thali" in o.lower()), None)
        vol = None
        lv = F.last_vera_msg()
        if lv:
            m = re.search(r"(\d+)\s*orders/day", lv.get("body", ""))
            vol = m.group(1) if m else None
        head = f"{F.salutation}, here's a first cut of the corporate thali package — edit anything:"
        lines = [
            f"• Base: your {thali or 'weekday thali'} for office groups{', minimum 10 per order' }",
            "• Order by 5pm the day before; delivered for the lunch hour",
            f"• Offices in {F.locality or F.city} within your delivery radius only",
            "• Monthly billing option for repeat offices",
        ]
        why = f"Your thali already runs at {vol} orders/day, so the kitchen can absorb bulk." if vol else ""
        cta = _cta(F, "Want me to draft the WhatsApp pitch for office admins next?",
                   "Next step: office admins ke liye WhatsApp pitch draft kar doon?")
    elif "kids" in t or "yoga" in t:
        prior = (F.last_vera_msg() or {}).get("body", "")
        m = re.search(r"(\d+)-week program, (\d+) classes/week, age ([\d-]+), (₹[\d,]+)", prior)
        head = f"{F.salutation}, here's the kids yoga summer camp, ready to publish:"
        if m:
            lines = [f"• {m.group(1)} weeks, {m.group(2)} classes a week, ages {m.group(3)}",
                     f"• {m.group(4)} for the full camp"]
        else:
            lines = ["• Weekend + weekday batches, age-grouped"]
        small = F.review_theme("small_classes")
        lines.append("• Small batches — the thing your reviews already praise" if small else "• Small batches, one instructor per group")
        lines.append("• Free trial class for first-timers, booked on WhatsApp")
        why = ""
        cta = _cta(F, "Want me to publish it as your Google post + an Insta carousel today?",
                   "Aaj hi Google post + Insta carousel publish kar doon?")
    else:
        offer, own = F.lead_offer()
        head = f"{F.salutation}, picking up where you left off (\"{last}\") — here's a starter plan for {humanize(topic)}:"
        lines = [f"• Anchor it on {'your ' if own else ''}'{offer}'" if offer else "• One clear service + price",
                 f"• Launch post on Google + WhatsApp broadcast to {F.nouns[1]}",
                 "• Review after 2 weeks and adjust"]
        why = ""
        cta = _cta(F, "Want me to draft the launch post now?", "Launch post abhi draft kar doon?")
    body = head + "\n" + "\n".join(lines) + ("\n" + why if why else "") + "\n" + cta
    return _result(body, "binary_yes_no",
                   f"Merchant already expressed intent ('{last}'). Decision: deliver the draft artifact now instead of "
                   f"asking more questions (intent-handoff). Only prices/figures already in context are used.",
                   [F.salutation, head, cta])


def seasonal_perf_dip(F: FactPack):
    p = F.trigger.get("payload") or {}
    metric, delta = p.get("metric", "views"), p.get("delta_pct")
    label = METRIC_LABEL.get(metric, metric)
    item = F.digest_by_kind("seasonal")
    beat = F.seasonal_beat_matching("lowest", "retention", "acquisition") or None
    line1 = f"{F.salutation}, your {label} are down {pct(delta)} this week — and that's expected, not a problem."
    ctx = ""
    if beat:
        ctx = f"{beat['month_range']} is the {beat['note']}."
    if item and item.get("actionable"):
        ctx += f" The data call ({item.get('source')}): {lower_first(ensure_period(item['actionable']))}"
    ret = ""
    members = F.agg.get("total_active_members")
    churn = F.agg.get("monthly_churn_pct")
    peer_churn = F.peer.get("monthly_churn_pct")
    if members:
        ret = f"Best use of the lull is retention: your {num(members)} members"
        if churn is not None:
            ret += f" are churning {pct(churn)}/month" + (f" vs {pct(peer_churn)} peer average" if peer_churn else "")
        ret += "."
    cta = _cta(F, "Want me to draft a member attendance challenge to hold churn down through the dip?",
               "Dip ke dauraan churn rokne ke liye member attendance challenge draft kar doon?")
    body = _join(line1, ctx, ret, cta)
    return _result(body, "binary_yes_no",
                   "Seasonal dip flagged as expected: pre-empts panic, recommends NOT spending on acquisition now "
                   "(category digest) and redirects to retention using the merchant's churn vs peer. Judgment over "
                   "templating.",
                   [F.salutation, line1, ret or ctx, cta])


def ipl_match_today(F: FactPack):
    p = F.trigger.get("payload") or {}
    match, venue = p.get("match"), p.get("venue")
    t = _when(p["match_time_iso"]).split(", ")[-1] if p.get("match_time_iso") else ""
    weeknight = p.get("is_weeknight")
    item = F.digest_matching("ipl") or {}
    line1 = f"{F.salutation}, {match} at {venue} tonight" + (f", {t}" if t else "") + "."
    bogo = next((o for o in F.active_offers if "tue" in o.lower() or "bogo" in o.lower() or "buy 1" in o.lower()), None)
    combo = F.catalog_offer(keyword="match-night")
    late = F.review_theme("delivery_late")
    if weeknight is False:
        data = ("Heads-up from magicpin order data: weekend match nights pull people home — Saturday IPL nights ran "
                "12% fewer covers, while weeknight matches ran 18% more.") if item else \
            "Weekend match nights usually pull people home to watch."
        plan = "So skip a dine-in match promo tonight and go delivery-first" + \
               (f": a delivery-only '{combo}' from 7pm" if combo else "") + "."
        if bogo:
            plan += f" Keep your '{bogo}' for the weeknight matches, where dine-in actually rises."
        if late:
            plan += f" Watch dispatch times — {late.get('occurrences_30d')} recent reviews flag late delivery."
        cta = _cta(F, "Want me to put the combo live on your listing before the match?",
                   "Match se pehle combo listing pe live kar doon?")
    else:
        data = ("Weeknight matches have been driving 18% more covers (magicpin order data).") if item else ""
        plan = f"Good night for a dine-in push: {'your ' + repr(bogo) if bogo else repr(combo)} on the listing and a match-time post."
        cta = _cta(F, "Want me to post it now?", "Abhi post kar doon?")
    body = _join(line1, data, plan, cta)
    return _result(body, "binary_yes_no",
                   f"IPL trigger cross-checked with category digest ({item.get('id', 'n/a')}): "
                   f"{'weekend match → covers fall, so recommend delivery-only combo and save the Tue-Thu BOGO' if weeknight is False else 'weeknight → dine-in push'}. "
                   f"Contrarian, data-backed call. Lever: loss aversion + 1-step action.",
                   [F.salutation, line1, plan, cta])


def review_theme_emerged(F: FactPack):
    p = F.trigger.get("payload") or {}
    theme = p.get("theme")
    rt = F.review_theme(theme) if theme else F.review("neg")
    if not theme and not rt:
        return _result("", "none", "No review data in context to anchor a review-theme message; restraint.",
                       [], skip="no review data in merchant context")
    theme = theme or rt.get("theme")
    occ = p.get("occurrences_30d") or (rt or {}).get("occurrences_30d")
    quote = p.get("common_quote") or (rt or {}).get("common_quote")
    trend = p.get("trend")
    line1 = f"{F.salutation}, {occ} reviews in the last 30 days mention {humanize(theme).replace('delivery late', 'late delivery')}" + \
            (f" and it's {trend}" if trend else "") + "."
    q = f"Most recent: \"{quote}\"." if quote else ""
    pos = F.review("pos")
    frame = ""
    if pos and pos.get("theme") != theme:
        frame = (f"The good news: {pos['occurrences_30d']} reviews praise {humanize(pos['theme'])}, so this is "
                 f"an operations fix, not a product problem.")
    cta = _cta(F, f"Want me to draft public replies to those {occ} reviews today?",
               f"Un {occ} reviews ke public replies aaj draft kar doon?")
    body = _join(line1, q, frame, cta)
    return _result(body, "binary_yes_no",
                   f"Emerging negative review theme ({theme}, {occ}/30d). Quotes the review verbatim, contrasts with the "
                   f"positive theme to localise the problem, offers the reply drafts. Lever: loss aversion + reciprocity.",
                   [F.salutation, line1, cta])


def supply_alert(F: FactPack):
    p = F.trigger.get("payload") or {}
    item = F.digest_item(p.get("alert_id")) or F.digest_by_kind("alert") or {}
    mol = p.get("molecule", "")
    batches = p.get("affected_batches") or []
    mfr = p.get("manufacturer")
    summ = item.get("summary", "")
    risk = ""
    if "sub-potency" in summ:
        risk = "Issue is sub-potency — no safety risk beyond weaker LDL control, but patients should be told and given a replacement."
    followup = ""
    lm = F.last_merchant_msg()
    if lm and lm.get("engagement") == "intent_action":
        followup = f"following up on your \"{lm.get('body')}\" — "
    chronic = F.agg.get("chronic_rx_count")
    line1 = f"{F.salutation}, {followup}{item.get('source', 'recall alert')}: voluntary recall on {mol} batches " \
            f"{', '.join(batches)}" + (f" ({mfr})" if mfr else "") + "."
    lst = f"I'll filter your {num(chronic)} chronic-Rx customers for anyone dispensed these batches." if chronic else ""
    cta = _cta(F, "Want me to also draft the patient WhatsApp and replacement-pickup note so it goes out today?",
               "Saath mein patient WhatsApp + replacement-pickup note bhi draft kar doon, taaki aaj hi chala jaaye?")
    body = _join(line1, risk, lst, cta)
    return _result(body, "binary_yes_no",
                   "Urgency-5 recall: exact batch numbers + manufacturer from the trigger, risk framed accurately from "
                   "the CDSCO digest (sub-potency, no safety risk), continues the merchant's earlier 'send me the list' "
                   "request and offers the complete outreach workflow.",
                   [F.salutation, line1, cta])


def category_seasonal(F: FactPack):
    p = F.trigger.get("payload") or {}
    trends = p.get("trends") or []
    ups, downs = [], []
    for tr in trends:
        m = re.match(r"([A-Za-z_]+?)_demand_([+-])(\d+)", tr)
        if m:
            name = humanize(m.group(1)).replace("cold cough", "cold & cough").replace("antifungal", "anti-fungal")
            if name.lower() == "ors":
                name = "ORS"
            (ups if m.group(2) == "+" else downs).append(f"{name} {m.group(2)}{m.group(3)}%")
    season = humanize(p.get("season", "")).replace("summer 2026", "summer")
    line1 = f"{F.salutation}, the {season} demand shift has started" + (f" in {F.city}" if F.city else "") + ": " + \
            ", ".join(ups) + (f" — while {', '.join(downs)}" if downs else "") + "."
    item = F.digest_matching("summer") or F.digest_by_kind("seasonal") or {}
    act = f"Shelf move: {lower_first(ensure_period(item['actionable']))}" if item.get("actionable") else ""
    content = F.content_item("summer")
    cta = _cta(F, f"Want me to send your repeat customers the '{content['title']}' checklist on WhatsApp?" if content
               else "Want me to post the summer essentials on your listing?",
               f"Repeat customers ko '{content['title']}' checklist WhatsApp kar doon?" if content else None)
    body = _join(line1, act, cta)
    return _result(body, "binary_yes_no",
                   "Category seasonal shift: exact demand deltas from the trigger, the shelf action from the digest, "
                   "and a ready patient-content piece from the library as the one-tap action.",
                   [F.salutation, line1, cta])


def gbp_unverified(F: FactPack):
    p = F.trigger.get("payload") or {}
    upl = p.get("estimated_uplift_pct")
    path = humanize(p.get("verification_path", "")).replace(" or ", " or a ")
    line1 = f"{F.salutation}, {_possessive(F.biz)} Google profile is still unverified" + \
            (f" — verified listings see an estimated {pct(upl)} more visibility" if upl else "") + "."
    cmp = F.peer_compare("calls")
    now = f"Right now: {num(F.perf.get('views', 0))} views and {num(F.perf.get('calls', 0))} calls in 30 days" + \
          (f" (peer average {num(cmp['peer'])} calls)" if cmp else "") + "."
    how = f"Verification is by {path}; I'll walk you through it." if path else ""
    cta = _cta(F, "Want to start it now?", "Abhi shuru karein?")
    body = _join(line1, now, how, cta)
    return _result(body, "binary_yes_no",
                   "Unverified GBP: uplift estimate from trigger + the merchant's own calls vs peer to make the gap "
                   "concrete; Vera handles the process. Lever: loss aversion + effort externalization.",
                   [F.salutation, line1, cta])


def weather_heatwave(F: FactPack):
    p = F.trigger.get("payload") or {}
    temp = next((v for k, v in p.items() if "temp" in k.lower()), None)
    city = p.get("city") or F.city
    line1 = f"{F.salutation}, {city} is at {temp}°C today." if temp else f"{F.salutation}, heatwave alert for {city} today."
    angle = {
        "restaurants": "Footfall drops in peak heat but delivery climbs — worth a delivery-first push this afternoon.",
        "pharmacies": "Expect ORS, sunscreen and anti-fungal demand to jump — keep them at counter view.",
        "gyms": "Midday sessions will thin out; early-morning and AC-studio slots are the ones to promote.",
        "salons": "Walk-ins dip in the afternoon heat; morning slots and hair-spa care are the easier sell.",
        "dentists": "Afternoon no-shows tend to rise in heat — a same-day reminder helps.",
    }.get(F.slug, "")
    offer, own = F.lead_offer()
    cta = _cta(F, f"Want me to post a heat-day update featuring '{offer}'?", f"'{offer}' ke saath heat-day post kar doon?")
    return _result(_join(line1, angle, cta), "binary_yes_no",
                   "Weather trigger translated into a category-specific operational move.", [F.salutation, line1, cta])


def category_trend_movement(F: FactPack):
    p = F.trigger.get("payload") or {}
    q = p.get("query") or p.get("topic")
    d = p.get("delta_yoy") or p.get("delta_pct")
    tr = F.trend_matching(q) if q else F.top_trend()
    q = q or (tr or {}).get("query")
    d = d if d is not None else (tr or {}).get("delta_yoy")
    line1 = f"{F.salutation}, '{q}' searches are up {pct(d)}" + (" YoY" if tr else "") + "."
    offer, own = F.lead_offer(keyword=(q or "").split()[0] if q else None)
    cta = _cta(F, f"Want me to update your listing description + a post to catch that demand with '{offer}'?",
               f"Is demand ke liye listing description + '{offer}' post update kar doon?")
    return _result(_join(line1, cta), "binary_yes_no", "Trend movement mapped to a listing action.",
                   [F.salutation, line1, cta])


def generic_merchant(F: FactPack):
    """Fallback for unknown / placeholder merchant-facing kinds: anchor on the merchant's real numbers."""
    kind = F.trigger.get("kind", "update")
    p = F.trigger.get("payload") or {}
    desc = next((p.get(k) for k in ("headline", "title", "event", "description", "note", "topic") if p.get(k)), None)
    line1 = f"{F.salutation}, {humanize(kind)}: {desc}." if desc else f"{F.salutation}, quick read on {F.biz} this week."
    weak = F.weakest_vs_peer()
    strong = F.strongest_vs_peer()
    fact = (f"Your {weak['text']}." if weak else f"You're ahead on {strong['text']}." if strong else "")
    offer, own = F.lead_offer()
    cta = _cta(F, f"Want me to refresh your listing with '{offer}' this week?",
               f"Is hafte listing ko '{offer}' ke saath refresh kar doon?")
    return _result(_join(line1, fact, cta), "binary_yes_no",
                   f"Kind '{kind}' handled by generic playbook: anchored on merchant's peer comparison and offer.",
                   [F.salutation, line1, cta])


# ============================================================ customer-facing playbooks

def _cust_open(F: FactPack) -> str:
    name = F.cust_parent or F.cust_name
    g = F.cust_greeting
    who = F.owner_display
    shop = F.biz + (f", {F.locality}" if F.locality and F.locality.lower() not in F.biz.lower() else "")
    if F.cust_lang == "hi":
        return f"Namaste {name} ji 🙏 {shop} se" + (f" ({who})" if who and F.slug != "dentists" else "") + "."
    if F.cust_lang == "hinglish":
        return f"Hi {name}, {shop} se message" + (f" — {who} here" if who and F.slug != "dentists" else "") + "."
    hello = f"{g} {name}!" if g else f"Hi {name},"
    return f"{hello} {who + ' from ' if who and F.slug != 'dentists' else ''}{shop} here."


def _slots(F: FactPack) -> list[dict]:
    p = F.trigger.get("payload") or {}
    return p.get("available_slots") or p.get("next_session_options") or p.get("slots") or []


def _cust_offer(F: FactPack, keyword=None) -> str | None:
    if keyword:
        for o in F.active_offers:
            if keyword.lower() in o.lower():
                return o
    for o in F.active_offers:
        if "first month" in o.lower() or "trial" in o.lower():
            continue  # new-user offers are wrong for existing customers
        return o
    return None


def recall_due(F: FactPack):
    p = F.trigger.get("payload") or {}
    svc = humanize(p.get("service_due", "")).replace("6 month", "6-month")
    last = p.get("last_service_date") or F.cust_last_visit
    slots = _slots(F)
    price = _cust_offer(F, keyword=_offer_core(svc).split()[-1] if svc else "cleaning") or _cust_offer(F)
    lang = F.cust_lang
    op = _cust_open(F)
    if lang in ("hi", "hinglish"):
        sw = svc or SERVICE_WORD.get(F.slug, ("next visit",))[0]
        l2 = (f"Aapki last visit {nice_date(last)} ko thi — {sw} ab due hai." if last
              else f"Aapka {sw} ab due hai.")
        l3 = ("Aapke liye slots: " + " ya ".join(s["label"] for s in slots[:2]) + ".") if slots else ""
        l4 = f"{price} wala offer abhi bhi valid hai." if price else ""
        cta = (f"Reply 1 for {slots[0]['label'].split(',')[0]}" + (f", 2 for {slots[1]['label'].split(',')[0]}" if len(slots) > 1 else "")
               + " — ya apna time bata dijiye.") if slots else "Slot book karne ke liye YES reply karein."
    else:
        sw = svc or SERVICE_WORD.get(F.slug, ("next visit",))[0]
        l2 = (f"Your last visit was on {nice_date(last)}, so your {sw} is due now." if last
              else f"Your {sw} is due now.")
        l3 = ("We've kept two slots for you: " + " or ".join(s["label"] for s in slots[:2]) + ".") if slots else ""
        l4 = f"Current offer: {price}." if price else ""
        cta = (f"Reply 1 for {slots[0]['label'].split(',')[0]}" + (f", 2 for {slots[1]['label'].split(',')[0]}" if len(slots) > 1 else "")
               + ", or tell us a time that suits you.") if slots else "Reply YES and we'll share this week's open slots."
    if F.slug == "dentists" and not F.cust_parent:
        op = op[:-1] + " 🦷" if op.endswith(".") else op + " 🦷"
    body = _join(op, l2, l3, l4, cta)
    return _result(body, "multi_choice_slot" if slots else "binary_yes_no",
                   f"Customer recall, sent as the merchant. Uses real last-visit date, the trigger's open slots"
                   f"{' (match the ' + (F.cust_pref_slot or '') + ' preference)' if slots and F.cust_pref_slot else ''} "
                   f"and the merchant's live price; language = {lang}. No medical claims.",
                   [F.cust_name, F.biz, l2, l3, cta], send_as="merchant_on_behalf")


def customer_lapsed(F: FactPack):
    p = F.trigger.get("payload") or {}
    days = p.get("days_since_last_visit")
    focus = humanize(p.get("previous_focus") or (F.customer or {}).get("preferences", {}).get("training_focus") or "")
    months = p.get("previous_membership_months")
    lang = F.cust_lang
    op = _cust_open(F)
    last = F.cust_last_visit
    free = next((o for o in F.active_offers if "free" in o.lower()), None) or _cust_offer(F)
    pref = F.cust_pref_slot
    if lang in ("hi", "hinglish"):
        gap = f"Aapko aaye {days} din ho gaye" if days else (f"Aapki last visit {nice_date(last)} ko thi" if last else "Kaafi time ho gaya")
        l2 = gap + " — break lena bilkul normal hai, koi pressure nahi."
        l3 = (f"Wapas shuru karne ke liye: {free}" + (f", aapke {pref} slot mein" if pref else "") + ".") if free else ""
        cta = {"pharmacies": "Aapka regular saaman ready rakh dein? YES reply karein.",
               "restaurants": "Is hafte ka menu bhej dein? YES reply karein."}.get(
            F.slug, "Is hafte ek slot hold kar dein? YES reply karein — no commitment.")
    else:
        gap = f"It's been {days} days since your last session" if days else (f"Your last visit was on {nice_date(last)}" if last else "It's been a while")
        extra = f" after {months} solid months with us" if months else ""
        l2 = gap + extra + " — breaks happen, no judgment."
        l3 = ""
        if free:
            l3 = f"To ease back in{' toward your ' + focus + ' goal' if focus else ''}: {free}" + \
                 (f", in your usual {_pref(pref)} slot" if pref else "") + " — no commitment, no auto-charge."
        cta = {"pharmacies": "Reply YES and we'll keep your regular items ready.",
               "restaurants": "Reply YES and we'll send you this week's menu."}.get(
            F.slug, "Reply YES and we'll hold a spot for you this week.")
    body = _join(op, l2, l3, cta)
    return _result(body, "binary_yes_no",
                   f"Lapsed customer win-back ({F.trigger.get('kind')}): no-shame framing, references their goal/tenure "
                   f"from context, uses the merchant's live free offer and the customer's slot preference.",
                   [F.cust_name, F.biz, l2, cta], send_as="merchant_on_behalf")


def appointment_tomorrow(F: FactPack):
    p = F.trigger.get("payload") or {}
    when = p.get("appointment_label") or (_when(p["appointment_iso"]) if p.get("appointment_iso") else None)
    svc = humanize(p.get("service", ""))
    lang = F.cust_lang
    op = _cust_open(F)
    thing = "table booking" if F.slug == "restaurants" else "appointment"
    if lang in ("hi", "hinglish"):
        l2 = (f"Reminder: kal aapki {thing} hai" if thing != "appointment" else "Reminder: kal aapka appointment hai") + \
             (f" — {when}" if when else "") + (f" ({svc})" if svc else "") + "."
        cta = "Confirm karne ke liye YES reply karein, ya time badalna ho toh CHANGE likhein."
    else:
        l2 = f"Just a reminder: your {thing} with us is tomorrow" + (f", {when}" if when else "") + (f" ({svc})" if svc else "") + "."
        cta = "Reply YES to confirm, or CHANGE if you need a different time."
    body = _join(op, l2, cta)
    return _result(body, "binary_confirm_cancel",
                   "Appointment reminder as the merchant; no time invented when the payload lacks one. Binary confirm "
                   "keeps no-shows down.", [F.cust_name, F.biz, l2, cta], send_as="merchant_on_behalf")


def chronic_refill_due(F: FactPack):
    p = F.trigger.get("payload") or {}
    mols = p.get("molecule_list") or []
    runout = p.get("stock_runs_out_iso")
    lang = F.cust_lang
    senior = (F.customer or {}).get("identity", {}).get("senior_citizen")
    offers = F.active_offers
    senior_offer = next((o for o in offers if "senior" in o.lower()), None) if senior else None
    deliv = next((o for o in offers if "delivery" in o.lower()), None)
    addr = p.get("delivery_address_saved")
    recall_note = ""
    for d in F.category.get("digest") or []:
        if d.get("kind") == "alert":
            hit = next((m for m in mols if m.lower() in (d.get("title", "") + d.get("summary", "")).lower()), None)
            if hit:
                recall_note = hit
    name = F.cust_name or ""
    via = (F.customer or {}).get("preferences", {}).get("channel", "")
    if not mols:
        return generic_customer(F)
    if lang in ("hi", "hinglish"):
        op = f"Namaste 🙏 {F.biz}" + (f", {F.locality}" if F.locality else "") + " se."
        subj = (name.replace("Mr. ", "") + " ji") if "via" in via else "Aapki"
        l2 = (f"{subj} ki monthly dawaiyan ({', '.join(mols)})" if "via" in via else f"Aapki monthly dawaiyan ({', '.join(mols)})") + \
             (f" {nice_date(runout)} tak khatam ho jayengi." if runout else " refill ke liye due hain.")
        thr = re.search(r"₹\s?[\d,]+", deliv or "")
        so = re.sub(r"\s*OFF", " discount", senior_offer or "", flags=re.I).lower()
        l3 = "Same refill ready rakha hai" + (f"; {so} lagega" if senior_offer else "") + \
             ((f", aur {thr.group(0)} se upar free home delivery" if thr else ", aur free home delivery") +
              (" saved address par" if addr else "") if deliv else "") + "."
        l4 = f"{recall_note.title()} ka refill recall wale batches se alag, unaffected stock se hoga." if recall_note else ""
        cta = "Dispatch ke liye CONFIRM reply karein, ya dose mein koi badlav ho toh bata dijiye."
    else:
        op = f"Hello from {F.biz}" + (f", {F.locality}" if F.locality else "") + "."
        l2 = f"{name + chr(39) + 's' if name else 'Your'} regular medicines ({', '.join(mols)}) " + \
             (f"run out on {nice_date(runout)}." if runout else "are due for refill.")
        l3 = "The same refill is ready" + (f" with {senior_offer}" if senior_offer else "") + \
             (f"; {deliv.lower()}" + (" to your saved address" if addr else "") if deliv else "") + "."
        l4 = f"Given the current {recall_note} batch recall, we'll dispense from unaffected stock." if recall_note else ""
        cta = "Reply CONFIRM to dispatch, or tell us if the dosage has changed."
    body = _join(op, l2, l3, l4, cta)
    return _result(body, "binary_confirm_cancel",
                   "Chronic refill as the pharmacy: exact molecules + run-out date from trigger, merchant's live senior "
                   "discount + delivery offer, and a cross-check against the category recall alert"
                   f"{' (' + recall_note + ')' if recall_note else ''}. Respectful Hindi for a senior via family channel.",
                   [F.cust_name, F.biz, l2, cta], send_as="merchant_on_behalf")


def trial_followup(F: FactPack):
    p = F.trigger.get("payload") or {}
    tdate = p.get("trial_date")
    slots = _slots(F)
    child = F.cust_name if F.cust_parent else None
    op = _cust_open(F)
    lang = F.cust_lang
    trial_word = SERVICE_WORD.get(F.slug, ("", "first visit"))[1]
    if lang in ("hi", "hinglish"):
        l2 = (f"{child} ki" if child else "Aapki") + f" {trial_word}" + (f" ({nice_date(tdate)})" if tdate else "") + " ke liye shukriya!"
        l3 = ("Agla session: " + " ya ".join(s["label"] for s in slots[:2]) + ".") if slots else ""
        cta = f"{child + ' ke liye' if child else 'Aapke liye'} spot save kar dein? YES reply karein."
    else:
        l2 = f"Thanks for bringing {child} for the {trial_word}" if child else f"Thanks for coming in for your {trial_word}"
        l2 += f" on {nice_date(tdate)}!" if tdate else "!"
        l3 = ("Next session: " + " or ".join(s["label"] for s in slots[:2]) +
              (f" — fits your {_pref(F.cust_pref_slot)} preference." if F.cust_pref_slot else ".")) if slots else ""
        cta = f"Shall we save a spot for {child if child else 'you'}? Reply YES."
    body = _join(op, l2, l3, cta)
    return _result(body, "binary_yes_no",
                   "Trial follow-up as the merchant: addresses the parent for a child trial, uses the real trial date and "
                   "the next session from the payload, single YES.", [F.cust_name, F.biz, l2, cta],
                   send_as="merchant_on_behalf")


def wedding_package_followup(F: FactPack):
    p = F.trigger.get("payload") or {}
    wd = p.get("wedding_date") or (F.customer or {}).get("preferences", {}).get("wedding_date")
    days = p.get("days_to_wedding")
    trial = p.get("trial_completed")
    nxt = _program_name(p.get("next_step_window_open", ""))
    op = f"Hi {F.cust_name} 💍 {F.first_name} from {F.biz}" + (f", {F.locality}" if F.locality and F.locality not in F.biz else "") + "."
    l2 = f"{days} days to your wedding on {nice_date(wd)}" if days and wd else "Your big day is coming up"
    l2 += f" — and after your bridal trial on {nice_date(trial)}, the next step is the {nxt}." if trial and nxt else "."
    l3 = "Starting it now gives your skin time to settle well before the functions begin."
    pref = F.cust_pref_slot
    cta = f"Shall we pencil in a {_pref(pref) + ' ' if pref else ''}consultation to plan it? Reply YES."
    body = _join(op, l2, l3, cta)
    return _result(body, "binary_yes_no",
                   "Bridal follow-up as the salon owner: exact wedding date/day-count and trial date from payload, "
                   "names the next program without inventing a price, honours Saturday preference.",
                   [F.cust_name, F.biz, l2, cta], send_as="merchant_on_behalf")


def generic_customer(F: FactPack):
    kind = F.trigger.get("kind", "")
    slots = _slots(F)
    op = _cust_open(F)
    last = F.cust_last_visit
    lang = F.cust_lang
    offer = _cust_offer(F)
    if lang in ("hi", "hinglish"):
        l2 = (f"Aapki last visit {nice_date(last)} ko thi — " if last else "") + "aapke next visit ka time ho gaya hai."
        l3 = f"{offer} abhi available hai." if offer else ""
        cta = "Slot book karne ke liye YES reply karein."
    else:
        sw = SERVICE_WORD.get(F.slug, ("next visit",))[0]
        l2 = (f"Your last visit was on {nice_date(last)}, " if last else "") + f"so it's a good time for your {sw}."
        l3 = f"{offer} is available right now." if offer else ""
        cta = "Reply YES and we'll share this week's open slots."
    if slots:
        cta = ("Open slots: " + " or ".join(s["label"] for s in slots[:2]) + ". Reply 1 or 2 to book.")
    body = _join(op, l2, l3, cta)
    return _result(body, "multi_choice_slot" if slots else "binary_yes_no",
                   f"Customer '{kind}' via generic playbook: name, real last-visit date, merchant's live offer only.",
                   [F.cust_name, F.biz, l2, cta], send_as="merchant_on_behalf")


MERCHANT_PLAYBOOKS = {
    "research_digest": research_digest,
    "category_research_digest_release": research_digest,
    "research_digest_release": research_digest,
    "regulation_change": regulation_change,
    "cde_opportunity": cde_opportunity,
    "competitor_opened": competitor_opened,
    "perf_dip": perf_dip,
    "perf_spike": perf_spike,
    "milestone_reached": milestone_reached,
    "dormant_with_vera": dormant_with_vera,
    "winback_eligible": winback_eligible,
    "renewal_due": renewal_due,
    "festival_upcoming": festival_upcoming,
    "curious_ask_due": curious_ask_due,
    "scheduled_recurring": curious_ask_due,
    "active_planning_intent": active_planning_intent,
    "seasonal_perf_dip": seasonal_perf_dip,
    "ipl_match_today": ipl_match_today,
    "review_theme_emerged": review_theme_emerged,
    "supply_alert": supply_alert,
    "category_seasonal": category_seasonal,
    "gbp_unverified": gbp_unverified,
    "weather_heatwave": weather_heatwave,
    "category_trend_movement": category_trend_movement,
}

CUSTOMER_PLAYBOOKS = {
    "recall_due": recall_due,
    "customer_lapsed_soft": customer_lapsed,
    "customer_lapsed_hard": customer_lapsed,
    "winback_customer": customer_lapsed,
    "appointment_tomorrow": appointment_tomorrow,
    "chronic_refill_due": chronic_refill_due,
    "trial_followup": trial_followup,
    "wedding_package_followup": wedding_package_followup,
}


def compose_raw(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
                extra: dict | None = None) -> dict:
    F = factmod.build(category, merchant, trigger, customer, extra)
    kind = trigger.get("kind", "")
    is_customer = trigger.get("scope") == "customer" or bool(trigger.get("customer_id"))
    if is_customer:
        if not customer:
            return _result("", "none", "Customer-scoped trigger but no customer context pushed; not guessing a name.",
                           [], send_as="merchant_on_behalf", skip="missing customer context")
        fn = CUSTOMER_PLAYBOOKS.get(kind, generic_customer)
        # chronic refill at a non-pharmacy makes no sense as a refill → generic follow-up
        if kind == "chronic_refill_due" and not (trigger.get("payload") or {}).get("molecule_list"):
            fn = generic_customer
    else:
        fn = MERCHANT_PLAYBOOKS.get(kind, generic_merchant)
    out = fn(F)
    out["_facts"] = F
    out.setdefault("send_as", "merchant_on_behalf" if is_customer else "vera")
    if not out.get("template_name"):
        out["template_name"] = ("merchant_" if is_customer else "vera_") + (kind or "generic") + "_v1"
    out["_facts"] = F
    return out
