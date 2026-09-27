# Vera bot — magicpin AI Challenge

**Approach: code decides, templates speak, a validator guards.** Every message is built in three steps:

1. **Fact pack** (`vera/facts.py`). This step resolves the 4 contexts into verifiable facts:
   - digest items resolved by id, always from the latest category version
   - performance compared with peer_stats
   - live offers only, with catalog suggestions clearly labelled as suggestions
   - parsed signals, review themes with their quotes, and conversation history
   - the customer's consent, language preference and preferred slot
2. **Decision playbook per trigger kind** (`vera/composer.py`, 25+ kinds plus generic fallbacks). Each playbook chooses:
   - whether to send at all
   - the angle and the recommendation
   - the compulsion lever
   - a single CTA as the final sentence

   It also cross-references the other contexts. Examples:
   - IPL on a weekend: the digest shows Saturday matches cost 12% of covers, so it recommends a delivery-only combo and keeps the Tue–Thu BOGO for weeknights.
   - Competitor at ₹199: it doesn't price-match and leads with the "explains patiently" reviews instead.
   - Seasonal gym dip: it advises against acquisition spend and redirects to retention, using churn vs peer.
   - Chronic refill that includes atorvastatin: it promises stock unaffected by the active recall.
   - Planning intent: it delivers the draft now instead of asking more questions.
3. **Validator** (`vera/validator.py`). Every number in the body must trace back to a context field or simple arithmetic on one. The validator also rejects taboo vocabulary and URLs. On all 100 dataset triggers it finds 0 ungrounded numbers.

**Decision rules in `/v1/tick`** (`vera/engine.py`):
- Merchant-facing messages are capped at one per merchant per tick, highest urgency first; the rest are deferred to later ticks.
- Suppression keys are deduplicated, and the same body is never sent twice.
- Expired triggers are skipped when the tick uses a simulated clock.
- A merchant who opted out, or who is in an auto-reply back-off, is not messaged.
- Customer triggers are not sent without consent or a customer profile.
- A renewal more than 45 days away gets no nudge.
- A review-theme trigger with no review data is skipped rather than invented.

**Replies** (`vera/replies.py`) use a rule-first state machine with this priority: hostile > auto-reply > commitment > slot pick > later > decline > off-topic > question.
- **Auto-replies** are counted per merchant, because they arrive across conversation IDs. The first gets one nudge to the owner, the second waits 24h, and the third ends the conversation.
- **Commitments** switch straight to action mode ("Done — … Reply CONFIRM"), with no re-qualifying.
- **Off-topic requests** such as GST get a one-line decline and a redirect.
- **Hindi/Hinglish** is detected per turn and mirrored.
- **Turn cap:** after 4 bot turns the conversation exits.

**Language.** Merchants whose languages include `hi` get a Hinglish CTA; dentists stay clinical English. Customers follow `language_pref`: `hi` gets respectful Hindi, `hi-en mix` gets Hinglish, and `ta/te/kn-en` gets a regional greeting.

**Optional LLM.** Set `VERA_LLM_API_KEY` (Anthropic or OpenAI-compatible) to enable two things:
- answering on-topic questions mid-conversation
- with `VERA_LLM_POLISH=1`, rephrasing the drafts

LLM output is kept only if it passes the same validator; otherwise the deterministic text ships. Temperature is 0 and results are cached.

**Tradeoffs.**
- Deterministic templates give up some stylistic variety in exchange for zero hallucination, sub-50ms ticks, and fully reproducible output. The rubric's heaviest penalties are for fabrication, and templates are auditable.
- State is held in memory, so the service must run as a single always-on instance.
- The generated placeholder triggers have no event details, so those messages anchor on the merchant's own numbers rather than inventing specifics.

**What extra context would help most:**
- real appointment and slot calendars (for recall and appointment reminders)
- per-offer redemption counts
- review counts and ratings on every merchant
- the actual customer list behind each aggregate (for example, who was dispensed a recalled batch)
- a `now` field inside each trigger, so relative dates ("in 5 days") are safe to compute

**Files:**
- `server.py`: the HTTP API (stdlib only, no dependencies)
- `bot.py`: `compose()`
- `conversation_handlers.py`: `respond()`
- `make_submission.py`: generates `submission.jsonl`
- `tests/`: `harness_test.py`, `run_judge_sim.py`, `dump_all.py`
- `DEPLOY.md`: deployment steps
