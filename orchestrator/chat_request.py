"""The participant's build request, owned by the host and never reconstructed by the model.

One rule is unchanged from the first version: only the participant's own words can
become a build request. Assistant plans, guesses, and tool results never do, and the
model's ``task`` argument is ignored. What changed is WHICH of the participant's words.

The first version made the latest message the whole request and could attach earlier
turns only as "reference, not requirements". Every real conversation broke it:

- a chat that ended with "ㄱㄱ" (or the Lab 2 page's own "go") submitted "ㄱㄱ";
- answering the coordinator's clarifying question ("FastAPI + React", "keyboard")
  replaced the request with the answer;
- a correction right after a dispatch became a second build of only the correction;
- the coordinator's own one-time retry submitted "how is it going?", the message it
  happened to be answering;
- attachments never reached the build, and a long, tool-heavy chat trimmed the
  original request out of the model's window before anyone could approve it;
- a dropped connection after a dispatch forgot that the build had started, so the
  same approval started it again.

So the host keeps a ledger per conversation: the participant's turns that have not
been built yet, and the runs this conversation started with the exact request each
was admitted with. The model chooses among those facts; it cannot add to them.

- The request is the turns the model names in ``request_turns`` (or the recorded
  request of a run it names in ``revise_run``), followed by the latest message,
  which takes precedence where it adds or changes anything.
- A turn that only approves ("go", "ㄱㄱ", "진행해 주세요") is never a request.
- While earlier turns are unbuilt, a dispatch must either name them or say
  ``standalone=true``: silently dropping them is how every case above started.
- ``resubmit`` repeats a run's recorded request exactly, once, and only when the
  engine says repeating it can recover the outcome.
- One build per turn. The ledger records a dispatch the moment it is admitted, so a
  disconnect cannot erase it.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

import presets as _presets

# Limits reject complete values; they never hide a clipped request.
MAX_USER_REQUEST_BYTES = 64 * 1024
MAX_USER_CONTEXT_BYTES = 64 * 1024
MAX_USER_CONTEXT_TURNS = 16
MAX_REQUEST_PROVENANCE_BYTES = 160 * 1024
MAX_LEDGER_TURNS = 40          # retained unbuilt turns; older ones are counted, not kept
MAX_LISTED_BYTES = 48 * 1024   # what get_user_request shows at once
# The whole admitted request. It reaches the CLI as ONE argument and the check as
# one environment variable, both under Linux's 128 KiB per-string limit; revising a
# revision could otherwise grow past it and fail as a red gate on working code.
MAX_EFFECTIVE_TASK_BYTES = 96 * 1024
# The Lab 2 page's placeholder, sent unedited, is not anyone's creative direction.
_PLACEHOLDERS = ("<your team's setting, mood, or play idea>",)

LATEST_HEADER = ("\n\nLatest participant message (it takes precedence where it adds to "
                 "or changes the request above):\n")


class UserRequestError(ValueError):
    pass


# ------------------------------------------------------------------- approvals
# A closed list of literal approvals in the workshop's languages. It recognizes a turn
# that ONLY says "go"; it never classifies what a request asks for. Any other word
# makes the turn a request in its own right.
_APPROVAL_WORDS = frozenset("""
go gogo ahead yes yep yeah yup y ok okay k kk sure proceed please do it start
lgtm sounds good ship fine confirm confirmed continue run build that looks great
lets let's alright right now the this for and to me with you just thanks thank
""".split())
_APPROVAL_EMOJI = frozenset("👍 👌 🙆 ✅ 🚀 🆗".split())
# Korean approvals are compounds ("진행해주세요", "부탁드려요"): strip polite and
# imperative endings until a known approval stem remains.
_KO_STEMS = frozenset("""
진행 시작 빌드 만들어 부탁 해 하 가 고 좋아 좋 그렇게 그대로 이대로 바로 그거 그걸로
네 넵 넹 예 옙 응 웅 어 ㅇㅇ ㅇㅋ 오케이 콜 확인 승인 그래 알겠 고마워 감사
""".split())
_KO_ENDINGS = tuple(sorted("""
주세요 주십시오 주실래요 줘요 줘 드려요 드립니다 드릴게요 합니다 할게요 해요 해 하자 자
보자 봐요 봐 씽 싱 즈아 요 용 염 니다 습니다 죠 게요
""".split(), key=len, reverse=True))
# Words that make a short turn a refusal or a change, never an approval.
_DENY_WORDS = frozenset("no not don't dont cancel stop wait hold never nope".split())
_KO_DENY = ("취소", "해지", "중지", "중단", "멈춰", "멈추", "그만", "말고", "아니", "싫", "하지마", "하지 마")
# Greetings and thanks: not a request, so they never have to be "addressed".
_SMALL_TALK = frozenset("""
hi hello hey hiya yo thanks thank you thx cheers
안녕 안녕하세요 반가워 반가워요 반갑습니다 고마워 고마워요 감사 감사해요 감사합니다
""".split())


def _korean_approval(token: str) -> bool:
    if token in _KO_ENDINGS:          # "진행해 주세요": the ending on its own
        return True
    for _ in range(6):
        if token in _KO_STEMS or re.fullmatch(r"ㄱ+|(?:고)+|ㅇ+", token):
            return True
        for ending in _KO_ENDINGS:
            if token.endswith(ending) and len(token) > len(ending):
                token = token[: -len(ending)]
                break
        else:
            return False
    return False


def is_confirmation(text: str) -> bool:
    """True when the whole turn is an approval, such as "go", "ㄱㄱ", or "네 진행해 주세요"."""
    value = (text or "").strip()
    if not value or len(value) > 80:
        return False
    if all(part in _APPROVAL_EMOJI for part in value.split()):
        return True
    words = re.sub(r"[^\w']+", " ", value.casefold()).split()
    if not words:
        return False
    if any(word in _DENY_WORDS for word in words) or any(stem in value for stem in _KO_DENY):
        return False
    if re.fullmatch(r"(?:go)+|(?:ok)+", "".join(words)):
        return True
    return all(word in _APPROVAL_WORDS or _korean_approval(word) for word in words)


def is_small_talk(text: str) -> bool:
    """A greeting, thanks, or bare approval: nothing a later request must address."""
    words = re.sub(r"[^\w']+", " ", (text or "").casefold()).split()
    return bool(words) and len(words) <= 6 and (
        all(word in _SMALL_TALK for word in words) or is_confirmation(text))


# --------------------------------------------------------------------- presets
# A period or comma may follow the id ("preset=x. Creative direction: ..."), but only
# before whitespace or the end, so "preset=x.other" names no preset.
_SELECTOR = r"^\s*`?(?:(?:please\s+)?use\s+)?(?:(?:the\s+)?preset\s*[=:]?\s*)?`?{id}`?(?=[.,;:!]?(?:\s|$))"


def explicit_preset(text: str, preset: str) -> bool:
    """Recognize a literal preset selector the participant typed, in its common spellings.

    ``preset=<id>``, ``Use preset=<id>. Creative direction: ...``, a capitalized or
    backticked form, "please use preset=<id>", or the preset's exact title or
    canonical request. The first version accepted only two exact shapes, so the
    Lab 2 command with a comma, or without "Use", was refused and then submitted as
    a custom build whose request was the selector line itself.
    """
    value = (text or "").strip()
    if not value or preset not in _presets.PRESETS:
        return False
    if re.match(_SELECTOR.format(id=re.escape(preset)), value, flags=re.IGNORECASE):
        return True
    for marker in _markers(preset):
        if value == marker or value.startswith(marker + "\n"):
            return True
    return False


def _markers(preset: str) -> tuple[str, ...]:
    spec = _presets.PRESETS.get(preset) or {}
    return tuple(m for m in (spec.get("title"), _presets.default_task(preset)) if m)


def pure_selector(text: str, preset: str) -> bool:
    """The text says only which preset, and nothing the builder needs to read."""
    raw = (text or "").strip().strip("`")
    value = raw.rstrip(".!")
    return (value.casefold() in {preset, f"preset={preset}", f"use preset={preset}"}
            or raw in _markers(preset) or value in _markers(preset))


def selected_presets(text: str) -> set[str]:
    return {key for key in _presets.PRESETS if explicit_preset(text, key)}


# ---------------------------------------------------------------------- ledger
@dataclass
class RequestLedger:
    """One conversation's unbuilt participant turns and the runs it started."""

    pending: list[tuple[int, str]] = field(default_factory=list)
    runs: dict[str, dict] = field(default_factory=dict)   # run_id -> admitted request
    resubmitted: set[str] = field(default_factory=set)
    omitted: int = 0
    next_id: int = 0
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def begin(self, text: str) -> "UserRequest":
        with self.lock:
            request = UserRequest(current=text, prior=tuple(self.pending),
                                  runs=dict(self.runs), omitted=self.omitted,
                                  turn_id=self.next_id, ledger=self)
            self.next_id += 1
            return request

    def record_dispatch(self, request: "UserRequest", run_id: str, admitted: dict) -> None:
        """Called the moment a run is admitted, so a disconnect cannot erase it."""
        with self.lock:
            request.started.append(run_id)
            self.runs[run_id] = admitted
            if admitted.get("resubmission_of"):
                self.resubmitted.add(admitted["resubmission_of"])

    def commit(self, request: "UserRequest") -> None:
        with self.lock:
            if request.committed:
                return
            request.committed = True
            if request.started:
                # Those words are now built; a new build starts a new request.
                self.pending.clear()
                self.omitted = 0
                return
            if isinstance(request.current, str) and request.current.strip():
                self.pending.append((request.turn_id, request.current))
            overflow = len(self.pending) - MAX_LEDGER_TURNS
            if overflow > 0:
                del self.pending[:overflow]
                self.omitted += overflow

    @classmethod
    def from_messages(cls, messages: list | None) -> "RequestLedger":
        """Rebuild a ledger from a Strands message list (tests and legacy callers)."""
        turns, runs = _turns_from_messages(messages)
        ledger = cls(pending=list(turns), next_id=len(messages or []))
        for run_id in runs:
            ledger.runs.setdefault(run_id, {})
        return ledger


# One ledger per cached coordinator agent (the deployed Runtime keeps one agent per
# session microVM). The console rebuilds its agent each turn and keeps its ledger per
# conversation instead.
def ledger_for(agent: Any) -> RequestLedger:
    ledger = getattr(agent, "_workshop_request_ledger", None)
    if ledger is None:
        ledger = RequestLedger.from_messages(list(getattr(agent, "messages", []) or []))
        setattr(agent, "_workshop_request_ledger", ledger)
    return ledger


# ---------------------------------------------------------------- one request
@dataclass(eq=False)
class UserRequest:
    current: str
    prior: tuple[tuple[int, str], ...] = ()
    runs: dict[str, dict] = field(default_factory=dict)
    omitted: int = 0
    turn_id: int = 0
    ledger: RequestLedger | None = field(default=None, repr=False)
    started: list[str] = field(default_factory=list)
    committed: bool = False
    closed: threading.Event = field(default_factory=threading.Event)
    admission_lock: Any = field(default_factory=threading.RLock)

    def require_active(self) -> None:
        if self.closed.is_set():
            raise UserRequestError("USER_REQUEST_CLOSED")

    def require_open(self) -> None:
        self.require_active()
        if not isinstance(self.current, str) or not self.current.strip():
            raise UserRequestError("EMPTY_USER_REQUEST")
        if len(self.current.encode("utf-8")) > MAX_USER_REQUEST_BYTES:
            raise UserRequestError("USER_REQUEST_TOO_LARGE")

    def close(self) -> None:
        # A tool already inside submit is admitted; a later call cannot start work
        # after a disconnect, even if its synchronous model/routing call returns late.
        with self.admission_lock:
            self.closed.set()
        if self.ledger is not None:
            self.ledger.commit(self)


_USER_REQUEST: ContextVar[UserRequest | None] = ContextVar("chat_user_request", default=None)


@contextmanager
def bind(request: UserRequest):
    token = _USER_REQUEST.set(request)
    try:
        yield request
    finally:
        _USER_REQUEST.reset(token)


def current() -> UserRequest:
    request = _USER_REQUEST.get()
    if request is None:
        raise UserRequestError("USER_REQUEST_NOT_BOUND")
    request.require_open()
    return request


def request_text(prompt: str, attachments: list[dict] | None) -> str:
    """The participant's turn as the host admits it: typed text plus attached text.

    Attachments are the participant's own material (play-test notes, a spec), so
    their text is part of what they asked for. Images are named, not forwarded:
    builders receive text only.
    """
    parts = [prompt or ""]
    for attachment in attachments or []:
        name = str(attachment.get("name") or "attachment")
        data = str(attachment.get("data") or "")
        if data.startswith("data:image/"):
            parts.append(f"[The participant attached an image, {name}. Builders receive "
                         "text only, so describe what it shows in words if it matters.]")
            continue
        parts.append(f"--- attached by the participant: {name} ---\n"
                     f"{attachment.get('text') or ''}")
    return "\n\n".join(part for part in parts if part.strip())


# ------------------------------------------------------------------- selection
def _select(request: UserRequest, turn_ids: list[int] | None) -> list[dict]:
    if not isinstance(turn_ids, (list, type(None))):
        raise UserRequestError("INVALID_USER_CONTEXT")
    ids = turn_ids or []
    if len(ids) > MAX_USER_CONTEXT_TURNS:
        raise UserRequestError("USER_CONTEXT_TOO_LARGE")
    if any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        raise UserRequestError("INVALID_USER_CONTEXT")
    available = dict(request.prior)
    if any(i not in available for i in ids):
        raise UserRequestError("USER_CONTEXT_UNAVAILABLE")
    # Chronology is server-owned; argument order cannot reverse precedence.
    selected = [{"turn": i, "text": text} for i, text in request.prior if i in ids]
    if sum(len(item["text"].encode("utf-8")) for item in selected) > MAX_USER_CONTEXT_BYTES:
        raise UserRequestError("USER_CONTEXT_TOO_LARGE")
    return selected


def listing(request: UserRequest) -> dict:
    """What get_user_request shows: unbuilt turns (newest kept under a byte budget)
    and the runs this conversation started. Never fails because a chat grew long."""
    shown, used = [], 0
    for turn, text in reversed(request.prior):
        size = len(text.encode("utf-8"))
        if used + size > MAX_LISTED_BYTES:
            if not shown:   # one oversized turn: show that it exists, not all of it
                shown.append({"turn": turn, "text": text[:2000], "truncated": True})
            break
        shown.append({"turn": turn, "text": text})
        used += size
    shown.reverse()
    hidden = request.omitted + len(request.prior) - len(shown)
    return {
        "current_user_text": request.current,
        "current_turn_is_approval": is_confirmation(request.current),
        "prior_user_turns": shown,
        "older_turns_not_shown": hidden,
        "runs_started_here": [
            {"run_id": run_id, "preset": item.get("preset"),
             "request_excerpt": " ".join(str(item.get("task") or "").split())[:240]}
            for run_id, item in request.runs.items()],
        "context_scope": "The participant's turns since this conversation's last build, "
                         "and the builds it started.",
    }


# ------------------------------------------------------------------- admission
def admit(model_task: str = "", *, preset: str = "", creative_direction: str = "",
          request_turns: list[int] | None = None, standalone: bool = False,
          revise_run: str = "") -> tuple[str, dict]:
    """The effective task and its provenance, from the participant's words only."""
    request = current()
    if request.started:
        raise UserRequestError("ONE_BUILD_PER_TURN")
    requested = _select(request, request_turns)
    approval = is_confirmation(request.current)
    base: list[str] = []
    revised = None
    if revise_run:
        revised = request.runs.get(revise_run)
        if revised is None:
            raise UserRequestError("RUN_NOT_STARTED_IN_THIS_CONVERSATION")
        if not revised.get("task"):
            raise UserRequestError("RUN_REQUEST_NOT_RECORDED")
        if approval and not request_turns:
            # An approval adds nothing: "revising" with it repeated the recorded
            # request exactly, a resubmit without resubmit_run's once-only bound.
            raise UserRequestError("REVISION_ADDS_NOTHING")
        base.append(revised["task"])
        # The recorded request already holds its preset and direction; the change
        # is the participant's latest words. Re-deriving a direction here made the
        # model's paraphrase fail as "not from the user".
        preset, creative_direction = revised.get("preset") or "", ""
    base += [item["text"] for item in requested]
    user_text = [request.current, *(item["text"] for item in requested)]
    preset_in_current = bool(selected_presets(request.current))
    if not base:
        if approval:
            raise UserRequestError("CONFIRMATION_NEEDS_REQUEST_TURNS")
        substantive = [text for _turn, text in request.prior if not is_small_talk(text)]
        if substantive and not standalone and not preset_in_current:
            raise UserRequestError("EARLIER_TURNS_NOT_ADDRESSED")
    if creative_direction.strip() and not preset:
        raise UserRequestError("AMBIGUOUS_PRESET_DIRECTION")
    if not preset and not revised and any(selected_presets(text) for text in user_text):
        # The participant asked for a preset; a custom build of the selector line
        # would give the builder "preset=..." instead of the preset's request.
        raise UserRequestError("PRESET_SELECTOR_AS_CUSTOM_TASK")

    if revised is not None:
        task = "\n\n".join(base)
        if not approval:
            task += LATEST_HEADER + request.current
    elif preset:
        try:
            canonical = _presets.default_task(preset)
        except _presets.RouteError as exc:
            raise UserRequestError(str(exc)) from exc
        latest = selected_presets(request.current)
        if latest and preset not in latest:
            raise UserRequestError("PRESET_NOT_REQUESTED")
        if not (any(explicit_preset(text, preset) for text in user_text)
                or (revised and revised.get("preset") == preset)):
            raise UserRequestError("PRESET_NOT_REQUESTED")
        if creative_direction and not any(creative_direction in text for text in
                                          user_text + ([revised["task"]] if revised else [])):
            raise UserRequestError("CREATIVE_DIRECTION_NOT_FROM_USER")
        task = canonical if not revised else revised["task"]
        # Retain the participant's whole text: a model-chosen substring of a creative
        # direction must not discard their other constraints.
        extra = [item["text"] for item in requested if not pure_selector(item["text"], preset)]
        for text in extra:
            task += "\n\nParticipant request (verbatim):\n" + text
        if not approval and not pure_selector(request.current, preset):
            task += (LATEST_HEADER if (extra or revised) else
                     "\n\nCurrent participant request (verbatim; latest user text wins):\n")
            task += request.current
    elif base:
        task = "\n\n".join(base)
        if not approval:
            task += LATEST_HEADER + request.current
    else:
        task = request.current
    if approval and base:
        task += f"\n\nThe participant approved this request with: {request.current.strip()}"
    if not task.strip():
        raise UserRequestError("EMPTY_USER_REQUEST")
    if any(marker in text for marker in _PLACEHOLDERS for text in user_text):
        raise UserRequestError("PLACEHOLDER_NOT_REPLACED")
    if len(task.encode("utf-8")) > MAX_EFFECTIVE_TASK_BYTES:
        raise UserRequestError("REQUEST_TOO_LARGE_TO_BUILD")
    provenance = {
        "source": "server_user_turn",
        "current_user_text": request.current,
        "current_user_sha256": hashlib.sha256(request.current.encode("utf-8")).hexdigest(),
        "request_turns": requested,
        "revised_run": revise_run or None,
        "standalone": bool(standalone),
        "current_turn_is_approval": approval,
        "available_prior_user_turns": len(request.prior),
        "effective_task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "model_task_ignored": bool(model_task) and model_task != request.current,
        "preset": preset or None,
        "creative_direction_verified": bool(creative_direction),
    }
    if len(json.dumps(provenance, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_PROVENANCE_BYTES:
        raise UserRequestError("USER_REQUEST_PROVENANCE_TOO_LARGE")
    return task, provenance


def admit_resubmission(run_id: str, allowed: Callable[[str], bool]) -> tuple[dict, dict]:
    """The recorded request of a run this conversation started, for ONE retry."""
    request = current()
    if request.started:
        raise UserRequestError("ONE_BUILD_PER_TURN")
    recorded = request.runs.get(run_id)
    if recorded is None:
        raise UserRequestError("RUN_NOT_STARTED_IN_THIS_CONVERSATION")
    if not recorded.get("task"):
        raise UserRequestError("RUN_REQUEST_NOT_RECORDED")
    if recorded.get("resubmission_of") or (request.ledger and run_id in request.ledger.resubmitted):
        raise UserRequestError("ALREADY_RESUBMITTED_ONCE")
    if not allowed(run_id):
        raise UserRequestError("RESUBMISSION_NOT_ALLOWED")
    provenance = {"source": "resubmission", "resubmission_of": run_id,
                  "current_user_text": request.current,
                  "effective_task_sha256": hashlib.sha256(
                      recorded["task"].encode("utf-8")).hexdigest()}
    return recorded, provenance


_HINTS = {
    "CONFIRMATION_NEEDS_REQUEST_TURNS":
        "The current message only approves an earlier request. Call get_user_request, "
        "then call this tool again with request_turns set to the prior USER turn IDs "
        "whose text IS the request (or revise_run for a run started here). If the "
        "request exists only in your own proposal, ask the participant to state it.",
    "EARLIER_TURNS_NOT_ADDRESSED":
        "This conversation has participant turns that were not built yet. Call "
        "get_user_request. If they are part of this request (the original ask, answers "
        "to your questions, refinements), pass their IDs as request_turns; the latest "
        "message is appended automatically and takes precedence. Only if the current "
        "message is a complete new request on its own, pass standalone=true.",
    "PRESET_SELECTOR_AS_CUSTOM_TASK":
        "The participant named a preset. Call run_build with preset=<that id> (and "
        "creative_direction, if they gave one); never submit the selector line as a "
        "custom task.",
    "PRESET_NOT_REQUESTED":
        "That preset was not named by the participant. Ask them to send preset=<id>, "
        "or submit their own words as a custom request; never resubmit a selector as "
        "a custom task.",
    "REVISION_ADDS_NOTHING":
        "The current message only approves, so a revision would repeat the recorded "
        "request exactly. To retry a failed build use resubmit_run; to change it, "
        "the participant's words must say what changes.",
    "AMBIGUOUS_PRESET_DIRECTION":
        "creative_direction applies only with a preset the participant named. For a "
        "custom request leave creative_direction empty; for a correction use revise_run.",
    "CREATIVE_DIRECTION_NOT_FROM_USER":
        "Pass creative_direction exactly as the participant typed it, or leave it "
        "empty: the participant's whole message is kept either way.",
    "PLACEHOLDER_NOT_REPLACED":
        "The message still contains the page's placeholder text. Ask the participant "
        "for their own setting, mood, or play idea, or to send preset=<id> alone.",
    "REQUEST_TOO_LARGE_TO_BUILD":
        "The combined request is too large to hand to a builder. Ask the participant "
        "for a shorter request, or start a new one instead of revising a revision.",
    "ONE_BUILD_PER_TURN":
        "A build already started in this turn. Use run_build once for a request that "
        "needs several builders; report the started run instead of dispatching again.",
    "RUN_NOT_STARTED_IN_THIS_CONVERSATION":
        "Only a run this conversation started can be revised or resubmitted here. "
        "Tell the participant to submit the request again themselves.",
    "ALREADY_RESUBMITTED_ONCE":
        "This request was already resubmitted once. Report the result and stop; a "
        "person decides what happens next.",
    "RESUBMISSION_NOT_ALLOWED":
        "The engine says repeating this request cannot recover it. Follow the run's "
        "next_action instead.",
}


def error_json(exc: UserRequestError) -> str:
    code = str(exc)
    if code.startswith("DUPLICATE_OF_ACTIVE_BUILD:"):
        return json.dumps({"error": "DUPLICATE_OF_ACTIVE_BUILD",
                           "running_run_id": code.split(":", 1)[1],
                           "hint": "No run was started. The same request is already "
                                   "building; report that run and follow it instead."})
    hint = _HINTS.get(code, "Use the participant's own words; get_user_request lists them. "
                            "Missing or oversized input must be supplied explicitly, never "
                            "reconstructed from an assistant response.")
    return json.dumps({"error": code, "hint": "No run was started. " + hint})


# ---------------------------------------------------------- legacy/message path
def _turns_from_messages(messages: list | None) -> tuple[list[tuple[int, str]], list[str]]:
    """Participant text turns since the last successful dispatch, from Strands messages.

    Tool results are user-role messages too; they are not human input. A dispatch
    boundary is a real tool-use ID of a build tool whose result says it started.
    """
    import chat  # noqa: PLC0415 - the tool names come from the live roster

    names = chat._dispatch_tool_names()
    turns: list[tuple[int, str]] = []
    runs: list[str] = []
    dispatch_ids: set[str] = set()
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        blocks = message.get("content") or []
        if not isinstance(blocks, list):
            continue
        if message.get("role") == "assistant":
            for block in blocks:
                use = block.get("toolUse") if isinstance(block, dict) else None
                if (isinstance(use, dict) and use.get("name") in names
                        and isinstance(use.get("toolUseId"), str) and use["toolUseId"]):
                    dispatch_ids.add(use["toolUseId"])
            continue
        if message.get("role") != "user":
            continue
        results = [block["toolResult"] for block in blocks
                   if isinstance(block, dict) and "toolResult" in block]
        if results:
            for result in results:
                if not isinstance(result, dict) or result.get("toolUseId") not in dispatch_ids:
                    continue
                if result.get("status") == "error":
                    continue
                for block in result.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    try:
                        data = block.get("json") or json.loads(block.get("text", ""))
                    except (TypeError, ValueError):
                        continue
                    if (isinstance(data, dict) and data.get("status") == "started"
                            and isinstance(data.get("run_id"), str) and data["run_id"]):
                        turns.clear()
                        runs.append(data["run_id"])
            continue
        # The typed prompt comes first; attachment blocks follow it.
        text_blocks = [block.get("text") for block in blocks
                       if isinstance(block, dict) and isinstance(block.get("text"), str)]
        text = "\n\n".join(t for t in text_blocks if t.strip())
        if text.strip():
            turns.append((index, text))
    return turns, runs
