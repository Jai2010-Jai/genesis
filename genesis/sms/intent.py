"""Plain-English understanding for chat messages ("send me the link again", "make the buttons bigger").

Rule-based and instant: runs inside the webhook, so it never calls a model. Exact commands (STATUS, USE 2,
...) still work; this decides what everything else means. When a message could be either a new app or a
change, the service asks instead of guessing, because a wrong guess starts a paid build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NEW_APP = re.compile(
    r"\b(?:build|create|make|generate|design|develop|start|code|write)\s+(?:me\s+|us\s+|for\s+me\s+)?"
    r"(?:a|an|another|one|my)\s+(?:new\s+)?(?:[\w'-]+\s+){0,6}?(?:app|application|tracker|calculator|planner|"
    r"journal|timer|counter|game|manager|organi[sz]er|flashcards?|quiz|to-?do list|notes?|diary|log|budget)\b"
    r"|\b(?:new|another|second|separate|different|fresh)\s+app\b"
    r"|\b(?:i\s+(?:want|need|would like|'d like)|can you (?:make|build|create)|could you (?:make|build|create))\s+"
    r"(?:a|an|another|one|me a|me an)\s+(?:[\w'-]+\s+){0,6}?(?:app|application|game|tracker|calculator)\b"
    r"|^(?:an?\s+)?(?:[\w'-]+\s+){0,4}?app\s+(?:that|which|to|for|where|with)\b", re.I)

CHANGE = re.compile(
    r"\b(?:add|remove|delete|change|make\s+(?:it|the|them|this|that|everything|all|each|my)|put|rename|fix|"
    r"replace|increase|decrease|bigger|smaller|larger|darker|lighter|brighter|move|swap|switch\s+(?:the|it)|"
    r"turn\s+(?:the|it|on|off)|use\s+(?:a|an|the)|include|show|hide|let\s+me|allow|should|update|edit|tweak|"
    r"improve|redesign|instead|also|color|colour|theme|font|button|screen|page|icon|layout|dark\s*mode|"
    r"light\s*mode)\b", re.I)


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.I)


SMALL = r"[\s!.?,:)(*~😊🙂👍🙏❤️🔥💯]*"
GREETING = _rx(rf"^(?:hi+|hey+|hello+|hiya|yo+|sup|wassup|what'?s up|hola|namaste|good (?:morning|afternoon|evening))"
               rf"(?:\s+(?:there|genesis|bro|man|buddy|dude))?{SMALL}$")
_ACK = (r"(?:thanks?(?: you)?|thank u|thx|ty|tysm|cheers|ok(?:ay)?|kk?|cool|nice|great|awesome|perfect|amazing|"
        r"sweet|got it|alright|sure|lol|haha+|wow|love it|works|it works|so much|a lot|bro|man|buddy|dude|genesis|"
        r"👍|🙏|❤️|🔥)")
THANKS = _rx(rf"^{_ACK}(?:[\s,!.]+{_ACK})*{SMALL}$")
HELP = _rx(r"^(?:help|menu|commands|options)\b", r"\bwhat can (?:you|u) do\b", r"\bhow (?:does|do) (?:this|it|you) work",
           r"\bhow (?:do|can) i use\b", r"^(?:what|who) are (?:you|u)\b")
STATUS = _rx(r"\b(?:status|progress|update|eta)\b\??$", r"\bhow(?:'s| is)\s+(?:it|my app|the build|that)\s+going\b",
             r"\b(?:is|are)\s+(?:it|my app|the app|the build)\s+(?:ready|done|finished|built)\b",
             r"\bhow (?:long|much longer)\b", r"\bwhat(?:'s| is) (?:happening|going on)\b", r"\bany (?:update|news)\b",
             r"\bwhere(?:'s| is) my app\b", r"\bdone yet\b", r"\bready yet\b")
PROJECTS = _rx(r"\b(?:list|show|see|what are)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:apps|projects)\b",
               r"^(?:my\s+)?(?:apps|projects)\??$", r"\bwhat apps\b", r"\bwhich apps\b")
DOWNLOAD = _rx(r"\b(?:send|give|resend|share|get|need|want)\s+(?:me\s+)?(?:the\s+|a\s+|my\s+)?(?:new\s+)?"
               r"(?:download\s+)?(?:link|apk|download|file)\b",
               r"\bwhere(?:'s| is| do i get)\s+(?:the\s+)?(?:link|apk|download)\b", r"^(?:the\s+)?(?:link|apk|download)"
               r"(?:\s+(?:please|pls|again))?\??$", r"\bdownload\s+(?:it|link|again|the app)\b", r"\bhow do i (?:download|install)\b")
CANCEL = _rx(r"^(?:please\s+)?(?:cancel|abort|stop|kill|halt)\s+(?:it|this|that|(?:the\s+|my\s+|this\s+)?build|building|the\s+app)\b",
             r"^(?:abort|never\s*mind|nvm|forget it)\b")
RETRY = _rx(r"^(?:retry|try again|again|redo|one more time|try it again|rebuild|build it again)\b")
SWITCH = re.compile(r"^(?:switch\s+to|use|open|go\s+(?:back\s+)?to|work\s+on|select|pick|back\s+to)\s+"
                    r"(?:the\s+|my\s+)?(?:app\s+|project\s+)?(?:#|no\.?\s*|number\s+)?(?P<target>.+?)(?:\s+(?:app|one|project))?"
                    r"[\s.!?]*$", re.I)
YES = _rx(r"^(?:y|yes|yeah|yep|yup|sure|ok(?:ay)?|do it|go|go ahead|change it|change)\b")
NEW_WORD = _rx(r"^(?:new|new app|a new (?:one|app)|separate|separate app|make (?:a )?new (?:one|app)|start (?:a )?new)\b")
QUESTION = _rx(r"\?\s*$", r"^(?:can|could|do|does|are|is|will|would|what|how|why|who|where|when|which|should)\b")
REQUEST = _rx(r"^(?:can|could|would|will)\s+(?:you|u)\b")  # "can you add X" is a request, not a question
FILLER = {"i", "want", "a", "an", "new", "app", "application", "make", "me", "build", "create", "please", "pls", "can",
          "you", "u", "another", "one", "the", "to", "need", "would", "like", "id", "i'd", "just", "some", "for", "us",
          "lets", "let's", "do", "start", "now", "ok", "okay", "yes", "different", "separate", "fresh"}
NO = _rx(r"^(?:no|nope|nah|cancel|never\s*mind|nvm)\b")


@dataclass
class Intent:
    kind: str  # greeting|thanks|help|status|projects|download|cancel|retry|switch|new|change|ambiguous|unclear
    arg: str = ""


def classify(text: str, *, has_projects: bool, has_active: bool, project_names: dict[int, str] | None = None) -> Intent:
    t = re.sub(r"\s+", " ", text.strip())
    low = t.lower()
    if not t:
        return Intent("unclear")
    for kind, rx in (("greeting", GREETING), ("thanks", THANKS), ("help", HELP), ("cancel", CANCEL),
                     ("retry", RETRY), ("download", DOWNLOAD), ("projects", PROJECTS), ("status", STATUS)):
        if rx.search(t):
            return Intent(kind)
    if has_projects and (m := SWITCH.match(t)):
        target = m.group("target").strip().lower()
        number = _project_number(target, project_names or {})
        if number is not None:
            return Intent("switch", str(number))
    if NEW_APP.search(t):
        return Intent("new", t)
    if QUESTION.search(t) and not (has_active and CHANGE.search(t) and REQUEST.search(t)):
        return Intent("question", t)
    if not has_projects:
        # Nothing to change yet: anything descriptive is an app idea.
        return Intent("new", t) if len(t) >= 15 and len(t.split()) >= 3 else Intent("unclear")
    if CHANGE.search(t):
        return Intent("change", t)  # the service asks which app when none is active
    if t.endswith("?") and len(t.split()) < 12:
        return Intent("unclear")
    if len(t.split()) >= 3:
        return Intent("ambiguous", t)
    return Intent("unclear")


def _project_number(target: str, names: dict[int, str]) -> int | None:
    words = {"one": 1, "first": 1, "1st": 1, "two": 2, "second": 2, "2nd": 2, "three": 3, "third": 3, "3rd": 3,
             "four": 4, "fourth": 4, "five": 5, "fifth": 5}
    target = target.strip(" .!?#")
    if target.isdigit():
        n = int(target)
        return n if n in names else None
    if target in words and words[target] in names:
        return words[target]
    if target in ("last", "latest", "newest", "recent", "previous") and names:
        return max(names)
    for n, name in names.items():
        nm = name.lower()
        if target and (target in nm or nm in target or target.replace(" ", "") in nm.replace(" ", "")):
            return n
    return None


def answer_reply(text: str) -> str | None:
    """Resolve a pending 'new app or change?' question. Returns 'change', 'new', 'no' or None."""
    t = text.strip()
    short = len(t.split()) <= 6
    if NEW_WORD.search(t) or (short and re.search(r"\bnew\b", t, re.I)):
        return "new"
    if YES.search(t) or (short and re.search(r"\bchange\b", t, re.I)):
        return "change"
    if NO.search(t):
        return "no"
    return None


def substance(text: str) -> int:
    """How many words of actual idea a message has ("i want a new app" has none)."""
    return sum(1 for w in re.findall(r"[\w']+", text.lower()) if w not in FILLER)
