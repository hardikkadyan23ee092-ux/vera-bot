"""/v1/reply state machine.

Rules classify first (cheap, deterministic, testable); text is drafted per situation.
Priority: hostile/opt-out > auto-reply > commitment > slot pick > not-now > decline >
off-topic > question > acknowledgement > engaged.
"""
from __future__ import annotations

import re

import llm
from facts import build
from util import humanize, is_hindi, parse_dt
from validator import QUALIFYING, check

AUTO_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)", r"will (respond|reply|get back)( to you)? (shortly|soon|asap)",
    r"\bautomated (assistant|message|reply|response)\b", r"\bauto[- ]?reply\b", r"out of (the )?office",
    r"currently (unavailable|away|closed)", r"(our )?business hours", r"we have received your (message|query|enquiry)",
    r"aapki jaankari ke liye", r"team tak pahuncha", r"our team will", r"this is an automated",
    r"i am an automated", r"main ek automated", r"please leave (a|your) message", r"we('| a)re closed",
]
HOSTILE_PATTERNS = [
    r"\bstop\b", r"\bunsubscribe\b", r"not interested", r"don'?t (message|text|contact|disturb)", r"do not (message|text|contact)",
    r"\bspam\b", r"\buseless\b", r"\bbothering\b", r"leave me alone", r"\bblock(ed)?\b", r"\bharass",
    r"mat (bhejo|karo|bhejna)", r"band karo", r"pareshan", r"\bidiot\b", r"\bnonsense\b", r"\bbakwas\b", r"\bfraud\b",
    r"\bscam\b", r"shut up", r"\bwtf\b", r"\bdamn\b", r"\bbloody\b", r"get lost",
]
COMMIT_PATTERNS = [
    r"^\s*(yes|yess+|yeah|yep|yup|ok(ay)?|sure|haan|han|ha|ji|done|confirm(ed)?|go|chalo|theek hai|thik hai|kar do|karo|please)\b",
    r"let'?s do it", r"lets do it", r"go ahead", r"do it", r"sounds good", r"please (do|send|proceed|go)", r"\bproceed\b",
    r"send (it|me|the)", r"i want to (join|start|do)", r"judna hai", r"judrna hai", r"kar do", r"kar dijiye", r"bhej do",
    r"\bconfirm\b", r"\bbook( it|)\b", r"\bdraft it\b", r"\bgo live\b", r"\bpublish\b", r"what'?s next", r"whats next",
]
LATER_PATTERNS = [r"\blater\b", r"\bbusy\b", r"baad mein", r"\bkal\b", r"tomorrow", r"not now", r"abhi nahi", r"in a meeting",
                  r"call (you|me) later", r"\bweekend\b", r"next week"]
DECLINE_PATTERNS = [r"^\s*(no|nope|nahi|nahin|na)\b", r"no thanks", r"not needed", r"zaroorat nahi", r"don'?t need"]
OFFTOPIC_PATTERNS = [r"\bgst\b", r"\bincome tax\b", r"\btax\b", r"\bitr\b", r"\bloan\b", r"\binsurance\b", r"\blawyer\b",
                     r"\blegal\b", r"\baccount(ing|ant)\b", r"\bvisa\b", r"\belectricity\b", r"\brent\b", r"\bcricket score\b",
                     r"\bpassport\b", r"\bbank\b"]
THANKS_PATTERNS = [r"^\s*(thanks|thank you|thx|ty|shukriya|dhanyavaad|great|perfect|awesome|nice|cool)\W*$"]


def _any(pats, text):
    t = text.lower()
    return any(re.search(p, t) for p in pats)


def classify(msg: str, prev_inbound: str | None) -> str:
    m = (msg or "").strip()
    if not m:
        return "empty"
    if prev_inbound and m.lower() == prev_inbound.strip().lower() and len(m) > 25:
        return "auto_reply"
    if _any(AUTO_PATTERNS, m):
        return "auto_reply"
    if _any(HOSTILE_PATTERNS, m):
        # "stop" + off-topic ask in the same breath: still an exit; handled by caller
        return "hostile"
    if re.fullmatch(r"\s*[1-3]\s*[.)]?\s*", m):
        return "slot_pick"
    if _any(COMMIT_PATTERNS, m):
        return "commit"
    if _any(LATER_PATTERNS, m):
        return "later"
    if _any(DECLINE_PATTERNS, m):
        return "decline"
    if _any(OFFTOPIC_PATTERNS, m):
        return "offtopic"
    if "?" in m or re.match(r"^\s*(what|how|why|when|where|which|who|can|could|is|are|does|do|kya|kaise|kitna|kab|kyun)\b", m.lower()):
        return "question"
    if _any(THANKS_PATTERNS, m):
        return "thanks"
    return "engaged"


# ---------------------------------------------------------------- action copy per trigger kind

def _action_copy(kind: str, F, hi: bool) -> tuple[str, str]:
    """(what Vera is doing now, next step with CONFIRM). Never phrased as a question."""
    p = F.trigger.get("payload") or {}
    k = kind or ""
    if k in ("research_digest", "research_digest_release"):
        a = "pulling the abstract and drafting a patient-friendly WhatsApp on it now"
        n = "draft lands here for a quick check — reply CONFIRM and I'll schedule it"
    elif k == "regulation_change":
        a = "preparing the one-page audit checklist for your team now"
        n = "I'll also set a reminder two weeks before the deadline — reply CONFIRM to lock that in"
    elif k == "cde_opportunity":
        a = "sending the registration details now"
        n = "a reminder is set for the day — reply CONFIRM if you want it on your calendar too"
    elif k == "supply_alert":
        batches = ", ".join(p.get("affected_batches") or [])
        n_rx = F.agg.get("chronic_rx_count")
        a = f"filtering your {n_rx} chronic-Rx customers for batches {batches} and drafting the patient note + pickup steps" if n_rx \
            else f"filtering your customer list for batches {batches} and drafting the patient note"
        n = "list + draft land here shortly — reply CONFIRM to send the notes"
    elif k == "ipl_match_today":
        a = "putting the delivery-only match-night combo live on your listing now"
        n = "I'll share a preview before the match — reply CONFIRM to publish"
    elif k in ("perf_dip", "gbp_unverified", "renewal_due"):
        a = "starting on the fixes now (verification + a live offer on your listing)" if F.verified is False \
            else "refreshing your listing with a live offer and a new post now"
        n = "you'll get a preview here — reply CONFIRM to publish"
    elif k == "active_planning_intent":
        a = f"finalising the {humanize(p.get('intent_topic', 'plan'))} post and creatives now"
        n = "preview comes here next — reply CONFIRM to publish"
    elif k == "competitor_opened":
        a = "adding your best review highlights to the profile and drafting the post now"
        n = "draft lands here in a few minutes — reply CONFIRM to publish"
    elif k in ("review_theme_emerged",):
        a = "drafting calm public replies to those reviews now"
        n = "you'll see them here first — reply CONFIRM to post"
    elif k == "category_seasonal":
        a = "sending the summer checklist to your repeat customers as a draft broadcast"
        n = "preview comes here — reply CONFIRM to send"
    elif k == "curious_ask_due":
        a = "turning that into a Google post and a ready price-reply now"
        n = "draft lands here — reply CONFIRM to publish"
    else:
        lm = F.last_merchant_msg() if F.merchant else None
        if lm and lm.get("engagement") in ("intent_action", "intent_planning", "intent_question"):
            a = f"picking up your request (\"{lm.get('body')}\") and drafting it now"
        else:
            a = "drafting it now"
        n = "you'll get the draft here — reply CONFIRM to go live"
    if hi:
        return ("Ho gaya — " + a, "Next: " + n)
    return ("Done — " + a, "Next: " + n)


def _slots(F):
    p = F.trigger.get("payload") or {}
    return p.get("available_slots") or p.get("next_session_options") or []


# ---------------------------------------------------------------- main entry

def handle(engine, req: dict) -> dict:
    conv_id = req.get("conversation_id") or "conv_unknown"
    mid = req.get("merchant_id")
    msg = req.get("message") or ""
    role = req.get("from_role") or "merchant"
    now = req.get("received_at")

    with engine.lock:
        conv = engine.conversations.get(conv_id)
        if conv is None:
            # Unknown conversation: attach to the merchant's latest thread for context, if any.
            ms_prev = engine.mstate(mid)
            prev = engine.conversations.get(ms_prev.get("last_conv_id") or "")
            conv = {"conversation_id": conv_id, "merchant_id": mid or (prev or {}).get("merchant_id"),
                    "customer_id": req.get("customer_id"), "trigger_id": (prev or {}).get("trigger_id"),
                    "kind": (prev or {}).get("kind"), "send_as": "merchant_on_behalf" if role == "customer" else "vera",
                    "status": "active", "bot_turns": 0, "commit_count": 0, "turns": [], "first_body": None}
            engine.conversations[conv_id] = conv
        mid = mid or conv.get("merchant_id")
        ms = engine.mstate(mid)
        prev_inbound = ms.get("last_inbound")
        conv["turns"].append({"from": role, "body": msg, "ts": now})
        kind_msg = classify(msg, prev_inbound)
        ms["last_inbound"] = msg

        merchant = engine.get("merchant", mid) or {"merchant_id": mid, "identity": {}}
        trigger = engine.get("trigger", conv.get("trigger_id")) or {"kind": conv.get("kind") or "", "payload": {}}
        category = engine._category_for(merchant, trigger) or {}
        customer = engine.get("customer", conv.get("customer_id")) if conv.get("customer_id") else None
        F = build(category, merchant, trigger, customer)
        hi = is_hindi(msg) or (role == "customer" and F.cust_lang in ("hi", "hinglish"))
        resp = _decide(engine, conv, ms, F, kind_msg, msg, role, hi, now)

        # anti-repetition inside a conversation
        if resp.get("action") == "send":
            prior = {t["body"] for t in conv["turns"] if t["from"] == "bot"}
            if resp["body"] in prior:
                resp["body"] += " (Just reply here whenever you're ready.)" if not hi else " (Jab ready hon, yahin reply kar dijiye.)"
            conv["turns"].append({"from": "bot", "body": resp["body"], "ts": now})
            conv["bot_turns"] += 1
        elif resp.get("action") == "end":
            conv["status"] = "ended"
        elif resp.get("action") == "wait":
            conv["status"] = "waiting"
        return resp


def _decide(engine, conv, ms, F, kind_msg, msg, role, hi, now):
    name = F.first_name
    topic = humanize(conv.get("kind") or "update")

    # ---------------- hostile / opt-out
    if kind_msg == "hostile":
        ms["opted_out"] = True if role == "merchant" else ms["opted_out"]
        ms["auto_count"] = 0
        return {"action": "end", "rationale": "Merchant signalled opt-out/frustration. Ending immediately and suppressing "
                                              "further proactive messages to this merchant."}

    # ---------------- auto-reply (counted per merchant: auto-replies arrive across conversation ids)
    if kind_msg == "auto_reply":
        ms["auto_count"] += 1
        n = ms["auto_count"]
        if n == 1:
            body = ("Lagta hai yeh auto-reply hai 🙂 Owner/manager dekhein toh bas YES reply kar dijiye — "
                    f"{topic} ki details ready hain.") if hi else \
                   (f"Looks like an auto-reply 🙂 When the owner sees this, a quick YES is all I need to send over "
                    f"the {topic} details.")
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "Detected WhatsApp Business auto-reply (canned phrasing). One short prompt aimed at the "
                                 "human owner; will not burn more turns if it repeats."}
        if n == 2:
            base = parse_dt(now)
            if base:
                from datetime import timedelta
                ms["wait_until"] = (base + timedelta(seconds=86400)).isoformat()
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same auto-reply again — owner not at the phone. Backing off 24h instead of spending turns."}
        return {"action": "end", "rationale": f"Auto-reply {n}x in a row with no human response; closing the "
                                              f"conversation gracefully."}

    ms["auto_count"] = 0  # a real human replied

    # an ended conversation that receives a new message: only respond if it's non-hostile, and softly
    if conv.get("status") == "ended" and kind_msg not in ("commit", "question", "offtopic", "slot_pick"):
        return {"action": "end", "rationale": "Conversation already closed; no further outreach."}

    # ---------------- customer-facing replies
    if role == "customer":
        return _customer_reply(conv, F, kind_msg, msg, hi)

    # ---------------- commitment → action mode (never another qualifying question)
    if kind_msg == "commit":
        conv["commit_count"] = conv.get("commit_count", 0) + 1
        ms["opted_out"] = False
        if conv["commit_count"] >= 2:
            body = ("Confirmed ✅ Live ho gaya. Next week results ke saath update bhejungi."
                    if hi else "Confirmed ✅ It's live. I'll send you a short results update next week — nothing else needed from you.")
            return {"action": "send", "body": body, "cta": "none",
                    "rationale": "Second confirmation: execute and close the loop; no further asks."}
        a, n = _action_copy(conv.get("kind"), F, hi)
        body = f"{a}. {n}."
        errs = [q for q in QUALIFYING if q in body.lower()]
        if errs:
            body = f"{'Ho gaya' if hi else 'Done'} — drafting it now. Next: reply CONFIRM and it goes live."
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": "Merchant committed; switched from pitch to action immediately — states what Vera is "
                             "doing now and the single confirm step (no re-qualification)."}

    if kind_msg == "slot_pick":
        return {"action": "send", "body": ("Noted ✅ Option " + msg.strip()[0] + " — setting it up now. Reply CONFIRM to lock it."),
                "cta": "binary_confirm_cancel", "rationale": "Merchant picked an option; executing."}

    # ---------------- not now
    if kind_msg == "later":
        secs = 86400 if re.search(r"tomorrow|\bkal\b|next week|weekend", msg.lower()) else 3600
        return {"action": "wait", "wait_seconds": secs,
                "rationale": f"Merchant asked for time; backing off {secs // 3600}h without pushing."}

    # ---------------- polite decline
    if kind_msg == "decline":
        return {"action": "end", "rationale": "Merchant declined; closing politely without a counter-pitch."}

    # ---------------- off-topic (GST etc.): decline in one line, steer back once
    if kind_msg == "offtopic":
        what = "GST filing" if "gst" in msg.lower() else "that"
        if ms.get("opted_out"):
            body = (f"{what.capitalize()} ke liye aapke CA sahi rahenge — main usme help nahi kar sakti. Kabhi listing ya "
                    f"offers mein zaroorat ho toh bas 'Hi Vera' likh dijiye." if hi else
                    f"Sorry — {what} is outside what I can help with; your CA is the right person. I won't message "
                    f"further, but if you ever want help with your listing, just send 'Hi Vera'.")
            return {"action": "send", "body": body, "cta": "none",
                    "rationale": "Merchant had opted out, then asked an off-topic question: answer politely, no re-pitch."}
        back = f"Coming back to the {topic} — shall I go ahead with it?" if conv.get("kind") else \
            "Anything on your Google profile, offers or customer messages I can take off your plate today?"
        body = (f"{what.capitalize()} mere scope se bahar hai — uske liye aapke CA best rahenge. " +
                (f"Wapas {topic} par — aage badhaun?" if conv.get("kind") else "Profile, offers ya customer messages mein kuch help chahiye?")) \
            if hi else f"I'll have to leave {what} to your CA — it's outside what I can help with. {back}"
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "Out-of-scope request declined in one line; redirected to the original task."}

    # ---------------- turn cap: don't nag
    if conv.get("bot_turns", 0) >= 4:
        return {"action": "end", "rationale": "4 bot turns without a commitment — exiting gracefully rather than nagging."}

    # ---------------- genuine question
    if kind_msg == "question":
        ans = None
        if llm.enabled():
            facts = {"merchant": F.merchant, "trigger": F.trigger, "category_digest": F.category.get("digest"),
                     "offer_catalog": F.category.get("offer_catalog"), "last_bot_message": conv.get("first_body")}
            cand = llm.answer(msg, facts, F.taboos)
            if cand and not check(cand, contexts=(F.category, F.merchant, F.trigger, F.customer), taboos=F.taboos):
                ans = cand
        if not ans:
            core = _core_fact(conv, msg, F)
            ans = (f"Achha sawaal. Short answer: {core} Main baaki kaam sambhal lungi — aage badhaun?" if hi else
                   f"Good question. Short answer: {core} I'll handle the rest — shall I go ahead?")
        return {"action": "send", "body": ans, "cta": "binary_yes_no",
                "rationale": "Answered the merchant's question from context only, then one forward-moving ask."}

    if kind_msg == "thanks":
        return {"action": "end", "rationale": "Merchant acknowledged; nothing pending — closing without extra messages."}

    # ---------------- engaged but no explicit yes: advance one concrete step
    a, n = _action_copy(conv.get("kind"), F, hi)
    body = (f"Samajh gayi. Main ise simple rakhti hoon: {a.split('— ', 1)[-1]}. Bas YES bol dijiye, baaki main kar dungi." if hi
            else f"Got it. Keeping this simple: I can take care of it — {a.split('— ', 1)[-1]}. Just reply YES and I'll handle the rest.")
    return {"action": "send", "body": body, "cta": "binary_yes_no",
            "rationale": "Engaged reply without explicit commitment: offer the concrete next step with a single YES."}


def _core_fact(conv, question: str = "", F=None) -> str:
    """Pick the sentence from what we already told the merchant that best answers the question."""
    from util import split_sentences
    q = (question or "").lower()
    sents = [x for x in split_sentences(conv.get("first_body") or "")[1:]
             if "?" not in x and "reply" not in x.lower()]
    if re.search(r"price|cost|charge|fee|kitna|kitne|paisa|amount|how much", q):
        hit = next((x for x in sents if "₹" in x), None)
        if not hit and F is not None:
            amt = (F.trigger.get("payload") or {}).get("renewal_amount")
            if amt:
                from util import money
                return f"the renewal is {money(amt)}."
            offer, own = F.lead_offer()
            if own and offer and "₹" in offer:
                return f"your live offer is {offer}."
        return hit or "I'll confirm the exact cost for you before anything goes live."
    if re.search(r"\bwhen\b|\bkab\b|date|deadline|time", q):
        hit = next((x for x in sents if re.search(r"\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)|\d(am|pm)", x)), None)
        if hit:
            return hit
    for x in sents:
        if re.search(r"\d", x):
            return x
    return "it's a small, done-for-you change on your listing — no extra effort from you."


def _customer_reply(conv, F, kind_msg, msg, hi):
    slots = _slots(F)
    biz = F.biz
    if kind_msg == "slot_pick" or (kind_msg in ("commit", "engaged") and slots and re.search(r"\b(wed|thu|fri|sat|sun|mon|tue)", msg.lower())):
        idx = 0
        m = re.search(r"[1-3]", msg)
        if m:
            idx = int(m.group()) - 1
        else:
            for i, s in enumerate(slots):
                if s["label"][:3].lower() in msg.lower():
                    idx = i
        if slots and 0 <= idx < len(slots):
            lab = slots[idx]["label"]
            body = (f"Booked ✅ {lab} — {biz}. Time badalna ho toh CHANGE reply kar dijiye." if hi else
                    f"Booked ✅ {lab} at {biz}. If anything changes, just reply CHANGE.")
        else:
            body = ("Done ✅ Hum aapko slot confirm karke message karenge." if hi else
                    "Done ✅ We'll message you shortly to confirm the slot.")
        return {"action": "send", "body": body, "cta": "none", "rationale": "Customer picked a slot; confirmed booking."}
    if kind_msg == "commit":
        if conv.get("kind") == "chronic_refill_due":
            body = ("Dispatch ho raha hai ✅ Delivery saved address par hogi. Koi dose change ho toh bata dijiye."
                    if hi else "Dispatching now ✅ Delivery goes to your saved address. Tell us if anything about the dose changes.")
        else:
            body = ("Done ✅ Aapka spot hold kar diya hai — confirmation jaldi bhejenge." if hi else
                    "Done ✅ Your spot is on hold — we'll send the confirmation shortly.")
        return {"action": "send", "body": body, "cta": "none", "rationale": "Customer said yes; executed."}
    if kind_msg in ("hostile", "decline"):
        return {"action": "end", "rationale": "Customer declined; no further messages."}
    if kind_msg == "later":
        return {"action": "wait", "wait_seconds": 86400, "rationale": "Customer asked for later; backing off a day."}
    if kind_msg == "thanks":
        return {"action": "end", "rationale": "Customer acknowledged; nothing pending."}
    body = (f"Zaroor — {biz} ki team aapko call karke details bata degi. Slot hold karna ho toh YES reply karein." if hi else
            f"Sure — the {biz} team will call you with the details. Reply YES if you'd like us to hold a slot meanwhile.")
    return {"action": "send", "body": body, "cta": "binary_yes_no",
            "rationale": "Customer question routed to the merchant's team; one clear next step."}
