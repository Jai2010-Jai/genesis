"""Inbound SMS handling: identity, rate limits, commands and conversation routing.

Runs inside the webhook request, so it only reads/writes SQLite and enqueues work; it never calls Gemini,
Gradle or the SMS provider. Replies go to the outbox, which the worker sends."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass

from ..config import Settings
from ..core.jobs import JobStore
from . import messages as M
from .observe import event
from .phone import mask, normalize_e164
from .intent import answer_reply, classify, substance
from .store import SmsStore

MIN_PROMPT, MAX_PROMPT = 10, 4000
LINK_MAX_FAILURES, LINK_LOCK_S = 5, 3600
UNKNOWN_REPLY_EVERY_S = 86_400
# Twilio's default Advanced Opt-Out keywords (whole-message match). Twilio blocks further sends itself;
# Genesis records the state and never replies. CANCEL is one of them, so builds are stopped with ABORT.
OPT_OUT = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
# WhatsApp has no carrier opt-out keywords; STOP still opts out of Genesis messages there. The web chat has none.
OPT_OUT_BY_CHANNEL = {"sms": OPT_OUT, "whatsapp": {"STOP", "STOPALL", "UNSUBSCRIBE"}, "web": set()}
OPT_IN = {"START", "UNSTOP", "YES"}
WEB_ADDRESS_RE = re.compile(r"^web:[0-9a-f]{32}$")
NEW_INTENT = re.compile(
    r"\b(?:build|create|make|generate|design|develop|start)\s+(?:me\s+|us\s+)?(?:a|an|another|one)\s+"
    r"(?:\w+\s+){0,5}?(?:app|application|tracker|calculator|planner|journal|timer|counter|game|manager|"
    r"organi[sz]er|flashcards?|quiz|to-?do list)\b"
    r"|\b(?:new|another|second|separate|different)\s+app\b", re.I)
REF_RE = re.compile(r"^G-[2-9A-HJ-NP-TV-Z]{5}$", re.I)


@dataclass
class Inbound:
    from_: str
    to: str
    body: str
    sid: str
    account_sid: str = ""
    num_media: int = 0
    heard: bool = False  # body is the transcript of a voice note


class SmsService:
    def __init__(self, settings: Settings, store: SmsStore, builds: JobStore):
        self.s = settings
        self.store = store
        self.builds = builds
        self._req = threading.local()  # per-request: our number the inbound message was sent to
        self.brain = None  # ChatBrain when plain-language chat runs through the AI backend
        self.owners = {n for n in (normalize_e164(x, settings.sms_default_country_code) for x in settings.sms_owner_numbers) if n}

    # ---------- entry point ----------
    def handle(self, msg: Inbound) -> str:
        """Process one inbound SMS. Returns an outcome label (for logs and tests)."""
        self._req.to = None
        self._req.heard = (msg.body or "").strip() if msg.heard else None
        channel = channel_of(msg.from_)
        if channel == "web":
            # Web chat identities are created by the authenticated chat API, never from a phone number.
            phone = msg.from_ if WEB_ADDRESS_RE.fullmatch(msg.from_ or "") else None
        else:
            phone = normalize_e164(msg.from_, self.s.sms_default_country_code)
        if not phone:
            event("sms.inbound_rejected", logging.WARNING, reason="invalid_sender", sid=msg.sid)
            return "invalid_sender"
        body = (msg.body or "").strip()[:1600]
        user = self.store.get_user(phone)
        if phone in self.owners:
            if user is None:
                user = self.store.create_user(phone, "owner", channel)
                event("sms.user_created", role="owner", phone=mask(phone), channel=channel)
            elif user["role"] != "owner":
                self.store.update_user(user["id"], role="owner")
        our_number = normalize_e164(msg.to) if channel != "web" else None
        if user is not None and channel != "web" and (user["channel"] != channel or
                                                      (our_number and user["reply_from"] != our_number)):
            # Reply on the channel and from the number the user last wrote to.
            self.store.update_user(user["id"], channel=channel, reply_from=our_number or user["reply_from"])
            user = self.store.get_user(phone)
        self._req.to = our_number
        if not self.store.record_inbound(phone, body, msg.sid, user["id"] if user else None):
            event("sms.duplicate", sid=msg.sid, phone=mask(phone))
            return "duplicate"
        event("sms.inbound", sid=msg.sid, phone=mask(phone), channel=channel, chars=len(body),
              media=msg.num_media or None, known=bool(user))

        count = self.store.inbound_count(phone, time.time() - 3600)
        if count > self.s.sms_inbound_per_hour:
            event("sms.rate_limited", logging.WARNING, phone=mask(phone), count=count)
            if count == self.s.sms_inbound_per_hour + 1 and user and user["status"] == "active":
                self._reply(phone, user, "You're sending messages too quickly. Please wait a while and try again.")
            return "rate_limited"

        word, rest = _split(body)
        if body.upper() in OPT_OUT_BY_CHANNEL[channel]:
            if user:
                self.store.update_user(user["id"], status="opted_out")
            event("sms.opted_out", phone=mask(phone))
            return "opted_out"
        if body.upper() in OPT_IN and user and user["status"] == "opted_out":
            self.store.update_user(user["id"], status="active")
            self._reply(phone, user, "Welcome back. Text HELP for commands.")
            return "opted_in"

        if user is None:
            if channel == "web":
                return "unauthorized"
            return self._unknown(phone, channel, word, rest, body, msg.sid)
        if user["status"] != "active":
            event("sms.ignored", phone=mask(phone), status=user["status"])
            return "ignored"
        self.store.set_inbound_user(msg.sid, user["id"])
        if msg.num_media and not body:
            self._reply(phone, user, "I can only read text and voice notes. Describe your app in words, or send a voice note.")
            return "media_only"
        return self._dispatch(user, word, rest, body, msg.sid)

    # ---------- unknown senders ----------
    def _unknown(self, phone: str, channel: str, word: str, rest: str, body: str, sid: str) -> str:
        info = self.store.unknown_sender(phone)
        reply_to = address(phone, channel)
        now = time.time()
        if word == "LINK" and self.s.sms_signup in ("link", "open"):
            if info["locked_until"] > now:
                event("sms.link_locked", logging.WARNING, phone=mask(phone))
                return "link_locked"
            user = self.store.consume_link_code(rest, phone, channel)
            if user is None:
                failures = info["link_failures"] + 1
                self.store.update_unknown_sender(phone, link_failures=failures,
                                                 locked_until=now + LINK_LOCK_S if failures >= LINK_MAX_FAILURES else 0)
                event("sms.link_failed", logging.WARNING, phone=mask(phone), failures=failures)
                self._send(reply_to, None, "That link code is invalid or expired. Ask the Genesis owner for a new one.")
                return "link_failed"
            self.store.update_unknown_sender(phone, link_failures=0, locked_until=0)
            self.store.set_inbound_user(sid, user["id"])
            user = self._remember_reply_from(user)
            event("sms.user_linked", role=user["role"], phone=mask(phone))
            self._reply(phone, user, "Your number is linked. Text an app idea to start, e.g. "
                                     "\"Build me a habit tracker with streaks\". Text HELP for commands.")
            return "linked"
        if self.s.sms_signup == "open":
            user = self.store.create_user(phone, "guest", channel)
            self.store.set_inbound_user(sid, user["id"])
            user = self._remember_reply_from(user)
            event("sms.user_created", role="guest", phone=mask(phone))
            if word in ("HELP", "INFO", "HI", "HELLO", "") or len(body) < MIN_PROMPT:
                self._reply(phone, user, "Welcome! Describe an Android app and I'll build it. " + M.HELP)
                return "welcomed"
            return self._dispatch(user, word, rest, body, sid)
        event("sms.unauthorized", logging.WARNING, phone=mask(phone))
        if now - info["last_reply_at"] > UNKNOWN_REPLY_EVERY_S:
            self.store.update_unknown_sender(phone, last_reply_at=now)
            text = ("This number isn't linked to Genesis. Ask the Genesis owner for a link code, then text LINK <code>."
                    if self.s.sms_signup == "link" else "This number isn't authorized to use Genesis.")
            self._send(reply_to, None, text)
        return "unauthorized"

    # ---------- commands and plain English ----------
    def _dispatch(self, user: dict, word: str, rest: str, body: str, sid: str) -> str:
        if user.get("pending_text") and answer_reply(body) is not None:
            # "new" / "change" / "no" answers the question we just asked; it is not the NEW command.
            return self._resolve_pending(user, body, sid)
        command = self._command(user, word, rest, sid)
        if command is not None:
            if user.get("pending_text"):
                self.store.update_user(user["id"], pending_text=None)
            return command
        if self.brain is not None:
            if user.get("pending_text"):
                self.store.update_user(user["id"], pending_text=None)
            to, heard = getattr(self._req, "to", None), getattr(self._req, "heard", None)
            self.brain.submit(lambda: self._think(user["id"], body, sid, to, heard))
            return "thinking"
        if user.get("pending_text"):
            resolved = self._resolve_pending(user, body, sid)
            if resolved is not None:
                return resolved
            user = self.store.user_by_id(user["id"])
        return self._plain(user, body, sid)

    # ---------- chat brain ----------
    def _think(self, user_id: int, body: str, sid: str, to: str | None, heard: str | None) -> str:
        self._req.to, self._req.heard = to, heard
        user = self.store.user_by_id(user_id)
        if user is None:
            return "gone"
        try:
            decision = self.brain.decide(self._chat_context(user, body))
        except Exception as e:  # noqa: BLE001 - fall back to the built-in rules
            event("sms.brain_failed", logging.WARNING, error=type(e).__name__, detail=str(e)[:200])
            return self._plain(user, body, sid)
        event("sms.brain", action=decision.action, phone=mask(user["phone"]))
        try:
            return self._apply(user, decision, body, sid)
        except Exception:  # noqa: BLE001
            logging.getLogger("genesis.sms").exception("applying chat decision failed")
            self._reply(user["phone"], user, "Sorry, something went wrong on my side. Could you say that again?")
            return "brain_error"

    def _chat_context(self, user: dict, body: str) -> str:
        lines = [f"Channel: {user['channel']}", "", "Their apps:"]
        projects = self.store.projects(user["id"], limit=10)
        for p in projects:
            last = self.store.latest_job(user["id"], p["id"])
            state = last["status"] if last else "no builds"
            if p["last_success_build_id"] and state != "succeeded":
                state += " (an earlier version is ready)"
            active = " [ACTIVE]" if p["id"] == user["active_project_id"] else ""
            lines.append(f"{p['number']}. {M.name_of(p)}{active} - {state}. Idea: {p['original_prompt'][:200]}")
        if not projects:
            lines.append("(none yet)")
        job = self.store.active_job(user["id"])
        lines += ["", f"Build running now: {job['ref']} ({job['kind']}, {job['status']})" if job else "Build running now: none"]
        history = [m for m in self.store.messages_for_user(user["id"], limit=14) if m["body"]]
        if history and history[-1]["direction"] == "in" and history[-1]["body"].strip() == body.strip():
            history = history[:-1]  # the new message itself
        lines += ["", "Conversation (oldest first):"]
        for m in history[-12:]:
            who = "User" if m["direction"] == "in" else "Genesis"
            lines.append(f"{who}: {m['body'].removeprefix('Genesis: ')[:400]}")
        if not history:
            lines.append("(this is the first message)")
        lines += ["", f"New message from the user: {body[:1600]}"]
        return "\n".join(lines)

    def _apply(self, user: dict, d, body: str, sid: str) -> str:
        reply = (d.reply or "").strip()
        project = None
        if d.project is not None:
            project = self.store.project_by_number(user["id"], d.project)
        if d.action == "new_app":
            spec = (d.spec or "").strip() or body
            return self._start_new(user, spec, sid, lead=reply)
        if d.action == "change_app":
            project = project or self._active_project(user)
            if project is None:
                return self._clarify(user, reply or "Which app should I change?")
            return self._start_change(user, project, (d.spec or "").strip() or body, sid, lead=reply)
        if d.action == "status":
            return self._status(user, "")
        if d.action == "list_apps":
            return self._projects(user)
        if d.action == "download":
            return self._open(user, str(project["number"]) if project else "")
        if d.action == "switch" and project is not None:
            return self._use(user, str(project["number"]))
        if d.action == "cancel":
            return self._cancel(user)
        if d.action == "retry":
            return self._retry(user, sid)
        self._reply(user["phone"], user, reply or "Sorry, I didn't catch that. What would you like to build?")
        return "chat"

    def _command(self, user: dict, word: str, rest: str, sid: str) -> str | None:
        """Exact commands (HELP, STATUS, USE 2, ...). Returns None for anything that reads like normal speech."""
        number = rest.strip().lstrip("#")
        if word in ("HELP", "INFO", "COMMANDS", "?", "MENU") and not rest:
            self._reply(user["phone"], user, M.HELP)
            return "help"
        if word == "NEW":
            if len(rest) >= MIN_PROMPT:
                return self._start_new(user, rest, sid)
            if not rest:
                self.store.update_user(user["id"], awaiting_new=1, active_project_id=None)
                self._reply(user["phone"], user, "Okay, a new app. What should it do?")
                return "awaiting_new"
            return None
        if word == "CHANGE" and rest:
            project = self._active_project(user)
            if project is None:
                return self._clarify(user, "Which app should I change?")
            return self._start_change(user, project, rest, sid)
        if word == "STATUS" and (not rest or REF_RE.fullmatch(rest.strip())):
            return self._status(user, rest)
        if word in ("PROJECTS", "APPS", "LIST") and not rest:
            return self._projects(user)
        if word in ("USE", "SWITCH", "SELECT") and number.isdigit():
            return self._use(user, number)
        if word in ("OPEN", "DOWNLOAD") and (not rest or number.isdigit()):
            return self._open(user, number)
        if word == "LINK" and rest and " " not in rest.strip():
            self._reply(user["phone"], user, "This number is already linked. Just tell me what to build.")
            return "already_linked"
        if word in ("ABORT", "CANCEL") and not rest:  # bare CANCEL on SMS is an opt-out keyword and never gets here
            return self._cancel(user)
        if word == "RETRY" and not rest:
            return self._retry(user, sid)
        return None

    def _plain(self, user: dict, body: str, sid: str) -> str:
        projects = self.store.projects(user["id"], limit=20)
        project = self._active_project(user)
        intent = classify(body, has_projects=bool(projects), has_active=project is not None,
                          project_names={p["number"]: M.name_of(p) for p in projects})
        event("sms.intent", kind=intent.kind, phone=mask(user["phone"]))
        if user["awaiting_new"] and intent.kind in ("new", "change", "ambiguous", "unclear") and len(body) >= MIN_PROMPT:
            return self._start_new(user, body, sid)
        kind = intent.kind
        if kind == "greeting":
            if project is not None:
                text = (f"Hey! We're working on {M.name_of(project)}. Tell me what to change "
                        "(like \"make the buttons bigger\"), or describe a new app.")
            else:
                text = ("Hey! I'm Genesis. Describe an app and I'll build it for your Android phone, "
                        "like \"a habit tracker with streaks and a dark theme\". You can also send a voice note.")
            self._reply(user["phone"], user, text)
            return "greeting"
        if kind == "thanks":
            job = self.store.active_job(user["id"])
            text = ("I'll message you as soon as it's ready." if job else
                    "Anytime! Tell me a change or a new app idea whenever you like.")
            self._reply(user["phone"], user, text)
            return "thanks"
        if kind == "help":
            self._reply(user["phone"], user, M.HELP)
            return "help"
        if kind == "status":
            return self._status(user, "")
        if kind == "projects":
            return self._projects(user)
        if kind == "download":
            return self._open(user, "")
        if kind == "cancel":
            return self._cancel(user)
        if kind == "retry":
            return self._retry(user, sid)
        if kind == "switch":
            return self._use(user, intent.arg)
        if kind == "new":
            if substance(intent.arg) < 1:
                self.store.update_user(user["id"], awaiting_new=1)
                self._reply(user["phone"], user, "Sure, a new app! What should it do?")
                return "awaiting_new"
            return self._start_new(user, intent.arg, sid)
        if kind == "question":
            self._reply(user["phone"], user, "I build Android apps from a description (not websites or iPhone apps). "
                                             "Tell me what you'd like the app to do and I'll build it.")
            return "question"
        if kind == "change" and project is not None:
            return self._start_change(user, project, intent.arg, sid)
        if kind == "ambiguous" and project is not None:
            self.store.update_user(user["id"], pending_text=body[:MAX_PROMPT])
            self._reply(user["phone"], user, f"Should I change {M.name_of(project)} with that, or make it a new app? "
                                             "Reply \"change\" or \"new\".")
            return "ask_new_or_change"
        if kind in ("ambiguous", "change"):
            return self._clarify(user, "Which app is that for?")
        if not projects:
            self._reply(user["phone"], user, "Tell me what app you'd like, e.g. \"a workout log that tracks my sets "
                                             "and shows weekly progress\".")
        else:
            name = M.name_of(project) if project else "your app"
            self._reply(user["phone"], user, f"I'm not sure what you mean. Tell me a change for {name}, describe a "
                                             "new app, or ask \"how's it going?\" or \"send the link\".")
        return "unclear"

    def _resolve_pending(self, user: dict, body: str, sid: str) -> str | None:
        pending = user["pending_text"]
        answer = answer_reply(body)
        self.store.update_user(user["id"], pending_text=None)
        if answer == "new":
            if substance(pending) < 1 or classify(pending, has_projects=False, has_active=False).kind == "question":
                self.store.update_user(user["id"], awaiting_new=1, active_project_id=None)
                self._reply(user["phone"], user, "Sure, a new app! What should it do?")
                return "awaiting_new"
            return self._start_new(user, pending, sid)
        if answer == "change":
            project = self._active_project(user)
            if project is None:
                return self._clarify(user, "Which app should I change?")
            return self._start_change(user, project, pending, sid)
        if answer == "no":
            self._reply(user["phone"], user, "Okay, I won't build that.")
            return "pending_dropped"
        return None  # they said something else: treat it as a new message

    def _clarify(self, user: dict, question: str) -> str:
        lines = [question, "Reply NEW <idea> for a new app, or USE <n> to pick one to change:"]
        lines += [f"{p['number']} {M.name_of(p)}" for p in self.store.projects(user["id"], limit=5)]
        self._reply(user["phone"], user, "\n".join(lines))
        return "clarify"

    def _start_new(self, user: dict, idea: str, sid: str, lead: str = "") -> str:
        if len(idea) < MIN_PROMPT:
            self._reply(user["phone"], user, "Tell me a bit more about the app (at least 10 characters).")
            return "too_short"
        if len(idea) > MAX_PROMPT:
            idea = idea[:MAX_PROMPT]
        if (blocked := self._gate(user)) is not None:
            return blocked
        project = self.store.create_project(user["id"], idea)
        job = self.store.enqueue_job(user_id=user["id"], project_id=project["id"], kind="new", prompt=idea,
                                     change_request=None, base_build_id=None,
                                     max_attempts=self.s.sms_job_max_attempts, inbound_sid=sid)
        event("sms.job_enqueued", ref=job["ref"], kind="new", phone=mask(user["phone"]))
        self._reply(user["phone"], user, (f"{lead} " if lead else "Got it! I'm turning your idea into an app. ") +
                                         f"Build ID: {job['ref']}. "
                                         "I'll message you when it's ready (usually 2-6 min). "
                                         "Ask \"how's it going?\" anytime.",
                    job_id=job["id"])
        return "queued_new"

    def _start_change(self, user: dict, project: dict, change: str, sid: str, lead: str = "") -> str:
        if (blocked := self._gate(user)) is not None:
            return blocked
        changes = json.loads(project["changes"]) + [change[:1000]]
        spec = compose_spec(project["original_prompt"], changes)
        if spec is None:
            self._reply(user["phone"], user, "That project has too many changes to track by SMS. Text NEW <idea> to start fresh.")
            return "too_long"
        base = project["last_success_build_id"]
        if base and not self._build_ok(base):
            base = None
        # With a working previous version, Gemini edits its sources; otherwise rebuild from the full spec.
        job = self.store.enqueue_job(user_id=user["id"], project_id=project["id"], kind="change" if base else "new",
                                     prompt=spec, change_request=change[:1000] if base else None, base_build_id=base,
                                     max_attempts=self.s.sms_job_max_attempts, inbound_sid=sid)
        self.store.add_change(project["id"], change[:1000])
        event("sms.job_enqueued", ref=job["ref"], kind=job["kind"], phone=mask(user["phone"]))
        self._reply(user["phone"], user, (f"{lead} " if lead else f"Got it! Updating {M.name_of(project)} "
                                                                    f"(project {project['number']}). ") +
                                         f"Build ID: {job['ref']}. I'll text you when it's ready. "
                                         "(For a separate app, say \"new app: <idea>\".)", job_id=job["id"])
        return "queued_change"

    def _gate(self, user: dict) -> str | None:
        active = self.store.active_job(user["id"])
        if active:
            self._reply(user["phone"], user, f"Build {active['ref']} is still in progress. Reply STATUS, "
                                             "or ABORT to stop it before starting another.")
            return "busy"
        limit = self.s.sms_daily_builds_guest if user["role"] == "guest" else self.s.sms_daily_builds_member
        if self.store.jobs_since(user["id"], time.time() - 86_400) >= limit:
            event("sms.quota_exceeded", logging.WARNING, phone=mask(user["phone"]), limit=limit)
            self._reply(user["phone"], user, f"You've reached the limit of {limit} builds per day. Please try again tomorrow.")
            return "quota"
        if self.store.queued_total() >= self.s.sms_max_queued:
            event("sms.queue_full", logging.WARNING)
            self._reply(user["phone"], user, "Genesis is busy with other builds. Please try again in a few minutes.")
            return "queue_full"
        return None

    def _status(self, user: dict, rest: str) -> str:
        job = None
        if REF_RE.fullmatch(rest.strip()):
            job = self.store.job_for_user(user["id"], rest.strip())
        if job is None:
            job = self.store.active_job(user["id"])
        if job is None and (project := self._active_project(user)):
            job = self.store.latest_job(user["id"], project["id"])
        if job is None:
            self._reply(user["phone"], user, "No builds yet. Text an app idea to start.")
            return "status_none"
        project = self.store.project(user["id"], job["project_id"])
        name, ref = M.name_of(project), job["ref"]
        if job["status"] == "queued":
            text = f"{ref} ({name}) is waiting for a build slot."
            if job["attempts"]:
                text = f"{ref} ({name}) hit a temporary problem and will retry shortly."
        elif job["status"] == "running":
            text = f"{ref} ({name}) is running."
            if job["build_id"]:
                try:
                    b = self.builds.get(job["build_id"])
                    stage = M.PROGRESS.get(b.get("stage", ""), (None, None, b.get("message") or ""))[2]
                    text = f"{ref} ({name}): {stage} ({b.get('progress', 0)}%)"
                except KeyError:
                    pass
            if job["cancel_requested"]:
                text += " Stopping it now."
        elif job["status"] == "succeeded":
            text = f"{ref} ({name}) is ready. Reply OPEN for the download link."
        elif job["status"] == "cancelled":
            text = f"{ref} ({name}) was cancelled. Reply RETRY to build it again."
        else:
            text = f"{ref} ({name}) failed. {M.failure_reason(job['error'])} Reply RETRY to try again."
        self._reply(user["phone"], user, text)
        return "status"

    def _projects(self, user: dict) -> str:
        projects = self.store.projects(user["id"])
        if not projects:
            self._reply(user["phone"], user, "No projects yet. Text an app idea to start.")
            return "projects_none"
        lines = []
        for p in projects:
            last = self.store.latest_job(user["id"], p["id"])
            state = {"succeeded": "ready", "running": "building", "queued": "queued"}.get(last["status"] if last else "", "failed" if last else "-")
            if p["last_success_build_id"] and state == "failed":
                state = "ready (last change failed)"
            star = " *" if p["id"] == user["active_project_id"] else ""
            lines.append(f"{p['number']} {M.name_of(p)} - {state}{star}")
        self._reply(user["phone"], user, "Your apps (* = active):\n" + "\n".join(lines) + "\nReply USE <n> to switch.")
        return "projects"

    def _use(self, user: dict, rest: str) -> str:
        n = rest.strip().lstrip("#")
        project = self.store.project_by_number(user["id"], int(n)) if n.isdigit() else None
        if project is None:
            return self._clarify(user, "I couldn't find that project.")
        self.store.update_user(user["id"], active_project_id=project["id"], awaiting_new=0)
        self._reply(user["phone"], user, f"Now working on {M.name_of(project)}. Text a change, or OPEN for its link.")
        return "use"

    def _open(self, user: dict, rest: str) -> str:
        n = rest.strip().lstrip("#")
        project = self.store.project_by_number(user["id"], int(n)) if n.isdigit() else self._active_project(user)
        if project is None:
            return self._clarify(user, "Which app?")
        build_id = project["last_success_build_id"]
        if not build_id or not self._build_ok(build_id):
            self._reply(user["phone"], user, f"{M.name_of(project)} has no finished build yet. Reply STATUS for progress.")
            return "open_none"
        url = self.download_url(user["id"], build_id, relative_ok=user["channel"] == "web")
        if url is None:
            self._reply(user["phone"], user, "Download links aren't configured on the Genesis server (PUBLIC_BASE_URL).")
            return "open_unconfigured"
        self._reply(user["phone"], user, f"{M.name_of(project)} (Android APK): {url}\n"
                                         f"Link expires in {self.s.sms_link_ttl_hours}h.")
        return "open"

    def _cancel(self, user: dict) -> str:
        job = self.store.active_job(user["id"])
        if job is None:
            self._reply(user["phone"], user, "No build is running.")
            return "cancel_none"
        state = self.store.request_cancel(job["id"])
        event("sms.cancel_requested", ref=job["ref"], state=state)
        if state == "cancelled":
            self._reply(user["phone"], user, f"Build {job['ref']} cancelled.")
        else:
            self._reply(user["phone"], user, f"Stopping build {job['ref']}...")
        return "cancel_" + state

    def _retry(self, user: dict, sid: str) -> str:
        project = self._active_project(user)
        last = self.store.latest_job(user["id"], project["id"]) if project else None
        if last is None:
            self._reply(user["phone"], user, "Nothing to retry. Text an app idea to start.")
            return "retry_none"
        if last["status"] not in ("failed", "cancelled"):
            self._reply(user["phone"], user, f"Build {last['ref']} is {last['status']}; nothing to retry.")
            return "retry_none"
        if (blocked := self._gate(user)) is not None:
            return blocked
        base = last["base_build_id"] if last["base_build_id"] and self._build_ok(last["base_build_id"]) else None
        job = self.store.enqueue_job(user_id=user["id"], project_id=project["id"], kind="change" if base else "new",
                                     prompt=last["prompt"], change_request=last["change_request"] if base else None,
                                     base_build_id=base, max_attempts=self.s.sms_job_max_attempts, inbound_sid=sid)
        event("sms.job_enqueued", ref=job["ref"], kind=job["kind"], retry_of=last["ref"])
        self._reply(user["phone"], user, f"Retrying {M.name_of(project)}. Build ID: {job['ref']}.", job_id=job["id"])
        return "retry"

    # ---------- helpers ----------
    def _remember_reply_from(self, user: dict) -> dict:
        to = getattr(self._req, "to", None)
        if to and user["reply_from"] != to:
            self.store.update_user(user["id"], reply_from=to)
            user = self.store.user_by_id(user["id"])
        return user

    def _active_project(self, user: dict) -> dict | None:
        return self.store.project(user["id"], user["active_project_id"])

    def _build_ok(self, build_id: str) -> bool:
        try:
            b = self.builds.get(build_id)
        except KeyError:
            return False
        return b.get("status") == "succeeded" and bool(b.get("apk"))

    def download_url(self, user_id: int, build_id: str, relative_ok: bool = False) -> str | None:
        """An expiring link to the build's APK. The web chat can use a same-origin relative link."""
        base = self.s.public_base_url if self.s.public_base_url.startswith(("https://", "http://")) else None
        if base is None and not relative_ok:
            return None
        token = self.store.create_download_token(user_id, build_id, self.s.sms_link_ttl_hours * 3600)
        return f"{base or ''}/download/{token}"

    def voice_allowed(self, sender: str) -> bool:
        """Only transcribe voice notes from linked, active users (transcription costs CPU time)."""
        if channel_of(sender) == "web":
            return False
        phone = normalize_e164(sender, self.s.sms_default_country_code)
        user = self.store.get_user(phone) if phone else None
        return bool(user and user["status"] == "active") or (phone in self.owners)

    def voice_failed(self, msg: Inbound, reason: str) -> None:
        phone = normalize_e164(msg.from_, self.s.sms_default_country_code)
        user = self.store.get_user(phone) if phone else None
        if user is None and phone in self.owners:
            user = self.store.create_user(phone, "owner", channel_of(msg.from_))
        if user is None:
            return
        self._req.to, self._req.heard = normalize_e164(msg.to), None
        self.store.record_inbound(phone, "[voice note]", msg.sid, user["id"])
        self._reply(phone, user, f"Sorry, I couldn't use that voice note ({reason}). Could you type it instead?")

    def _reply(self, phone: str, user: dict | None, text: str, job_id: int | None = None) -> None:
        heard = getattr(self._req, "heard", None)
        if heard:
            self._req.heard = None  # only the first reply repeats what we heard
            quoted = heard if len(heard) <= 140 else heard[:137].rstrip() + "..."
            text = f'You said: "{quoted}"\n\n{text}'
        to = address(user["phone"], user["channel"]) if user else phone
        sender = (user.get("reply_from") if user else None) or getattr(self._req, "to", None)
        self._send(to, user["id"] if user else None, text, job_id, sender)

    def _send(self, phone: str, user_id: int | None, text: str, job_id: int | None = None,
              sender: str | None = None) -> None:
        if sender is None and not phone.startswith("web:"):
            sender = getattr(self._req, "to", None)
        limit = M.MAX_CHARS if not phone.startswith(("whatsapp:", "web:")) else M.MAX_CHARS_CHAT
        self.store.enqueue_sms(phone, M.fit("Genesis: " + text, limit), user_id, job_id, sender)


def channel_of(sender: str | None) -> str:
    s = (sender or "").strip().lower()
    if s.startswith("whatsapp:"):
        return "whatsapp"
    if s.startswith("web:"):
        return "web"
    return "sms"


def address(phone: str, channel: str) -> str:
    """Where replies to a user go: '+91…' (SMS), 'whatsapp:+91…' or 'web:<id>' (web chat)."""
    return "whatsapp:" + phone if channel == "whatsapp" else phone


def _split(body: str) -> tuple[str, str]:
    parts = body.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0].upper().rstrip(":,.!"), (parts[1].strip() if len(parts) > 1 else "")


def compose_spec(original: str, changes: list[str]) -> str | None:
    """The full description of a project: the first request plus every change, newest last, within the prompt limit."""
    kept = list(changes)
    while True:
        spec = original if not kept else original + "\n\nRequested changes (apply all, newest last):\n" + \
            "\n".join(f"- {c}" for c in kept)
        if len(spec) <= MAX_PROMPT:
            return spec
        if len(kept) <= 1:
            return None
        kept.pop(0)
