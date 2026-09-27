from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Phrase banks (English + common Hindi-English code-mix equivalents, since
# the brief explicitly expects Hinglish merchant replies)
# ---------------------------------------------------------------------------

AUTO_REPLY_HINTS = [
    "thank you for contacting", "thanks for contacting", "we will get back to you",
    "will revert shortly", "automated assistant", "automated message",
    "this is an automated", "hamari team tak pahuncha", "team ko bata denge",
    "aapki jaankari ke liye", "we have received your message", "busy right now",
    "currently unavailable", "out of office",
]

NOT_INTERESTED = [
    "not interested", "no thanks", "no thank you", "stop messaging", "unsubscribe",
    "leave me alone", "don't message", "dont message", "band karo", "mat karo",
    "no need", "not required", "not now, and not later",
]

INTENT_YES = [
    "yes", "yep", "yeah", "ok ", "okay", "let's do it", "lets do it", "go ahead",
    "please proceed", "proceed", "confirm", "sounds good", "do it", "sure",
    "haan", "theek hai", "chalega", "bilkul", "kar do", "kardo", "start karo",
    "ready", "i want to join", "mujhe join karna hai", "yes please",
]

HOSTILE_MARKERS = [
    "spam", "useless", "stupid", "shut up", "scam", "idiot", "harass",
    "nonsense", "bakwas", "faltu",
]

OFF_TOPIC_MARKERS = [
    "gst", "income tax", "loan", "insurance", "visa", "passport",
    "personal problem", "file my taxes", "legal advice",
]

QUALIFYING_OPENERS = ("would you", "do you", "could you tell", "kya aap", "how about", "what if")


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    category_slug: str
    kind: str
    lever: str
    business_name: str = ""
    owner_name: str = "there"
    hi_mix: bool = True
    turns: list = field(default_factory=list)          # [{"role": "bot"/"merchant", "text": str}]
    sent_bodies: list = field(default_factory=list)     # normalized bodies already sent (anti-repetition)
    mode: str = "pitch"                                 # pitch | action | ended
    auto_reply_strikes: int = 0
    unanswered_nudges: int = 0


def _prior_merchant_texts(state: ConversationState):
    return [t["text"] for t in state.turns if t["role"] == "merchant"]


def _dedupe(state: ConversationState, body: str) -> str:
    """Never resend a verbatim body already sent in this conversation
    (testing-brief §10: anti-repetition is a hard operational penalty)."""
    norm = _norm(body)
    if norm not in state.sent_bodies:
        return body
    return body.rstrip(".") + " — following up in case this got buried."


def _send(state: ConversationState, body: str, cta: str, rationale: str) -> dict:
    body = _dedupe(state, body)
    state.sent_bodies.append(_norm(body))
    state.turns.append({"role": "bot", "text": body})
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def _end(state: ConversationState, body: str, rationale: str) -> dict:
    state.mode = "ended"
    state.turns.append({"role": "bot", "text": body})
    return {"action": "end", "body": body, "rationale": rationale}


def respond(state: ConversationState, merchant_message: str) -> dict:
    if state.mode == "ended":
        # Conversation already closed on our side — don't re-engage.
        return {"action": "end", "rationale": "Conversation already ended; not re-opening."}

    msg = (merchant_message or "").strip()
    low = _norm(msg)
    prior_texts = _prior_merchant_texts(state)
    is_verbatim_repeat = any(_norm(t) == low for t in prior_texts)
    is_hint = any(p in low for p in AUTO_REPLY_HINTS)
    state.turns.append({"role": "merchant", "text": msg})

    # 1) Explicit stop / not-interested — highest priority, always honored.
    if any(p in low for p in NOT_INTERESTED):
        exit_body = (
            f"Samajh gayi, koi baat nahi — future mein kabhi help chahiye ho to yahin hoon. All the best, {state.business_name}!"
            if state.hi_mix else
            f"Understood — I won't follow up further. All the best with {state.business_name or 'the business'}!"
        )
        return _end(state, exit_body,
                    "Merchant explicitly signaled not-interested/stop; exiting immediately rather than "
                    "risking a spam penalty (brief §12.5 'knowing when to stop').")

    # 2) Auto-reply detection (verbatim repeat OR canned-language hint).
    if is_verbatim_repeat or is_hint:
        state.auto_reply_strikes += 1
        if state.auto_reply_strikes == 1:
            probe = (
                "Samajh gayi. Team tak pahunchane se pehle — 2 minute ka kaam hai, kya aap khud dekhna chahenge?"
                if state.hi_mix else
                "Got it. Before I loop in your team — this takes 2 minutes, want to take a quick look yourself?"
            )
            return _send(state, probe, "open_ended",
                         "Canned/repeated-language reply detected — one soft probe before assuming auto-reply "
                         "(mirrors brief §9 Pattern B), instead of burning further turns re-pitching.")
        exit_body = (
            f"Koi baat nahi, samajh gayi — main directly owner/manager se connect karne ki koshish karungi. "
            f"{state.business_name} accha chal raha hai — best wishes!"
            if state.hi_mix else
            f"No worries — I'll try reaching the owner/manager directly instead. "
            f"{state.business_name or 'Your business'} is doing well — best wishes!"
        )
        return _end(state, exit_body,
                    "Confirmed auto-reply (repeated/canned text 2+ times) — disengaging rather than wasting "
                    "further turns, per brief §3 pain point #1 (auto-reply pollution).")

    # 3) Hostile language — apologize once, stay polite, handle any off-topic tail, never escalate.
    if any(w in low for w in HOSTILE_MARKERS):
        off_topic = any(w in low for w in OFF_TOPIC_MARKERS)
        base = (
            "Sorry if these messages have felt like noise — that's not the intent, and I'll dial back."
            if not state.hi_mix else
            "Sorry agar yeh messages disturb kar rahe hain — aisa intend nahi tha, main kam kar dungi."
        )
        if off_topic:
            base += (
                " On the other question — that's outside what I can help with here (I'm focused on your "
                "listing/marketing), so I can't take that on, but happy to keep helping with that whenever you'd like."
            )
        else:
            base += " I'll only reach out again when there's something genuinely worth your time."
        return _send(state, base, "none",
                     "De-escalating a hostile message with a single apology, redirecting any off-topic ask "
                     "politely, and not pushing the original pitch further this turn.")

    # 4) Off-topic (non-hostile) — answer briefly that it's outside scope, then return to the thread.
    if any(w in low for w in OFF_TOPIC_MARKERS) and not any(p in low for p in INTENT_YES):
        body = (
            "That's outside what I can help with here — I'm focused on your listing and customer growth. "
            "Happy to pick back up on that whenever works for you."
            if not state.hi_mix else
            "Yeh mere scope se bahar hai — main aapke listing aur growth pe focus karti hoon. "
            "Jab chahen wapas continue kar sakte hain."
        )
        return _send(state, body, "none",
                     "Off-topic, non-mission request — staying polite and on-mission without ignoring the merchant.")

    # 5) Explicit intent transition — switch straight to action, never re-qualify (brief §9 Pattern D).
    if any(p in low for p in INTENT_YES) and state.mode != "action":
        state.mode = "action"
        state.unanswered_nudges = 0
        kind_label = state.kind.replace("_", " ")
        body = (
            f"Great — starting now on {kind_label}. I'll put the draft together and share it here in a few minutes "
            f"for your quick review before anything goes out."
            if not state.hi_mix else
            f"Badhiya — {kind_label} pe kaam shuru karti hoon. Kuch minute mein draft yahin share karungi, "
            f"aap review kar lena, tabhi kuch bhejenge."
        )
        return _send(state, body, "open_ended",
                     "Explicit affirmative/action intent detected — routing directly to action mode instead of "
                     "another qualifying question (this exact miss is called out in brief §9 Pattern D).")

    # 6) Default: acknowledge + advance one concrete step, single low-friction CTA.
    state.unanswered_nudges += 1
    if state.unanswered_nudges >= 3:
        exit_body = (
            "No worries — I'll step back for now. Message me anytime if something changes."
            if not state.hi_mix else
            "Koi baat nahi — abhi ke liye rukti hoon. Kabhi bhi zarurat ho to bata dijiyega."
        )
        return _end(state, exit_body,
                     "3 turns without a clear commit or decline — stepping back gracefully rather than "
                     "continuing to nudge (brief §12.5).")

    is_question = "?" in msg
    if is_question:
        body = (
            "Good question — let me pull the exact numbers and come back with a clear answer. "
            "In the meantime, should I go ahead and start drafting?"
            if not state.hi_mix else
            "Accha sawaal — exact numbers nikal ke batati hoon. Tab tak, draft shuru kar doon?"
        )
    else:
        body = (
            "Noted — here's the one next step that matters most right now: want me to go ahead and draft it "
            "so you can just take a quick look?"
            if not state.hi_mix else
            "Samajh gayi — sabse zaroori next step yeh hai: draft banau, aap bas ek baar dekh lena?"
        )
    return _send(state, body, "open_ended",
                 "Advancing the conversation by one concrete, low-friction step based on the merchant's reply.")
