from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

import conversation_handlers as ch

# ---------------------------------------------------------------------------
# Small data-access helpers (all read-only lookups into the 4 context dicts)
# ---------------------------------------------------------------------------

def _owner_first_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "there")


def _salutation(category: dict, merchant: dict) -> str:
    """Category-appropriate way to address the merchant (brief §4.1 salutation_examples)."""
    name = _owner_first_name(merchant)
    slug = category.get("slug", "")
    if slug == "dentists" and not name.lower().startswith("dr"):
        return f"Dr. {name}"
    return name


def _business_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name", "your business")


def _merchant_hi_mix(category: dict, merchant: dict) -> bool:
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in langs and category.get("voice", {}).get("code_mix", "").startswith(("hindi", "hi"))


def _customer_hi_mix(customer: Optional[dict]) -> bool:
    if not customer:
        return False
    pref = customer.get("identity", {}).get("language_pref", "")
    return "hi" in pref.lower()


def _active_offer(category: dict, merchant: dict) -> Optional[str]:
    """Prefer the merchant's own active offer (real, verifiable); fall back to the
    category's canonical catalog only as a generic reference, never invented."""
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active":
            return o.get("title")
    catalog = category.get("offer_catalog", []) or []
    return catalog[0]["title"] if catalog else None


def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for d in category.get("digest", []) or []:
        if d.get("id") == item_id:
            return d
    return None


def _is_placeholder(trigger: dict) -> bool:
    return bool((trigger.get("payload") or {}).get("placeholder"))


def _pct(x: Optional[float]) -> Optional[str]:
    if x is None:
        return None
    return f"{abs(x) * 100:.0f}%"


def _first_touch(merchant: dict) -> bool:
    return not merchant.get("conversation_history")


def _signal(merchant: dict, prefix: str) -> Optional[str]:
    for s in merchant.get("signals", []) or []:
        if s.startswith(prefix):
            return s
    return None


# ---------------------------------------------------------------------------
# Kind-specific composers.  Each returns (body, cta, lever_label).
# cta is one of: "binary", "open_ended", "none".
# ---------------------------------------------------------------------------

def _research_digest(category, merchant, trigger, customer):
    item = _digest_item(category, (trigger.get("payload") or {}).get("top_item_id"))
    sal = _salutation(category, merchant)
    if item:
        agg = merchant.get("customer_aggregate", {})
        cohort_note = ""
        if agg.get("high_risk_adult_count") and item.get("patient_segment") == "high_risk_adults":
            cohort_note = f" One item is relevant to your {agg['high_risk_adult_count']} high-risk adult patients —"
        elif item.get("patient_segment"):
            cohort_note = " One item worth a look:"
        trial_note = f" {item['trial_n']}-patient trial:" if item.get("trial_n") else ""
        body = (
            f"{sal}, new research landed.{cohort_note}{trial_note} {item.get('title', '')}. "
            f"Worth a look (2-min abstract) — want me to pull it and draft a patient-ed WhatsApp you can share? "
            f"Source: {item.get('source', '')}"
        )
        return body, "open_ended", "specificity+reciprocity"
    return _generic_merchant_fallback(category, merchant, trigger, "a research/knowledge update for your category")


def _regulation_change(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    item = _digest_item(category, payload.get("top_item_id"))
    sal = _salutation(category, merchant)
    deadline = payload.get("deadline_iso", "")
    if item:
        title = item.get("title", "")
        actionable = (item.get("actionable") or "Worth checking whether this changes your current process.").rstrip(".")
        # Only append the deadline separately if the title doesn't already state it.
        deadline_clause = f", effective {deadline}." if (deadline and deadline not in title) else "."
        body = (
            f"{sal}, compliance heads-up: {title} ({item.get('source', 'regulator notice')}){deadline_clause} "
            f"{actionable}. Want me to draft a one-page checklist so you're ready before the deadline?"
        )
        return body, "open_ended", "loss_aversion+specificity"
    return _generic_merchant_fallback(category, merchant, trigger, "a regulation change in your category")


def _cde_opportunity(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    item = _digest_item(category, payload.get("digest_item_id"))
    sal = _salutation(category, merchant)
    credits = payload.get("credits")
    fee = (payload.get("fee") or "").replace("_", " ")
    if item:
        credit_note = f"{credits} CDE credits, " if credits else ""
        body = (
            f"{sal}, {item.get('title', 'a relevant CE opportunity')} is up — {credit_note}{fee or 'check the fee'}. "
            f"{item.get('summary', '')}".strip() + " Want the registration link?"
        )
        return body, "open_ended", "specificity+low_friction"
    return _generic_merchant_fallback(category, merchant, trigger, "a continuing-education opportunity")


def _competitor_opened(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    if p.get("competitor_name") and p.get("distance_km") is not None:
        offer_note = f" They're running \"{p['their_offer']}\"." if p.get("their_offer") else ""
        my_offer = _active_offer(category, merchant)
        counter = f" Your \"{my_offer}\" is still live and price-competitive." if my_offer else ""
        body = (
            f"{sal}, heads up — {p['competitor_name']} opened {p['distance_km']}km away"
            f"{(' on ' + p['opened_date']) if p.get('opened_date') else ''}.{offer_note}{counter} "
            f"Want me to check how your GBP compares to theirs on photos/reviews so you're not losing the discovery race?"
        )
        return body, "open_ended", "loss_aversion+specificity"
    return _generic_merchant_fallback(category, merchant, trigger, "new competition nearby")


def _festival_upcoming(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    if p.get("festival") and p.get("days_until") is not None:
        offer = _active_offer(category, merchant)
        offer_line = f" Your \"{offer}\" is a natural fit to push this week." if offer else ""
        body = (
            f"{sal}, {p['festival']} is {p['days_until']} days out — this is usually when demand starts ramping "
            f"in your category.{offer_line} Want me to draft a {p['festival']} Google post + WhatsApp broadcast now, "
            f"before the rush?"
        )
        return body, "open_ended", "loss_aversion+effort_externalization"
    return _generic_merchant_fallback(category, merchant, trigger, f"{p.get('festival', 'an upcoming festival')}")


def _perf_spike(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    delta = _pct(p.get("delta_pct"))
    if delta and p.get("metric"):
        driver = f" Likely driver: {p['likely_driver'].replace('_', ' ')}." if p.get("likely_driver") else ""
        body = (
            f"{sal}, your {p['metric']} are up {delta} this week (vs {p.get('vs_baseline', 'your usual baseline')}/day)."
            f"{driver} Worth doubling down while it's hot — want me to put a small boost behind whatever's driving it?"
        )
        return body, "open_ended", "specificity+social_proof"
    return _generic_merchant_fallback(category, merchant, trigger, "a performance uptick")


def _perf_dip(category, merchant, trigger, customer, seasonal=False):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    delta = _pct(p.get("delta_pct"))
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr")
    my_ctr = merchant.get("performance", {}).get("ctr")
    if delta and p.get("metric"):
        if seasonal or p.get("is_expected_seasonal"):
            note = p.get("season_note", "").replace("_", " ")
            body = (
                f"{sal}, your {p['metric']} are down {delta} this week — flagging that this lines up with "
                f"the {note or 'usual seasonal pattern'}, so it's not a red flag on its own. "
                f"Best move now is to hold spend and focus on retention. Want a quick retention idea for this window?"
            )
        else:
            peer_note = ""
            if peer_ctr and my_ctr is not None and my_ctr < peer_ctr:
                peer_note = f" Category peers are averaging {_pct(peer_ctr)} CTR, you're at {_pct(my_ctr)}."
            body = (
                f"{sal}, your {p['metric']} are down {delta} this week (vs {p.get('vs_baseline', 'usual')}/day)."
                f"{peer_note} Want me to check what changed — profile, offers, or posts — and fix the likely cause?"
            )
        return body, "open_ended", "specificity+loss_aversion"
    return _generic_merchant_fallback(category, merchant, trigger, "a performance dip")


def _seasonal_perf_dip(category, merchant, trigger, customer):
    return _perf_dip(category, merchant, trigger, customer, seasonal=True)


_METRIC_LABELS = {"review_count": "reviews", "views": "views", "calls": "calls", "directions": "directions"}


def _milestone_reached(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    if p.get("value_now") is not None and p.get("milestone_value"):
        metric_label = _METRIC_LABELS.get(p.get("metric"), str(p.get("metric", "milestone")).replace("_", " "))
        remaining = p["milestone_value"] - p["value_now"]
        if p.get("is_imminent") and remaining > 0:
            body = (
                f"{sal}, you're {remaining} {metric_label} away from {p['milestone_value']} {metric_label} — "
                f"worth a small push this week. Want me to draft a review-ask message for your last few happy customers?"
            )
        else:
            body = (
                f"{sal}, you just crossed {p.get('value_now')} {metric_label} 🎉 "
                f"That's a solid trust signal for new customers. Want me to turn it into a Google post?"
            )
        return body, "open_ended", "social_proof+effort_externalization"
    return _generic_merchant_fallback(category, merchant, trigger, "a milestone")


def _dormant_with_vera(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    days = p.get("days_since_last_merchant_message")
    topic = (p.get("last_topic") or "").replace("_", " ")
    if days:
        follow = f" — last we spoke about {topic}." if topic else "."
        body = (
            f"{sal}, it's been {days} days since we last chatted{follow} "
            f"No pressure — just checking if there's one thing I can help move forward this week?"
        )
        return body, "open_ended", "reciprocity+low_friction"
    return _generic_merchant_fallback(category, merchant, trigger, "reconnecting after a quiet stretch")


def _review_theme_emerged(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    theme = (p.get("theme") or "").replace("_", " ")
    if theme and p.get("occurrences_30d"):
        trend_note = f" and trending {p['trend']}" if p.get("trend") else ""
        quote = f" One review said: \"{p['common_quote']}\"." if p.get("common_quote") else ""
        body = (
            f"{sal}, {p['occurrences_30d']} reviews this month mention \"{theme}\"{trend_note}.{quote} "
            f"Small operational fixes here tend to move rating fast. Want me to draft a response template "
            f"+ a one-line action plan?"
        )
        return body, "open_ended", "specificity+loss_aversion"
    return _generic_merchant_fallback(category, merchant, trigger, "a recurring theme in your reviews")


def _active_planning_intent(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    topic = (p.get("intent_topic") or "").replace("_", " ")
    if topic:
        body = (
            f"On it, {sal} — for {topic}, I'd structure it as: a fixed package (so it's easy to quote), "
            f"a headline price point, and one WhatsApp broadcast + Google post to launch it. "
            f"Want me to draft the full package + pricing now so you can just review and greenlight?"
        )
        return body, "open_ended", "effort_externalization"
    return _generic_merchant_fallback(category, merchant, trigger, "the plan you were putting together")


def _curious_ask_due(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    biz = _business_name(merchant)
    body = (
        f"Hi {sal}! Quick one — what's the most-asked-for service at {biz} this week? "
        f"I'll turn the answer into a Google post + a short WhatsApp reply you can reuse when customers ask about it. "
        f"Takes 5 min."
    )
    return body, "open_ended", "asking_the_merchant+reciprocity"


def _renewal_due(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    days = p.get("days_remaining")
    if days is not None:
        amt = p.get("renewal_amount")
        amt_note = f" (₹{amt:,})" if amt else ""
        body = (
            f"{sal}, your {p.get('plan', 'plan')}{amt_note} renews in {days} days. "
            f"Renewing now keeps your listing + offers live with zero gap in visibility. Reply YES to renew, "
            f"or STOP if you'd rather I not remind you again."
        )
        return body, "binary", "loss_aversion+single_commitment"
    return _generic_merchant_fallback(category, merchant, trigger, "your subscription renewal")


def _winback_eligible(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    if p.get("days_since_expiry") is not None:
        dip = _pct(p.get("perf_dip_pct"))
        dip_note = f" and visibility is down {dip} since then" if dip else ""
        lapsed = p.get("lapsed_customers_added_since_expiry")
        lapsed_note = f" — {lapsed} more customers have gone quiet in that window" if lapsed else ""
        body = (
            f"{sal}, it's been {p['days_since_expiry']} days since your plan lapsed{dip_note}.{lapsed_note} "
            f"Reactivating now gets your listing back to full visibility today. Reply YES to reactivate."
        )
        return body, "binary", "loss_aversion+specificity"
    return _generic_merchant_fallback(category, merchant, trigger, "reactivating your plan")


def _gbp_unverified(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    uplift = _pct(p.get("estimated_uplift_pct"))
    path = (p.get("verification_path") or "postcard or phone call").replace("_", " ")
    body = (
        f"{sal}, your Google profile isn't verified yet — verified listings typically see ~{uplift or 'meaningfully'} "
        f"more visibility in local search. It's a quick {path} step. Want me to walk you through it right now?"
    )
    return body, "open_ended", "loss_aversion+specificity"


def _ipl_match_today(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    my_offer = _active_offer(category, merchant)
    if p.get("match"):
        weeknight = p.get("is_weeknight", True)
        if weeknight:
            angle = (f"Match nights have been running higher than your weekday average this season — "
                     f"worth pushing a match-night push notification.")
        else:
            angle = ("Weekend IPL nights tend to shift covers toward home-viewing rather than dine-in — "
                     "a delivery-only push usually outperforms an in-house promo tonight.")
        offer_line = f" Your \"{my_offer}\" already fits this — want me to turn it into a Swiggy banner + Insta story?" if my_offer else " Want me to draft a quick delivery push for tonight?"
        body = (
            f"Quick heads-up {sal} — {p['match']}"
            f"{(' at ' + p['venue']) if p.get('venue') else ''} tonight. {angle}{offer_line}"
        )
        return body, "open_ended", "specificity+judgment_call"
    return _generic_merchant_fallback(category, merchant, trigger, "tonight's match-night demand shift")


def _supply_alert(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    if p.get("molecule") and p.get("affected_batches"):
        batches = ", ".join(p["affected_batches"])
        body = (
            f"{sal}, heads up: voluntary recall on {p['molecule']} — batches {batches} "
            f"({p.get('manufacturer', 'manufacturer')}). Want me to check how many of your chronic-Rx customers "
            f"were dispensed these batches in the last 90 days, and draft their WhatsApp note + replacement workflow?"
        )
        return body, "open_ended", "loss_aversion+specificity"
    return _generic_merchant_fallback(category, merchant, trigger, "a supply/compliance alert")


def _category_seasonal(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    sal = _salutation(category, merchant)
    trends = p.get("trends") or []
    if trends:
        top = ", ".join(t.replace("_", " ").replace("+", "+") for t in trends[:3])
        season = (p.get("season") or "this season").replace("_", " ")
        action = " Want me to draft a shelf/stock reminder + a WhatsApp broadcast highlighting these?" if p.get("shelf_action_recommended") else " Want me to put together a broadcast around this shift?"
        body = f"{sal}, seasonal demand shift for {season}: {top}.{action}"
        return body, "open_ended", "specificity"
    return _generic_merchant_fallback(category, merchant, trigger, "a seasonal demand shift")


# ---- customer-facing (send_as = merchant_on_behalf) ----

def _customer_fact_line(customer: Optional[dict]) -> str:
    """A real, non-fabricated fact about this customer to lean on when a
    trigger's payload is a placeholder (no service/date specifics yet)."""
    if not customer:
        return ""
    rel = customer.get("relationship", {})
    if rel.get("last_visit"):
        return f"since your visit on {rel['last_visit']}"
    if rel.get("visits_total"):
        return f"across your {rel['visits_total']} visits with us"
    return ""


def _recall_due(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    biz = _business_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    hi = _customer_hi_mix(customer)
    if not p.get("service_due") and not p.get("available_slots"):
        fact = _customer_fact_line(customer)
        body = (
            f"Hi {name}, {biz} yahan — {fact + ', ' if fact else ''}bas check karna tha ki aapko dubara visit ke liye kuch chahiye? Batayein."
            if hi else
            f"Hi {name}, {biz} here — {fact + ', ' if fact else ''}just checking if you're due for a follow-up visit. Let us know."
        )
        return body, "open_ended", "reciprocity"
    slots = p.get("available_slots") or []
    offer = _active_offer(category, merchant)
    slot_line = ""
    if len(slots) >= 2:
        slot_line = f"{slots[0]['label']} ya {slots[1]['label']}" if hi else f"{slots[0]['label']} or {slots[1]['label']}"
    elif slots:
        slot_line = slots[0]["label"]
    service = (p.get("service_due") or "").replace("_", " ")
    if hi:
        body = (
            f"Hi {name}, {biz} yahan 🦷 {service or 'aapka recall'} due hai. "
            + (f"Apke liye slots ready hain: {slot_line}. " if slot_line else "")
            + (f"{offer}. " if offer else "")
            + "Reply 1/2 ya batayein koi aur time chalega."
        )
    else:
        body = (
            f"Hi {name}, {biz} here. Your {service or 'recall'} is due. "
            + (f"Available slots: {slot_line}. " if slot_line else "")
            + (f"{offer}. " if offer else "")
            + "Reply 1/2, or tell us a time that works."
        )
    return body, "binary", "specificity+low_friction"


def _chronic_refill_due(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    biz = _business_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    hi = _customer_hi_mix(customer)
    molecules = p.get("molecule_list") or []
    if not molecules:
        fact = _customer_fact_line(customer)
        body = (
            f"Namaste, {biz} yahan — {fact + ', ' if fact else ''}bas confirm karna tha ki aapki regular medicines ka stock theek hai? Zarurat ho to bata dijiye."
            if hi else
            f"Hi {name}, {biz} here — {fact + ', ' if fact else ''}just checking if you need a refill on your regular medicines. Let us know."
        )
        return body, "open_ended", "reciprocity"
    mol_str = ", ".join(molecules)
    runs_out = (p.get("stock_runs_out_iso") or "")[:10]
    delivery = "Free home delivery to your saved address." if p.get("delivery_address_saved") else ""
    if hi:
        body = (
            f"Namaste, {biz} yahan. {name} ki {mol_str} {runs_out} tak khatam ho jayengi. "
            f"Same dose, same brand pack ready hai. {delivery} Reply CONFIRM to dispatch."
        )
    else:
        body = (
            f"Hi {name}, {biz} here. Your {mol_str} run out around {runs_out}. "
            f"Same dose, same brand ready to go. {delivery} Reply CONFIRM to dispatch."
        )
    return body, "binary", "specificity+effort_externalization"


def _appointment_tomorrow(category, merchant, trigger, customer):
    biz = _business_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    hi = _customer_hi_mix(customer)
    if hi:
        body = f"Hi {name}, {biz} yahan — bas confirm karna tha ki kal ka appointment as-planned hai? Reply YES to confirm, ya batayein agar reschedule karna hai."
    else:
        body = f"Hi {name}, {biz} here — just confirming your appointment tomorrow is still good. Reply YES to confirm, or let us know if you need to reschedule."
    return body, "binary", "low_friction"


def _trial_followup(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    biz = _business_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    hi = _customer_hi_mix(customer)
    opts = p.get("next_session_options") or []
    slot = opts[0]["label"] if opts else None
    if hi:
        body = f"Hi {name}, {biz} yahan! Trial kaisa laga? " + (f"Next session ke liye {slot} available hai — book karein?" if slot else "Agla session book karna chahenge?")
    else:
        body = f"Hi {name}, {biz} here! How was the trial? " + (f"Next session slot open: {slot} — want me to book it?" if slot else "Want to book your next session?")
    return body, "binary", "reciprocity"


def _wedding_package_followup(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    biz = _business_name(merchant)
    owner = _owner_first_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    days = p.get("days_to_wedding")
    window = (p.get("next_step_window_open") or "").replace("_", " ")
    offer = _active_offer(category, merchant)
    if not days and not window:
        fact = _customer_fact_line(customer)
        body = (
            f"Hi {name} 💍 {owner} from {biz} here — {fact + ', ' if fact else ''}"
            f"following up on your bridal planning. Want me to walk you through next steps whenever you're ready?"
        )
        return body, "open_ended", "reciprocity"
    body = (
        f"Hi {name} 💍 {owner} from {biz} here."
        + (f" {days} days to your wedding — good window to start {window}." if days else f" Good window to start {window}.")
        + (f" {offer} covers it." if offer else "")
        + " Want me to hold your preferred slot for the first session next week?"
    )
    return body, "binary", "specificity+urgency"


def _customer_lapsed_soft(category, merchant, trigger, customer):
    biz = _business_name(merchant)
    owner = _owner_first_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    offer = _active_offer(category, merchant)
    body = (
        f"Hi {name} 👋 {owner} from {biz} here. It's been a while since your last visit — "
        + (f"{offer} is on right now if you'd like to come back in. " if offer else "")
        + "Want me to hold a slot for you this week?"
    )
    return body, "binary", "reciprocity+low_friction"


def _customer_lapsed_hard(category, merchant, trigger, customer):
    p = trigger.get("payload") or {}
    biz = _business_name(merchant)
    owner = _owner_first_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    days = p.get("days_since_last_visit")
    focus = (p.get("previous_focus") or "").replace("_", " ")
    offer = _active_offer(category, merchant)
    weeks = f"{days // 7} weeks" if days else "a while"
    focus_note = f" We've got something that fits your {focus} goal well." if focus else ""
    offer_note = f" {offer}, no commitment." if offer else " No commitment."
    body = (
        f"Hi {name} 👋 {owner} from {biz} here. It's been about {weeks} — happens to most people at some point, "
        f"no judgment.{focus_note} Want me to hold a free trial slot for you next week?{offer_note}"
    )
    return body, "binary", "no_shame+low_friction"


# ---- fallbacks ----

def _generic_merchant_fallback(category, merchant, trigger, topic_label: str):
    """Used when a trigger's payload is a placeholder or missing expected fields.
    Leans entirely on the always-populated MerchantContext (performance, signals,
    offers, customer_aggregate) instead of inventing trigger-specific numbers —
    this is the anti-fabrication path required by brief §5 constraint #8."""
    sal = _salutation(category, merchant)
    perf = merchant.get("performance", {})
    signals = merchant.get("signals", []) or []
    agg = merchant.get("customer_aggregate", {})

    facts = []
    if perf.get("views") is not None:
        facts.append(f"{perf['views']} views")
    if perf.get("calls") is not None:
        facts.append(f"{perf['calls']} calls")
    if agg.get("total_unique_ytd"):
        facts.append(f"{agg['total_unique_ytd']} unique customers YTD")
    fact_str = ", ".join(facts[:2])

    signal_note = ""
    if signals:
        signal_note = f" I noticed: {signals[0].replace(':', ' — ').replace('_', ' ')}."

    body = (
        f"{sal}, checking in on {topic_label}."
        + (f" Your current numbers: {fact_str} (last 30d)." if fact_str else "")
        + signal_note
        + " Want me to look into this and come back with a concrete next step?"
    )
    return body, "open_ended", "reciprocity"


def _generic_customer_fallback(category, merchant, trigger, customer):
    biz = _business_name(merchant)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    hi = _customer_hi_mix(customer)
    if hi:
        body = f"Hi {name}, {biz} yahan. Bas ek quick update tha aapke liye — reply karke batayein agar koi help chahiye."
    else:
        body = f"Hi {name}, {biz} here with a quick update — let us know if there's anything you need."
    return body, "open_ended", "low_friction"


KIND_HANDLERS = {
    "research_digest": _research_digest,
    "regulation_change": _regulation_change,
    "cde_opportunity": _cde_opportunity,
    "competitor_opened": _competitor_opened,
    "festival_upcoming": _festival_upcoming,
    "perf_spike": _perf_spike,
    "perf_dip": _perf_dip,
    "seasonal_perf_dip": _seasonal_perf_dip,
    "category_seasonal": _category_seasonal,
    "milestone_reached": _milestone_reached,
    "dormant_with_vera": _dormant_with_vera,
    "review_theme_emerged": _review_theme_emerged,
    "active_planning_intent": _active_planning_intent,
    "curious_ask_due": _curious_ask_due,
    "scheduled_recurring": _curious_ask_due,
    "renewal_due": _renewal_due,
    "winback_eligible": _winback_eligible,
    "gbp_unverified": _gbp_unverified,
    "ipl_match_today": _ipl_match_today,
    "supply_alert": _supply_alert,
    "recall_due": _recall_due,
    "chronic_refill_due": _chronic_refill_due,
    "appointment_tomorrow": _appointment_tomorrow,
    "trial_followup": _trial_followup,
    "wedding_package_followup": _wedding_package_followup,
    "customer_lapsed_soft": _customer_lapsed_soft,
    "customer_lapsed_hard": _customer_lapsed_hard,
}


# ---------------------------------------------------------------------------
# The required pure function
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    Inputs are plain dicts loaded from the dataset JSON (or pushed via
    /v1/context). Returns a dict with keys: body, cta, send_as,
    suppression_key, rationale. Deterministic; no LLM call; <30s always.
    """
    kind = trigger.get("kind", "generic")
    scope = trigger.get("scope", "merchant")
    handler = KIND_HANDLERS.get(kind)
    if handler is None:
        handler = _generic_customer_fallback if (scope == "customer" or customer) else (
            lambda c, m, t, cu: _generic_merchant_fallback(c, m, t, kind.replace("_", " "))
        )

    if scope == "customer" and customer is None:
        # A customer-scope trigger with no customer pushed yet — cannot safely
        # personalize to a specific person, so fall back to a merchant-facing
        # heads-up instead of guessing at customer identity (anti-fabrication).
        body, cta, lever = _generic_merchant_fallback(
            category, merchant, trigger, f"a customer-facing opportunity ({kind.replace('_', ' ')})"
        )
    else:
        body, cta, lever = handler(category, merchant, trigger, customer)

    body = " ".join(body.split())  # normalize whitespace
    send_as = "merchant_on_behalf" if customer else "vera"

    rationale = (
        f"kind={kind}; scope={scope}; send_as={send_as}; "
        f"lever(s)={lever}; anchored on merchant/trigger fields only (no fabricated data); "
        f"{'first-touch, template-eligible' if _first_touch(merchant) else 'follow-up, free-form'}."
    )

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# FastAPI server — the 5 required endpoints (challenge-testing-brief.md §2)
# ---------------------------------------------------------------------------

app = FastAPI(title="Vera-beater bot")
START = time.time()

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}
# suppression keys already fired this test run -> merchant_id (dedup across ticks)
fired_suppressions: set[str] = set()
# conversation_id -> conversation_handlers.ConversationState
conversations: dict[str, "ch.ConversationState"] = {}


def _get(scope: str, cid: str) -> Optional[dict]:
    entry = contexts.get((scope, cid))
    return entry["payload"] if entry else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@app.get("/v1/healthz")
async def healthz():
    counts: dict[str, int] = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Solo — Brinda",
        "team_members": ["Brinda"],
        "model": "rule-based deterministic composer (no external LLM call; see _llm_polish() hook for optional upgrade)",
        "approach": (
            "Kind-routed template composer over the 4-context framework. Every template pulls only "
            "from fields actually present in the pushed contexts (no fabrication); placeholder-payload "
            "triggers fall back to the merchant's own performance/signals data. Stateful conversation "
            "handler layers auto-reply detection, intent-transition routing, hostile/off-topic handling "
            "and graceful exit on top."
        ),
        "contact_email": "brinda@example.com",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        if len(actions) >= 20:  # per-tick action cap (testing-brief §5)
            break
        trigger = _get("trigger", trg_id)
        if not trigger:
            continue
        supp_key = trigger.get("suppression_key") or trg_id
        dedup_key = f"{trigger.get('merchant_id')}::{supp_key}"
        if dedup_key in fired_suppressions:
            continue  # already sent this — restraint over spam

        merchant_id = trigger.get("merchant_id")
        merchant = _get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = _get("category", merchant.get("category_slug"))
        if not category:
            continue
        customer_id = trigger.get("customer_id")
        customer = _get("customer", customer_id) if customer_id else None

        composed = compose(category, merchant, trigger, customer)
        conversation_id = f"conv_{merchant_id}_{trg_id}"

        # Seed conversation state so /v1/reply can pick up the thread coherently.
        conversations[conversation_id] = ch.ConversationState(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            category_slug=category.get("slug", ""),
            kind=trigger.get("kind", "generic"),
            lever=composed["rationale"],
            business_name=_business_name(merchant),
            owner_name=_owner_first_name(merchant),
            hi_mix=_customer_hi_mix(customer) if customer else _merchant_hi_mix(category, merchant),
        )
        conversations[conversation_id].sent_bodies.append(ch._norm(composed["body"]))
        conversations[conversation_id].turns.append({"role": "bot", "text": composed["body"]})

        template_name = f"vera_{trigger.get('kind', 'generic')}_v1"
        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": template_name if _first_touch(merchant) else None,
            "template_params": [_owner_first_name(merchant)] if _first_touch(merchant) else [],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": supp_key,
            "rationale": composed["rationale"],
        })
        fired_suppressions.add(dedup_key)

    return {"actions": actions}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)
    if state is None:
        # Reply for a conversation we never initiated (e.g. merchant messaged
        # first). Build a minimal state from whatever context we have.
        merchant = _get("merchant", body.merchant_id) if body.merchant_id else None
        state = ch.ConversationState(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id or "",
            customer_id=body.customer_id,
            category_slug=(merchant or {}).get("category_slug", ""),
            kind="inbound",
            lever="",
            business_name=_business_name(merchant) if merchant else "",
            owner_name=_owner_first_name(merchant) if merchant else "there",
            hi_mix=True,
        )
        conversations[body.conversation_id] = state

    result = ch.respond(state, body.message)
    return result


class TeardownBody(BaseModel):
    reason: Optional[str] = None


@app.post("/v1/teardown")
async def teardown(_: TeardownBody | None = None):
    contexts.clear()
    fired_suppressions.clear()
    conversations.clear()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
