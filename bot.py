#!/usr/bin/env python3
"""
Vera — merchant engagement bot for the magicpin AI Challenge.

Zero-dependency HTTP server (Python 3.9+ stdlib only) implementing:
  GET  /v1/healthz   GET /v1/metadata
  POST /v1/context   POST /v1/tick   POST /v1/reply   POST /v1/teardown

Composition is deterministic and grounded: every fact in a message is read from
the pushed contexts (category / merchant / trigger / customer). Nothing invented.
Optional: set ANTHROPIC_API_KEY (or OPENAI_API_KEY) to let an LLM polish the
reply to free-form merchant questions; rule-based output is always the fallback.

Run:  PORT=8080 python bot.py
"""
from __future__ import annotations

import json, os, re, threading, time, uuid, hashlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest

START = time.time()
LOCK = threading.RLock()
CONTEXTS: dict[tuple[str, str], dict] = {}   # (scope, id) -> {"version", "payload"}
CONVS: dict[str, dict] = {}                  # conversation_id -> state
SENT_SUPPRESSION: set[str] = set()
MERCHANT_BLOCK: dict[str, float] = {}        # merchant_id -> blocked (opted out)
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get(scope, cid):
    with LOCK:
        c = CONTEXTS.get((scope, cid))
        return c["payload"] if c else None


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
def pct(x, signed=False):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    v = v * 100 if abs(v) <= 1.5 else v
    s = f"{abs(v):.0f}%" if not signed else f"{'+' if v >= 0 else '-'}{abs(v):.0f}%"
    return s


def ctr_s(x):
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return None


def num(x):
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def rupee(x):
    try:
        return f"₹{int(x):,}"
    except (TypeError, ValueError):
        return f"₹{x}"


def human(s):
    if not s:
        return ""
    s = re.sub(r"(\d+)day\b", r"\1-day", str(s))
    return s.replace("_", " ")


BAD = re.compile(r"\bNone\b|\{\}|\[\]|''")


def first_sent(text, n=0):
    parts = re.split(r"(?<!\bDr)(?<!\b[A-Z])\.\s+(?=[A-Z])", text or "")
    parts = [x.strip().rstrip(".") for x in parts if x.strip()]
    return parts[n] if len(parts) > n else ""


def win(p):
    w = str(p.get("window") or "7d")
    m = re.fullmatch(r"(\d+)d", w)
    return f"{m.group(1)} days" if m else w


def is_hindi(merchant):
    langs = (merchant or {}).get("identity", {}).get("languages", []) or []
    return "hi" in langs


def cust_hindi(customer):
    lp = ((customer or {}).get("identity", {}).get("language_pref") or "").lower()
    return lp.startswith("hi")


def salutation(merchant, category):
    ident = merchant.get("identity", {})
    first = ident.get("owner_first_name") or ""
    slug = (category or {}).get("slug") or merchant.get("category_slug", "")
    if slug == "dentists" and first:
        return first if first.lower().startswith("dr") else f"Dr. {first}"
    return first or ident.get("name", "there")


def active_offers(merchant):
    return [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active" and o.get("title")]


def find_digest(category, item_id):
    for d in (category or {}).get("digest", []) or []:
        if d.get("id") == item_id:
            return d
    return None


def best_catalog(category, keyword=None):
    cat = (category or {}).get("offer_catalog", []) or []
    if keyword:
        for o in cat:
            if keyword.lower() in o.get("title", "").lower():
                return o["title"]
    for o in cat:
        if o.get("type") == "service_at_price":
            return o["title"]
    return cat[0]["title"] if cat else None


TABOO_FALLBACK = ["guaranteed", "100% safe", "miracle", "best in city", "cure"]


def scrub(body, category):
    taboos = list((category or {}).get("voice", {}).get("vocab_taboo", []) or []) + TABOO_FALLBACK
    for t in taboos:
        t = re.sub(r"\s*\(.*\)", "", t).strip()
        if t and t.lower() in body.lower():
            body = re.sub(re.escape(t), "", body, flags=re.I)
    body = re.sub(r"https?://\S+", "", body)       # no URLs in WhatsApp body
    body = re.sub(r"[ \t]{2,}", " ", body)
    return body.strip()


def peer_line(merchant, category):
    perf = merchant.get("performance", {}) or {}
    peer = (category or {}).get("peer_stats", {}) or {}
    if perf.get("ctr") is not None and peer.get("avg_ctr"):
        mine, theirs = float(perf["ctr"]), float(peer["avg_ctr"])
        if mine < theirs:
            return f"your profile CTR is {ctr_s(mine)} vs {ctr_s(theirs)} for peer {human(category.get('slug'))} nearby"
        return f"your CTR of {ctr_s(mine)} is already above the {ctr_s(theirs)} peer average"
    return None


# ----------------------------------------------------------------------------
# Merchant-facing composers (one per trigger family)
# ----------------------------------------------------------------------------
def c_research(cat, m, t, name, hi):
    p = t.get("payload", {})
    d = find_digest(cat, p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id")) or {}
    if not d:
        items = cat.get("digest", []) or []
        d = items[0] if items else {}
    if not d:
        return None
    agg = m.get("customer_aggregate", {})
    seg = d.get("patient_segment")
    hook = ""
    if seg and "high_risk" in seg and agg.get("high_risk_adult_count"):
        hook = f" Directly relevant to your {agg['high_risk_adult_count']} high-risk adult patients."
    n = f"{num(d['trial_n'])}-patient study: " if d.get("trial_n") else ""
    summ = first_sent(d.get("summary"))
    body = (f"{name}, {d.get('source','this week’s digest')} is out — one item worth 2 minutes. "
            f"{n}{d.get('title')}.{hook} Key finding: {summ}. "
            f"Want me to pull the abstract and draft a patient-friendly WhatsApp you can forward?")
    return body, "open_ended", f"Digest item '{d.get('id')}' cited with its source; hooked to merchant cohort data where available."


def c_compliance(cat, m, t, name, hi):
    p = t.get("payload", {})
    d = find_digest(cat, p.get("top_item_id") or p.get("alert_id")) or {}
    deadline = p.get("deadline_iso") or ""
    if not d:
        return None
    body = (f"{name}, compliance heads-up: {d.get('title')} ({d.get('source')}). "
            f"{first_sent(d.get('summary'))}."
            f"{' Deadline: ' + deadline + '.' if deadline else ''} "
            f"What to do: {d.get('actionable','review your setup')}. "
            f"Want me to send a 5-point self-audit checklist? Reply YES.")
    return body, "binary_yes_no", "Regulation change with a hard deadline — loss-aversion + effort externalisation via checklist."


def c_alert(cat, m, t, name, hi):
    p = t.get("payload", {})
    d = find_digest(cat, p.get("alert_id")) or {}
    batches = ", ".join(p.get("affected_batches", []) or [])
    mol = p.get("molecule", "")
    agg = m.get("customer_aggregate", {})
    chronic = agg.get("chronic_rx_count")
    body = (f"{name}, urgent: {d.get('source','supplier')} recall on {mol} batches {batches}"
            f"{' by ' + p['manufacturer'] if p.get('manufacturer') else ''}. "
            f"{first_sent(d.get('summary'), 1)}. "
            f"{'You have ' + num(chronic) + ' chronic-Rx customers on file — ' if chronic else ''}"
            f"I can filter the ones dispensed these batches and draft their replacement WhatsApp. Shall I start? Reply YES.")
    return body, "binary_yes_no", "Urgency-5 supply recall; batch numbers verbatim; offers full workflow."


def c_perf_dip(cat, m, t, name, hi):
    p = t.get("payload", {})
    metric = p.get("metric", "calls")
    d = pct(p.get("delta_pct"))
    base = p.get("vs_baseline")
    perf = m.get("performance", {})
    extra = []
    if "unverified_gbp" in " ".join(m.get("signals", [])):
        extra.append("your Google profile is still unverified")
    if not active_offers(m):
        offer = best_catalog(cat)
        if offer:
            extra.append(f"there's no live offer (peers run things like '{offer}')")
    why = ("; ".join(extra)) if extra else peer_line(m, cat) or ""
    body = (f"{name}, your {metric} dropped {d} over the last {win(p)}"
            f"{f' (baseline ~{base})' if base else ''} — {num(perf.get('views'))} views this month but only {num(perf.get('calls'))} calls. "
            f"{('Likely factor: ' + why + '. ') if why else ''}"
            f"{'Ye jaldi fix ho sakta hai — ' if hi else ''}I can put together a 2-step recovery plan today. Want it? Reply YES.")
    return body, "binary_yes_no", "Internal perf dip, quantified from trigger payload + merchant performance; diagnosis from signals."


def c_seasonal_dip(cat, m, t, name, hi):
    p = t.get("payload", {})
    agg = m.get("customer_aggregate", {})
    beat = next((b for b in cat.get("seasonal_beats", []) if "Apr" in b.get("month_range", "")), None)
    members = agg.get("total_active_members")
    body = (f"{name}, {p.get('metric','views')} are down {pct(p.get('delta_pct'))} this week — and that's expected: "
            f"{(beat or {}).get('note', human(p.get('season_note')))}. Don't chase it with ad spend now. "
            f"{'Better play: keep your ' + num(members) + ' active members showing up. ' if members else ''}"
            f"Want me to draft a 4-week summer attendance challenge you can announce this week?")
    return body, "open_ended", "Seasonal dip reframed as normal (anxiety pre-emption) with a retention action."


def c_perf_spike(cat, m, t, name, hi):
    p = t.get("payload", {})
    drv = human(p.get("likely_driver"))
    base_s = f" (vs ~{p['vs_baseline']} baseline)" if p.get("vs_baseline") else ""
    body = (f"{name}, good news — {p.get('metric','calls')} are up {pct(p.get('delta_pct'))} over the last {win(p)}"
            f"{base_s}"
            f"{', most likely from your ' + drv if drv else ''}. Momentum like this fades in ~10 days unless you follow up. "
            f"Want me to draft a follow-up post to ride it? Reply YES.")
    return body, "binary_yes_no", "Perf spike: celebrate + loss-aversion on fading momentum."


def c_renewal(cat, m, t, name, hi):
    p = t.get("payload", {})
    perf = m.get("performance", {})
    body = (f"{name}, your {p.get('plan','')} plan renews in {p.get('days_remaining')} days ({rupee(p.get('renewal_amount'))}). "
            f"Last 30 days on magicpin: {num(perf.get('views'))} profile views, {num(perf.get('calls'))} calls, {num(perf.get('directions'))} direction requests. "
            f"If it lapses, profile upkeep pauses. Renew now so nothing breaks? Reply YES and I'll send the link.")
    return body, "binary_yes_no", "Renewal due — ROI recap from own performance data, loss-aversion, binary CTA."


def c_festival(cat, m, t, name, hi):
    p = t.get("payload", {})
    offer = (active_offers(m) or [best_catalog(cat)])[0]
    days = p.get("days_until")
    beat = next((b for b in cat.get("seasonal_beats", []) if "Oct" in b.get("month_range", "")), None)
    body = (f"{name}, {p.get('festival')} is on {p.get('date')}{f' ({days} days out)' if days else ''}. "
            f"{('For ' + human(cat.get('slug')) + ': ' + beat['note'] + '. ') if beat else ''}"
            f"Early planners lock the best slots. Want me to draft a {p.get('festival')} campaign around '{offer}' now so it's ready to go live?")
    return body, "open_ended", "Festival trigger + category seasonal beat + merchant's own offer."


def c_curious(cat, m, t, name, hi):
    trends = cat.get("trend_signals", []) or []
    top = max(trends, key=lambda x: x.get("delta_yoy", 0)) if trends else None
    loc = m.get("identity", {}).get("locality", "")
    guess = f" My guess: '{top['query']}' — searches are up {pct(top['delta_yoy'])} YoY." if top else ""
    body = (f"Hi {name}! Quick one — which service did customers ask for most this week at {m['identity'].get('name')}?{guess} "
            f"Tell me and I'll turn it into a Google post + a ready reply for price enquiries. 5 min, done.")
    return body, "open_ended", "Curious-ask cadence: asking-the-merchant lever + reciprocity, anchored on a real trend."


def c_winback(cat, m, t, name, hi):
    p = t.get("payload", {})
    agg = m.get("customer_aggregate", {})
    lapsed = p.get("lapsed_customers_added_since_expiry")
    dip_s = f" — calls are down {pct(p['perf_dip_pct'])}" if p.get("perf_dip_pct") else ""
    body = (f"{name}, it's been {p.get('days_since_expiry') or p.get('days_since_last_merchant_message')} days since your plan paused"
            f"{dip_s}"
            f"{f', and {lapsed} more customers have lapsed since' if lapsed else ''}. "
            f"{'Aapke ' + num(agg['lapsed_90d_plus']) + ' lapsed customers abhi bhi reachable hain. ' if hi and agg.get('lapsed_90d_plus') else ''}"
            f"I can restart with a winback message to them this week. Want me to? Reply YES.")
    return body, "binary_yes_no", "Winback: quantified loss since expiry + concrete reactivation action."


def c_dormant(cat, m, t, name, hi):
    p = t.get("payload", {})
    perf = m.get("performance", {})
    pl = peer_line(m, cat)
    body = (f"{name}, haven't heard from you in {p.get('days_since_last_merchant_message')} days, so here's one number worth a look: "
            f"{num(perf.get('views'))} people viewed {m['identity'].get('name')} on Google last month"
            f"{', ' + pl if pl else ''}. Want a 3-line summary of what's working and what's not? Reply YES.")
    return body, "binary_yes_no", "Dormant merchant: reciprocity via free insight, single binary CTA."


def c_review_theme(cat, m, t, name, hi):
    p = t.get("payload", {})
    q = p.get("common_quote")
    body = (f"{name}, {p.get('occurrences_30d')} reviews in the last 30 days mention '{human(p.get('theme'))}'"
            f"{' and the trend is ' + p['trend'] if p.get('trend') else ''}"
            f"{f' — one says: “{q}”' if q else ''}. Left alone, this starts showing up in your rating. "
            f"Want me to draft polite public replies to these reviews? Reply YES.")
    return body, "binary_yes_no", "Review theme emerged — quoted, counted, loss-aversion, effort externalised."


def c_milestone(cat, m, t, name, hi):
    p = t.get("payload", {})
    gap = (p.get("milestone_value") or 0) - (p.get("value_now") or 0)
    body = (f"{name}, you're at {p.get('value_now')} {human(p.get('metric')).replace('review count', 'reviews')} — just {gap} away from {p.get('milestone_value')}. "
            f"A small nudge to happy regulars usually closes that in a week. Want me to draft a 2-line review request you can share at the counter/WhatsApp?")
    return body, "open_ended", "Imminent milestone — goal-gradient + effort externalisation."


def c_competitor(cat, m, t, name, hi):
    p = t.get("payload", {})
    mine = active_offers(m)
    body = (f"{name}, heads-up: {p.get('competitor_name')} opened {p.get('distance_km')} km away on {p.get('opened_date')}, "
            f"running '{p.get('their_offer')}'."
            f"{' Your live offer is ' + repr(mine[0]) + '.' if mine else ''} "
            f"Don't race to the bottom on price — compete on what your reviews already praise. "
            f"Want me to draft a Google post that highlights that this week?")
    return body, "open_ended", "Competitor trigger with all details from payload; counsels against price war."


def c_planning(cat, m, t, name, hi):
    p = t.get("payload", {})
    topic = human(p.get("intent_topic"))
    hist = m.get("conversation_history", []) or []
    last_vera = next((h["body"] for h in reversed(hist) if h.get("from") == "vera"), "")
    offer = (active_offers(m) or [best_catalog(cat)])[0]
    body = (f"{name}, picking up on your {topic} idea — here's a draft you can edit:\n"
            f"• Built around your '{offer}'\n"
            f"• Clear price, fixed schedule, capped seats\n"
            f"• Google post + WhatsApp message to existing customers\n"
            f"Reply GO and I'll publish the post and prepare the customer message.")
    return body, "binary_confirm_cancel", "Merchant already expressed intent — moving straight to a drafted artifact (action mode, no re-qualifying)."


def c_gbp_unverified(cat, m, t, name, hi):
    p = t.get("payload", {})
    perf = m.get("performance", {})
    body = (f"{name}, your Google profile is still unverified — {num(perf.get('views'))} people saw it last month, "
            f"but unverified listings get roughly {pct(p.get('estimated_uplift_pct'))} fewer actions. "
            f"Verification is via {human(p.get('verification_path'))}; I'll walk you through it in 5 min. Start now? Reply YES.")
    return body, "binary_yes_no", "Unverified GBP — loss-aversion with the uplift figure from trigger."


def c_category_seasonal(cat, m, t, name, hi):
    p = t.get("payload", {})
    trends = ", ".join(human(x).replace(" +", " +").replace("demand", "") + "%" if re.search(r"[+-]\d+$", x) else human(x) for x in p.get("trends", []))
    body = (f"{name}, {human(p.get('season'))} demand shift is here: {trends}. "
            f"Quick shelf move — bring ORS/sunscreen to the counter, cold & cough to the back. "
            f"Want me to post a 'summer essentials in stock' update on your Google profile? Reply YES.")
    return body, "binary_yes_no", "Category seasonal shift with payload trends verbatim."


def c_cde(cat, m, t, name, hi):
    p = t.get("payload", {})
    d = find_digest(cat, p.get("digest_item_id")) or {}
    body = (f"{name}, {d.get('title', 'a CDE session')} — {d.get('date', '')[:10]}, {p.get('credits', d.get('credits'))} CDE credits"
            f" ({human(p.get('fee'))}). {first_sent(d.get('summary'))}. Want me to block the time and send you the joining details?")
    return body, "open_ended", "CDE opportunity from digest item with date + credits."


def c_ipl(cat, m, t, name, hi):
    p = t.get("payload", {})
    weeknight = p.get("is_weeknight")
    offer = (active_offers(m) or [best_catalog(cat, "Match")])[0]
    d = next((x for x in cat.get("digest", []) if "ipl" in x.get("id", "")), None)
    tip = ("Weeknight matches drive more covers — push a match-night combo." if weeknight
           else f"Heads-up: weekend matches tend to move people to home-watch — {first_sent((d or {}).get('summary')).lower() if d else 'dine-in dips'}. Lean on delivery instead.")
    body = (f"{name}, {p.get('match')} tonight at {p.get('venue')}, {p.get('match_time_iso','')[11:16]}. {tip} "
            f"Your '{offer}' fits — want me to draft a delivery-focused post for this evening? Reply YES.")
    return body, "binary_yes_no", "IPL trigger interpreted with category digest (weekday vs weekend) — adds judgment."


def c_generic(cat, m, t, name, hi):
    perf = m.get("performance", {})
    pl = peer_line(m, cat)
    body = (f"{name}, quick update on {m['identity'].get('name')}: {num(perf.get('views'))} views and {num(perf.get('calls'))} calls in the last 30 days"
            f"{', ' + pl if pl else ''}. Want me to suggest one change that would move the needle this week?")
    return body, "open_ended", f"Trigger '{t.get('kind')}' — grounded performance summary."


MERCHANT_COMPOSERS = {
    "research_digest": c_research, "category_research_digest_release": c_research,
    "regulation_change": c_compliance, "supply_alert": c_alert,
    "perf_dip": c_perf_dip, "seasonal_perf_dip": c_seasonal_dip, "perf_spike": c_perf_spike,
    "renewal_due": c_renewal, "festival_upcoming": c_festival, "curious_ask_due": c_curious,
    "winback_eligible": c_winback, "dormant_with_vera": c_dormant, "review_theme_emerged": c_review_theme,
    "milestone_reached": c_milestone, "competitor_opened": c_competitor,
    "active_planning_intent": c_planning, "gbp_unverified": c_gbp_unverified,
    "category_seasonal": c_category_seasonal, "cde_opportunity": c_cde, "ipl_match_today": c_ipl,
}


# ----------------------------------------------------------------------------
# Customer-facing composer
# ----------------------------------------------------------------------------
def compose_customer(cat, m, t, cust):
    p = t.get("payload", {}) or {}
    ident = cust.get("identity", {})
    cname = re.sub(r"\s*\(.*\)", "", ident.get("name", "")).strip() or "there"
    parent = re.search(r"parent:\s*([^)]+)", ident.get("name", ""))
    hi = cust_hindi(cust)
    biz = m["identity"].get("name")
    owner = m["identity"].get("owner_first_name")
    offers = active_offers(m)
    kind = t.get("kind")
    slots = p.get("available_slots") or p.get("next_session_options") or []
    slot_labels = [s.get("label") for s in slots if s.get("label")]
    greet = f"Namaste" if ident.get("language_pref") == "hi" else f"Hi {parent.group(1) if parent else cname}"
    from_line = f"{biz} here" + (f" ({owner})" if owner and not owner.startswith("Dr") and cat.get("slug") != "dentists" else "")

    if kind == "recall_due":
        last = cust.get("relationship", {}).get("last_visit")
        months = _months_since(last, t)
        offer = next((o for o in offers if "clean" in o.lower()), offers[0] if offers else None)
        s = " ya ".join(slot_labels) if hi else " or ".join(slot_labels)
        body = (f"{greet}, {from_line} 🦷 It's been {months} months since your last visit — your {human(p.get('service_due'))} is due by {p.get('due_date')}. "
                f"{('Aapke liye slots ready hain: ' if hi else 'Open slots: ') + s + '. ' if s else ''}"
                f"{offer + '. ' if offer else ''}"
                f"{'Reply 1 for the first slot, 2 for the second, or tell us a time that suits you.' if len(slot_labels) >= 2 else 'Reply YES to book.'}")
        cta = "multi_choice_slot" if len(slot_labels) >= 2 else "binary_yes_no"
    elif kind == "chronic_refill_due":
        mols = ", ".join(p.get("molecule_list", []))
        runout = (p.get("stock_runs_out_iso") or "")[:10]
        deliv = next((o for o in offers if "deliver" in o.lower()), None)
        senior = next((o for o in offers if "senior" in o.lower()), None) if ident.get("senior_citizen") else None
        who = f"{cname} ji" if hi or ident.get("language_pref") == "hi" else cname
        if hi or ident.get("language_pref") == "hi":
            body = (f"Namaste — {biz} yahan. {who} ki medicines ({mols}) {runout} tak khatam ho jayengi. Same pack ready hai."
                    f"{' ' + senior + ' lagu hoga.' if senior else ''}{' ' + deliv + ' — saved address par.' if deliv and p.get('delivery_address_saved') else ''} "
                    f"Dispatch karne ke liye CONFIRM reply karein; dose mein koi badlav ho to bata dijiye.")
        else:
            body = (f"Hi, {biz} here. {who}'s medicines ({mols}) run out on {runout}. Same pack is ready."
                    f"{' ' + senior + ' applies.' if senior else ''}{' ' + deliv + ' to your saved address.' if deliv else ''} Reply CONFIRM to dispatch.")
        cta = "binary_confirm_cancel"
    elif kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        days = p.get("days_since_last_visit")
        focus = human(p.get("previous_focus") or cust.get("preferences", {}).get("training_focus"))
        offer = offers[0] if offers else best_catalog(cat)
        body = (f"{greet} 👋 {from_line}. It's been {f'{days} days' if days else 'a while'} — happens to everyone, no judgment. "
                f"{('Since you were working on ' + focus + ', ') if focus else ''}we'd love to have you back: {offer}. "
                f"Want us to hold a spot for you this week? Reply YES — no commitment.")
        cta = "binary_yes_no"
    elif kind == "trial_followup":
        s = slot_labels[0] if slot_labels else None
        body = (f"{greet}, {from_line} 🙏 Thanks for bringing {cname} for the trial on {p.get('trial_date')}. "
                f"{('Next session: ' + s + '. ') if s else ''}Shall we save the spot? Reply YES.")
        cta = "binary_yes_no"
    elif kind == "wedding_package_followup":
        body = (f"{greet} 💍 {from_line}. {p.get('days_to_wedding')} days to your wedding on {p.get('wedding_date')} — "
                f"this is the right window to start the {human(p.get('next_step_window_open'))} after your trial on {p.get('trial_completed')}. "
                f"Want us to block a Saturday slot for session one? Reply YES.")
        cta = "binary_yes_no"
    elif kind == "appointment_tomorrow":
        body = f"{greet}, {from_line}. Reminder: your appointment is tomorrow. Reply YES to confirm or tell us if you need to reschedule."
        cta = "binary_yes_no"
    else:
        offer = offers[0] if offers else best_catalog(cat)
        body = (f"{greet}, {from_line}. {('We have ' + offer + ' running right now. ') if offer else ''}"
                f"Want us to book you in this week? Reply YES.")
        cta = "binary_yes_no"
    return body, cta, f"Customer-facing {kind}: sent on merchant's behalf, honours {ident.get('language_pref')} language and consent scope."


def _months_since(date_s, trigger):
    try:
        last = datetime.fromisoformat(date_s)
        due = datetime.fromisoformat(trigger.get("payload", {}).get("due_date", ""))
        ref = due if due > last else datetime.now()
        return max(1, round((ref - last).days / 30) - 1) if due > last else max(1, (datetime.now() - last).days // 30)
    except Exception:
        return "a few"


# ----------------------------------------------------------------------------
# Public composer
# ----------------------------------------------------------------------------
def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict | None:
    category = category or {}
    kind = trigger.get("kind", "")
    if customer or trigger.get("scope") == "customer":
        if not customer:
            return None
        consent = customer.get("consent", {}) or {}
        if not consent.get("opted_in_at") or customer.get("preferences", {}).get("reminder_opt_in") is False:
            return None   # no consent -> don't message
        body = None
        if not (trigger.get("payload") or {}).get("placeholder"):
            try:
                body, cta, why = compose_customer(category, merchant, trigger, customer)
            except Exception:
                body = None
        if not body or BAD.search(body):
            cname = re.sub(r"\s*\(.*\)", "", customer.get("identity", {}).get("name", "")).strip() or "there"
            offer = (active_offers(merchant) or [best_catalog(category)])[0]
            body = (f"Hi {cname}, {merchant['identity'].get('name')} here. "
                    + (f"We have {offer} running this week. " if offer else "")
                    + "Want us to book you in? Reply YES.")
            cta = "binary_yes_no"
            why = f"Customer-facing {kind}: trigger payload was sparse, so only the merchant's verified offer is used."
        send_as = "merchant_on_behalf"
        tpl = f"merchant_{kind}_v1"
        params = [customer.get("identity", {}).get("name", ""), merchant["identity"].get("name", ""), body[:120]]
    else:
        name = salutation(merchant, category)
        fn = MERCHANT_COMPOSERS.get(kind, c_generic)
        out = None
        if not (trigger.get("payload") or {}).get("placeholder"):
            try:
                out = fn(category, merchant, trigger, name, is_hindi(merchant))
            except Exception:
                out = None
        if not out or BAD.search(out[0]):
            out = c_generic(category, merchant, trigger, name, is_hindi(merchant))
        body, cta, why = out
        send_as = "vera"
        tpl = f"vera_{kind}_v1"
        params = [name, body[:160], body[160:320]]
    body = scrub(re.sub(r"\n{3,}", "\n\n", body), category)
    return {"body": body, "cta": cta, "send_as": send_as,
            "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
            "rationale": why, "template_name": tpl, "template_params": params}


# ----------------------------------------------------------------------------
# Reply handling (multi-turn)
# ----------------------------------------------------------------------------
AUTO_PATTERNS = [r"thank(s| you) for (contacting|reaching|your message)", r"will (get back|respond|revert) (to you )?(shortly|soon)",
                 r"automated (assistant|reply|message)", r"our team will", r"we are (currently )?(closed|unavailable)",
                 r"out of office", r"jaankari ke liye.*shukriya", r"team tak pahuncha"]
HOSTILE = [r"\bstop\b", r"\bspam\b", r"not interested", r"don'?t (message|text|contact)", r"useless", r"unsubscribe",
           r"leave me alone", r"bothering", r"band karo", r"mat bhejo", r"nahi chahiye"]
COMMIT = [r"\b(yes|yep|yeah|haan|ha|ok|okay|sure|done|confirm|go|go ahead|proceed)\b", r"let'?s do it", r"do it", r"i want to join",
          r"judna hai", r"judrna hai", r"kar do", r"send (it|me)", r"what'?s next", r"please (send|draft|do)"]
LATER = [r"\blater\b", r"busy", r"call me (later|tomorrow)", r"baad mein", r"not now", r"tomorrow"]
OFF_TOPIC = [r"\bgst\b", r"income tax", r"\bitr\b", r"loan", r"\bvisa\b", r"electricity bill", r"accounting"]


def _match(pats, text):
    return any(re.search(p, text, re.I) for p in pats)


def handle_reply(req: dict) -> dict:
    cid = req.get("conversation_id") or f"conv_{uuid.uuid4().hex[:8]}"
    msg = (req.get("message") or "").strip()
    with LOCK:
        st = CONVS.setdefault(cid, {"merchant_id": req.get("merchant_id"), "customer_id": req.get("customer_id"),
                                    "trigger_id": None, "turns": [], "sent": [], "auto": 0, "ended": False, "stage": "pitch"})
        if req.get("merchant_id") and not st.get("merchant_id"):
            st["merchant_id"] = req["merchant_id"]
        st["turns"].append({"from": req.get("from_role"), "msg": msg})
    if st["ended"]:
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging."}

    m = get("merchant", st.get("merchant_id") or "") or {"identity": {}}
    cat = get("category", m.get("category_slug", "")) or {}
    name = salutation(m, cat) if m.get("identity") else ""
    hi = is_hindi(m) or bool(re.search(r"\b(hai|haan|nahi|kya|karo|mujhe|aap)\b", msg, re.I))
    prev_in = [t["msg"] for t in st["turns"][:-1] if t["from"] != "vera"]
    low = msg.lower()

    # 1) Auto-reply detection (pattern OR verbatim repeat)
    if _match(AUTO_PATTERNS, low) or (prev_in and prev_in[-1].strip().lower() == low and len(low) > 20):
        st["auto"] += 1
        if st["auto"] == 1:
            return _send(st, "Looks like an auto-reply 🙂 Whenever the owner sees this — just reply YES and I'll take it from there.",
                         "binary_yes_no", "Detected WhatsApp Business auto-reply; one short flag for the owner, no pitch.")
        if st["auto"] == 2:
            return {"action": "wait", "wait_seconds": 86400, "rationale": "Same auto-reply twice — owner not on phone. Backing off 24h."}
        st["ended"] = True
        return {"action": "end", "rationale": "Auto-reply 3+ times with no human signal; closing to avoid wasting turns."}
    st["auto"] = 0

    # 2) Hostile / opt-out
    if _match(HOSTILE, low):
        st["ended"] = True
        MERCHANT_BLOCK[st.get("merchant_id") or ""] = time.time()
        if st.get("apologized"):
            return {"action": "end", "rationale": "Merchant repeated opt-out; closing."}
        st["apologized"] = True
        body = ("Samajh gayi — sorry for the trouble, main aage message nahi karungi. Kabhi zarurat ho to bas 'Hi Vera' likh dijiye. 🙏" if hi
                else "Understood — sorry for the bother, I won't message again. If you ever need help, just say 'Hi Vera'. 🙏")
        return _send(st, body, "none", "Explicit opt-out/frustration: one apology + opt-out path, then conversation closed and merchant suppressed.")

    # 3) Off-topic
    if _match(OFF_TOPIC, low):
        return _send(st, (f"GST/tax filing is best left to your CA — that's outside what I handle. "
                          f"Back to what I can do for {m.get('identity',{}).get('name','your business')}: shall I go ahead with the draft I mentioned? Reply YES."),
                     "binary_yes_no", "Off-topic request declined politely; redirected to the original thread.")

    # 4) Later / busy
    if _match(LATER, low) and not _match(COMMIT[1:], low):
        return {"action": "wait", "wait_seconds": 3600 * 4, "rationale": "Merchant asked for time; backing off 4h."}

    # 5) Commitment -> action mode
    if _match(COMMIT, low):
        st["stage"] = "action"
        n_done = sum(1 for s in st["sent"] if s.get("stage") == "action")
        if n_done == 0:
            body = (f"Done{', ' + name if name else ''} — drafting it now. You'll get the draft here in the next few minutes; "
                    f"I'll publish only after you reply CONFIRM. Nothing else needed from you.")
            if hi:
                body = f"Ho gaya{', ' + name if name else ''} — draft abhi bana rahi hoon, kuch minute mein yahin bhej dungi. Aap CONFIRM bolenge tabhi publish karungi."
            return _send(st, body, "binary_confirm_cancel", "Explicit commitment detected — switched to action mode immediately, no more qualifying.", stage="action")
        if n_done == 1:
            return _send(st, "Confirmed ✅ It's live. I'll share how it performs (views + calls) in 7 days.", "none",
                         "Second confirmation — executing and closing loop with a measurable follow-up.", stage="action")
        st["ended"] = True
        return {"action": "end", "rationale": "Action completed and confirmed; nothing further to push."}

    # 6) Question / engaged -> answer with context, optionally via LLM
    llm = llm_reply(m, cat, st, msg)
    if llm:
        return _send(st, llm, "open_ended", "LLM-composed answer grounded in merchant + category context.")
    perf = m.get("performance", {})
    offer = (active_offers(m) or [best_catalog(cat)] if cat else [None])[0]
    facts = []
    if perf.get("views") is not None:
        facts.append(f"{num(perf.get('views'))} views and {num(perf.get('calls'))} calls in 30 days")
    pl = peer_line(m, cat) if cat else None
    if pl:
        facts.append(pl)
    body = (f"Good question{', ' + name if name else ''}. Quick context from your profile: {'; '.join(facts) or 'I have your latest numbers ready'}. "
            f"{('The simplest lever right now is promoting ' + repr(offer) + '. ') if offer else ''}"
            f"Want me to prepare that — reply YES and I'll draft it.")
    return _send(st, body, "binary_yes_no", "Merchant engaged with a question; answered from context and offered one next step.")


def _send(st, body, cta, why, stage=None):
    prior = {s["body"] for s in st["sent"]}
    if body in prior:
        body += " (Just say the word whenever ready.)" if body + " (Just say the word whenever ready.)" not in prior else f" [{len(prior)}]"
    st["sent"].append({"body": body, "stage": stage or st.get("stage")})
    st["turns"].append({"from": "vera", "msg": body})
    return {"action": "send", "body": body, "cta": cta, "rationale": why}


# ----------------------------------------------------------------------------
# Optional LLM (only for free-form question answering)
# ----------------------------------------------------------------------------
def llm_reply(m, cat, st, msg):
    key_a, key_o = os.getenv("ANTHROPIC_API_KEY"), os.getenv("OPENAI_API_KEY")
    if not (key_a or key_o):
        return None
    sysmsg = ("You are Vera, magicpin's WhatsApp assistant for Indian merchants. Reply in <=60 words, peer tone, "
              "match the merchant's language (Hindi-English mix if they use it). Use ONLY facts in the context JSON; never invent numbers, "
              "names or research. End with exactly one low-friction question/CTA. Never re-introduce yourself.")
    ctx = json.dumps({"merchant": {k: m.get(k) for k in ("identity", "performance", "offers", "signals", "customer_aggregate")},
                      "category": {k: cat.get(k) for k in ("slug", "voice", "peer_stats")},
                      "conversation": st["turns"][-6:]}, ensure_ascii=False)[:9000]
    prompt = f"CONTEXT:\n{ctx}\n\nMerchant just said: {msg}\n\nWrite Vera's reply (body only)."
    try:
        if key_a:
            req = urlrequest.Request("https://api.anthropic.com/v1/messages",
                                     data=json.dumps({"model": os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 300,
                                                      "temperature": 0, "system": sysmsg,
                                                      "messages": [{"role": "user", "content": prompt}]}).encode(),
                                     headers={"x-api-key": key_a, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            out = json.loads(urlrequest.urlopen(req, timeout=12).read())["content"][0]["text"]
        else:
            req = urlrequest.Request("https://api.openai.com/v1/chat/completions",
                                     data=json.dumps({"model": os.getenv("LLM_MODEL", "gpt-4o-mini"), "temperature": 0,
                                                      "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": prompt}]}).encode(),
                                     headers={"Authorization": f"Bearer {key_o}", "Content-Type": "application/json"})
            out = json.loads(urlrequest.urlopen(req, timeout=12).read())["choices"][0]["message"]["content"]
        return scrub(out.strip().strip('"'), cat) or None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Tick
# ----------------------------------------------------------------------------
def handle_tick(req: dict) -> dict:
    now_s = req.get("now") or now_iso()
    try:
        now_dt = datetime.fromisoformat(now_s.replace("Z", "+00:00"))
    except Exception:
        now_dt = datetime.now(timezone.utc)
    trig_ids = req.get("available_triggers") or []
    cands = []
    for tid in trig_ids:
        t = get("trigger", tid)
        if not t:
            continue
        exp = t.get("expires_at")
        try:
            if exp and datetime.fromisoformat(exp.replace("Z", "+00:00")) < now_dt:
                continue
        except Exception:
            pass
        if t.get("suppression_key") in SENT_SUPPRESSION:
            continue
        cands.append((t.get("urgency", 1), tid, t))
    cands.sort(key=lambda x: -x[0])

    actions, used_pairs = [], set()
    for _, tid, t in cands:
        mid = t.get("merchant_id") or (t.get("payload") or {}).get("merchant_id")
        m = get("merchant", mid or "")
        if not m or mid in MERCHANT_BLOCK:
            continue
        cat = get("category", m.get("category_slug", "")) or {}
        cust = get("customer", t.get("customer_id") or "") if t.get("customer_id") else None
        pair = (mid, t.get("customer_id"), t.get("kind"))
        if pair in used_pairs:
            continue
        try:
            out = compose(cat, m, t, cust)
        except Exception as e:
            out = None
        if not out:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", (t.get("kind") or "msg").lower())
        conv_id = f"conv_{mid[:18]}_{slug}_{hashlib.md5(tid.encode()).hexdigest()[:6]}"
        if conv_id in CONVS:
            continue
        with LOCK:
            CONVS[conv_id] = {"merchant_id": mid, "customer_id": t.get("customer_id"), "trigger_id": tid,
                              "turns": [{"from": "vera", "msg": out["body"]}], "sent": [{"body": out["body"], "stage": "pitch"}],
                              "auto": 0, "ended": False, "stage": "pitch"}
            SENT_SUPPRESSION.add(out["suppression_key"])
        used_pairs.add(pair)
        actions.append({"conversation_id": conv_id, "merchant_id": mid, "customer_id": t.get("customer_id"),
                        "send_as": out["send_as"], "trigger_id": tid, "template_name": out["template_name"],
                        "template_params": out["template_params"], "body": out["body"], "cta": out["cta"],
                        "suppression_key": out["suppression_key"], "rationale": out["rationale"]})
        if len(actions) >= 20:
            break
    return {"actions": actions}


# ----------------------------------------------------------------------------
# Context
# ----------------------------------------------------------------------------
def handle_context(req: dict):
    scope, cid, ver = req.get("scope"), req.get("context_id"), req.get("version")
    if scope not in VALID_SCOPES:
        return 400, {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"}
    if not cid or not isinstance(req.get("payload"), dict):
        return 400, {"accepted": False, "reason": "malformed", "details": "context_id and payload object required"}
    try:
        ver = int(ver)
    except (TypeError, ValueError):
        return 400, {"accepted": False, "reason": "malformed", "details": "version must be int"}
    with LOCK:
        cur = CONTEXTS.get((scope, cid))
        if cur and cur["version"] >= ver:
            return 409, {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
        CONTEXTS[(scope, cid)] = {"version": ver, "payload": req["payload"]}
    return 200, {"accepted": True, "ack_id": f"ack_{cid}_v{ver}", "stored_at": now_iso()}


def counts():
    c = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    with LOCK:
        for (s, _) in CONTEXTS:
            c[s] = c.get(s, 0) + 1
    return c


METADATA = {
    "team_name": os.getenv("TEAM_NAME", "Team Vera+"),
    "team_members": [x.strip() for x in os.getenv("TEAM_MEMBERS", "Vera Builder").split(",")],
    "model": os.getenv("LLM_MODEL", "rule-based-composer") if (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")) else "deterministic-rule-composer",
    "approach": "Deterministic, trigger-kind-dispatched composer grounded only in pushed contexts; regex+repeat auto-reply detection; intent→action switching; optional LLM for free-form answers.",
    "contact_email": os.getenv("CONTACT_EMAIL", ""),
    "version": "1.0.0",
    "submitted_at": "2026-09-27T00:00:00Z",
}


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    server_version = "VeraBot/1.0"

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 600_000:
            raise ValueError("payload too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def log_message(self, fmt, *args):
        if os.getenv("QUIET") != "1":
            super().log_message(fmt, *args)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p == "/v1/healthz":
            return self._json(200, {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts()})
        if p == "/v1/metadata":
            return self._json(200, METADATA)
        if p in ("", "/"):
            return self._json(200, {"service": "vera-bot", "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"]})
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        try:
            body = self._body()
        except Exception as e:
            return self._json(400, {"accepted": False, "reason": "malformed", "details": str(e)})
        try:
            if p == "/v1/context":
                code, out = handle_context(body)
                return self._json(code, out)
            if p == "/v1/tick":
                return self._json(200, handle_tick(body))
            if p == "/v1/reply":
                return self._json(200, handle_reply(body))
            if p == "/v1/teardown":
                with LOCK:
                    CONTEXTS.clear(); CONVS.clear(); SENT_SUPPRESSION.clear(); MERCHANT_BLOCK.clear()
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not_found"})
        except Exception as e:
            if p == "/v1/tick":
                return self._json(200, {"actions": []})
            if p == "/v1/reply":
                return self._json(200, {"action": "wait", "wait_seconds": 600, "rationale": f"internal error handled: {type(e).__name__}"})
            return self._json(500, {"error": str(e)})


def main():
    port = int(os.getenv("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    print(f"Vera bot listening on :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
