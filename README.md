# Vera+ — magicpin merchant engagement bot

A single-file HTTP bot (`bot.py`, stdlib only, no pip installs) implementing the challenge contract:
`GET /v1/healthz`, `GET /v1/metadata`, `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `POST /v1/teardown`.

## Approach
- **Grounded, deterministic composer.** One composer per trigger kind (research digest, compliance, supply recall, perf dip/spike, seasonal dip, renewal, festival, IPL, curious-ask, winback, dormancy, review theme, milestone, competitor, planning intent, GBP verification, CDE, category seasonal) plus a customer-facing composer (recall, refill, lapse winback, trial follow-up, bridal, appointment). Every number, name, date and citation is read from the pushed contexts — nothing is invented. Sparse/placeholder triggers fall back to a message built only from the merchant's verified performance numbers.
- **Voice:** "Dr." salutation for dentists, owner first name elsewhere, Hindi-English code-mix when the merchant lists `hi` or the customer prefers it, category taboo words scrubbed, URLs stripped, one CTA per message landing in the last sentence.
- **Consent:** customer messages only go out when the customer has opted in; sent as `merchant_on_behalf`.
- **Tick:** filters expired + already-sent suppression keys, ranks by urgency, max 20 actions, opted-out merchants skipped.
- **Reply state machine:** auto-reply (pattern or verbatim repeat) → one flag → wait 24h → end · hostile/opt-out → one apology then end + merchant suppressed · off-topic (GST etc.) → polite decline + redirect · "later/busy" → wait 4h · commitment ("ok let's do it", "haan", "go ahead") → immediate action mode, no re-qualifying · questions → grounded answer (LLM if a key is set). Anti-repetition guard on every send.
- **Optional LLM:** set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (and `LLM_MODEL`) to have free-form merchant questions answered by an LLM at temperature 0, constrained to context facts. Fails safe to rules within 12s.

## Run locally
    python bot.py            # PORT=8080 by default
    python test_local.py     # pushes the seed dataset, ticks all 25 triggers, replays auto-reply / intent / GST / hostile

## Deploy (pick one)
- **Render:** push this folder to GitHub → New → Blueprint → select repo (uses `render.yaml`). Use an always-on plan: state is in memory and the free tier sleeps.
- **Railway / Fly / any Docker host:** `docker build -t vera-bot . && docker run -p 8080:8080 vera-bot`.
- **Quick public URL from your laptop:** `python bot.py` then `ngrok http 8080`.

Submit `https://<your-host>` as the bot URL. Don't restart the service during the test window; in-memory contexts would be lost.

## Trade-offs / what would help
Rules give zero hallucination and <5 ms latency, at the cost of less varied wording than a tuned LLM. Most useful extra context: real appointment slots per merchant, per-customer batch/dispense history (for recall counts), and the merchant's own service price list.
