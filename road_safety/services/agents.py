"""AI agent orchestration — tool-calling agents for safety workflows.

ROLE IN THE SYSTEM
------------------
This module sits ABOVE the perception hot path. The hot path (``core/detection.py``,
``core/egomotion.py``, ``server._run_loop``) produces safety events. These events
are what the agents here consume. Agents never run in the per-frame critical
path; they are invoked on-demand from the HTTP API:

    POST /api/agents/coaching       -> run_coaching_agent
    POST /api/agents/investigation  -> run_investigation_agent
    POST /api/agents/report         -> run_report_agent

Three production agents, each with a focused tool set:

  1. CoachingAgent   — given a high/medium-risk event, generates a structured
                       coaching note for the road safety manager (what happened, why
                       it matters, what the driver should do differently).
  2. InvestigationAgent — correlates a single event with historical data,
                          road safety policy, and drift reports to build a root-cause
                          narrative.
  3. ReportAgent     — queries events, feedback, and drift data to produce a
                       structured daily/weekly safety summary.

KEY INVARIANTS (do not violate)
-------------------------------
  * 5-TOOL CAP PER AGENT. Each tool list exposed to the LLM has at most 5
    entries. Empirically, past ~5 tools, the model starts hallucinating tool
    names and passing ill-formed arguments — "tool-overload hallucination".
    CLAUDE.md codifies this as a hard rule.
  * LLM ROUTING. All LLM calls in this codebase are expected to route through
    ``road_safety/services/llm.py`` helpers so they inherit multi-provider
    failover (Anthropic <-> Azure OpenAI), the token-bucket rate budget, the
    3-fail/60s-open circuit breaker, and cost/latency tracking via
    ``llm_obs.observer``. The agent loop below calls the Anthropic SDK
    directly but uses the same API key resolution and records through the
    same observer — any NEW agent code MUST stay on that path and MUST NOT
    introduce a second raw SDK import.
  * Structured JSON output with schema enforcement via prompt.
  * Idempotent tool calls — re-running with the same input produces the same
    output (the tools are pure reads of the event buffer / disk).
  * Hard stop: agents cap iterations at MAX_STEPS to prevent runaway loops.

PYTHON IDIOMS USED IN THIS FILE (first-time reader notes)
---------------------------------------------------------
  * ``from __future__ import annotations`` — lets type hints like ``dict | None``
    and forward-references work on older Python without string-quoting them.
  * ``@dataclass`` — a decorator that auto-generates ``__init__``, ``__repr__``,
    and ``__eq__`` for a class from its typed attributes. See ``AgentResult``.
  * ``async def`` / ``await`` — cooperative concurrency. An ``async`` function
    returns a coroutine; ``await`` yields control to the event loop while
    waiting for I/O (here, the LLM HTTP call). FastAPI endpoints can ``await``
    these directly without blocking other requests.
  * ``Callable`` type hint — indicates the parameter is a function/closure.
    See ``event_lookup: Callable[[str], dict | None]`` — "a function that
    takes a string and returns a dict or None".
  * ``f"..."`` — f-string, inline string interpolation: ``f"hello {name}"``.
  * ``list[dict]`` / ``dict | None`` — modern (3.10+) generic + union syntax.
  * ``pathlib.Path`` — OS-agnostic paths. ``CORPUS_DIR / "file.md"`` joins paths.
  * CLOSURES — a function that captures variables from its defining scope.
    The ``event_lookup`` argument to ``AgentExecutor`` is a closure provided by
    ``server.py`` that looks up events in the server's in-memory buffer. The
    executor holds the closure and calls it whenever a tool needs the buffer;
    this avoids a tight import coupling between agents.py and server.py.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# All paths are imported from the single source of truth. Never compute
# ``Path(__file__).parent`` in this module — ``config.py`` owns path layout.
from road_safety.config import CORPUS_DIR, DATA_DIR
# ``llm_observer`` is the shared cost/latency tracker. Every LLM call in this
# file records through it so the /api/llm/* admin endpoints reflect agent
# spend alongside enrichment spend.
from road_safety.services.llm_obs import observer as llm_observer

# MAX_STEPS — hard cap on the agent's tool-use loop. After this many
# assistant-tool-result round trips, we stop regardless of what the model is
# doing. Prevents runaway loops where a confused model keeps calling tools
# without converging on a final answer. 5 is enough for a coaching or
# investigation workflow that needs 2-3 lookups plus a summary.
MAX_STEPS = 5


# ===========================================================================
# SECTION 1 — Tool implementations
# ---------------------------------------------------------------------------
# Each ``tool_*`` function is a pure read that the LLM can invoke through
# Anthropic's function-calling interface. They are pure in the sense that
# they do not emit events, do not call LLMs, and do not mutate state — they
# read from the in-memory event buffer or from disk.
#
# CRITICAL ROUTING RULE
# ---------------------
# Tool implementations MUST NOT import or call any LLM SDK (anthropic,
# openai, azure) directly. If a tool needs enrichment or text generation,
# it must go through ``road_safety/services/llm.py`` so the call inherits:
#     - multi-provider failover (Anthropic <-> Azure OpenAI)
#     - client-side token-bucket rate budget
#     - circuit breaker (3 failures -> 60s open)
#     - cost and latency tracking through ``llm_obs.observer``
# Bypassing this wrapper silently breaks cost accounting and resilience.
# ===========================================================================


def tool_get_event(event_lookup: Callable, event_id: str) -> dict | None:
    """Retrieve a single event by ID from the live buffer.

    Args:
        event_lookup: Closure provided by ``server.py`` that knows how to
            fetch an event dict from the server's in-memory deque keyed by
            ``event_id``. We pass the closure rather than importing the
            buffer directly to keep this module independent of server.py.
        event_id: The string event_id assigned at emission time.

    Returns:
        The event dict if found, or None when the buffer does not contain
        that id (event has rolled off the ring, or the id is invalid).
    """
    return event_lookup(event_id)


def tool_get_recent_events(
    events_source: Callable,
    risk_level: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query recent events with optional filters.

    Args:
        events_source: Closure that returns the full recent-events list
            from the server buffer.
        risk_level: If provided, keep only events whose ``risk_level``
            field equals this string (e.g. "high").
        event_type: If provided, keep only events whose ``event_type``
            equals this string (e.g. "near_miss").
        limit: Maximum number of events to return. The tail (most recent
            ``limit`` items) is returned because the buffer is append-only
            in time order.

    Returns:
        A list of event dicts, at most ``limit`` long. Empty list when the
        buffer is empty or filters match nothing.
    """
    # ``events_source() or []`` guards the case where the closure returns
    # None during startup before the buffer is populated.
    events = events_source() or []
    # List comprehensions — Python's concise filter/map syntax.
    # ``[e for e in events if e.get("risk_level") == risk_level]``
    # is equivalent to a for-loop that appends matching items.
    if risk_level:
        events = [e for e in events if e.get("risk_level") == risk_level]
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type]
    # ``events[-limit:]`` — negative slice: "the last ``limit`` items".
    return events[-limit:]


def tool_get_policy(filename: str | None = None) -> str:
    """Load road safety policy corpus documents.

    The "corpus" is a small directory of Markdown files that define the
    fleet's written road safety policy. The coaching and investigation
    agents cite these documents in their output so recommendations are
    grounded in the organization's actual rules instead of the LLM's
    generic opinions.

    Args:
        filename: Optional specific file under ``CORPUS_DIR`` to return
            (e.g. ``"road_policy.md"``). If omitted, concatenate every
            ``*.md`` in the corpus with a ``=== filename ===`` header.

    Returns:
        The file contents (single-file mode), the concatenated corpus
        (all-files mode), or a human-readable not-found string. Always
        returns a string so the tool contract with the LLM is stable —
        never None, never raises.
    """
    if not CORPUS_DIR.exists():
        return "No policy corpus available."
    if filename:
        # ``CORPUS_DIR / filename`` uses pathlib's ``/`` operator to join
        # path segments portably (Windows and POSIX).
        path = CORPUS_DIR / filename
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
        return f"Policy file '{filename}' not found."
    chunks = []
    # ``glob("*.md")`` yields all Markdown files in the corpus directory.
    # ``sorted(...)`` makes output deterministic across runs so the LLM
    # sees a stable context — important for caching and reproducibility.
    for p in sorted(CORPUS_DIR.glob("*.md")):
        try:
            chunks.append(f"=== {p.name} ===\n{p.read_text(encoding='utf-8')}")
        except Exception:
            # One unreadable policy file should not block the others.
            # A narrower ``except OSError`` would be more idiomatic; this
            # broad catch is kept for defense in depth on the admin path.
            pass
    # ``"\n\n".join(chunks)`` is Python's standard way to glue a list of
    # strings with a separator. Returns "" if chunks is empty, hence the
    # explicit fallback message.
    return "\n\n".join(chunks) if chunks else "No policy documents found."


def tool_get_feedback(limit: int = 100) -> list[dict]:
    """Read recent operator feedback.

    Operator feedback lives as a JSONL (JSON Lines) file — one JSON object
    per line — appended whenever a human marks an event true-positive (tp)
    or false-positive (fp) in the admin UI. JSONL is used over a JSON array
    so appends are atomic and the file can be tailed.

    Args:
        limit: Maximum number of most-recent records to return.

    Returns:
        List of feedback dicts in file order (oldest first within the
        returned window). Empty list when the file doesn't exist yet or
        cannot be read — tools must degrade gracefully, never raise.
    """
    path = DATA_DIR / "feedback.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    # ``try/except`` structure — Python's exception handling. If code
    # inside ``try:`` raises, control jumps to a matching ``except``. The
    # narrow ``OSError`` below covers file I/O errors only, letting
    # unexpected bugs propagate.
    try:
        # splitlines() handles both \n and \r\n line endings.
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # One malformed line shouldn't ruin the whole read.
                    continue
    except OSError:
        pass
    return out[-limit:]


def tool_get_drift_report(drift_monitor) -> dict:
    """Get current drift monitoring report.

    Wraps ``DriftMonitor.compute()`` (see ``services/drift.py``) so an
    agent can read precision, trend, and per-bucket breakdown. The
    investigation agent uses this to answer "is this a noisy event type
    right now?" before recommending action.

    Args:
        drift_monitor: A ``DriftMonitor`` instance from ``services/drift.py``.

    Returns:
        JSON-serializable dict from ``DriftReport.as_dict()``, or an
        ``{"error": ...}`` sentinel when computation fails. Never raises.
    """
    try:
        return drift_monitor.compute().as_dict()
    except Exception:
        return {"error": "drift computation failed"}


def tool_count_by_type(events_source: Callable) -> dict[str, int]:
    """Count events by type for summary reporting.

    Args:
        events_source: Closure returning the server's recent-events list.

    Returns:
        Mapping of ``event_type`` string -> count. Unknown / missing
        ``event_type`` is bucketed as ``"unknown"``.
    """
    events = events_source() or []
    counts: dict[str, int] = {}
    for e in events:
        # dict.get(key, default) — returns default when key missing,
        # never raises KeyError.
        t = e.get("event_type", "unknown")
        counts[t] = counts.get(t, 0) + 1
    return counts


def tool_count_by_risk(events_source: Callable) -> dict[str, int]:
    """Count events by risk level for summary reporting.

    Args:
        events_source: Closure returning the server's recent-events list.

    Returns:
        Mapping of ``risk_level`` string -> count (e.g. ``{"high": 4,
        "medium": 12, "low": 30}``).
    """
    events = events_source() or []
    counts: dict[str, int] = {}
    for e in events:
        r = e.get("risk_level", "unknown")
        counts[r] = counts.get(r, 0) + 1
    return counts


# ===========================================================================
# SECTION 2 — Tool schemas (exposed to the LLM)
# ---------------------------------------------------------------------------
# Each of these lists is the *per-agent* tool catalogue. They are handed to
# Anthropic's messages API as the ``tools`` parameter. The model then
# decides which to call (function-calling / tool-use).
#
# 5-TOOL CAP (CLAUDE.md "Things to avoid")
# ----------------------------------------
# Every list below is at most 5 items. The cap is deliberate: empirical
# testing on Claude and GPT-class models shows hallucination on tool names
# and argument shapes increases sharply past ~5 tools. If you are tempted
# to add a sixth tool to any of these lists, split the agent into two
# narrower agents instead — it will outperform the wider one.
#
# LLM ROUTING REMINDER
# --------------------
# All LLM calls MUST route through ``services/llm.py`` so they inherit
# failover + rate budget + circuit breaker + cost tracking. Do not call
# Anthropic/OpenAI SDKs directly from tool implementations.
# ===========================================================================

# COACHING_TOOLS: 3/5 slots used. Coaching needs the event itself, the
# relevant policy language, and a quick peek at neighbors for context.
COACHING_TOOLS = [
    {
        "name": "get_event",
        "description": "Retrieve a safety event by its event_id",
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    },
    {
        "name": "get_policy",
        "description": "Load road safety policy documents. Optionally specify a filename like 'road_policy.md'",
        "input_schema": {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_recent_events",
        "description": "Get recent events, optionally filtered by risk_level or event_type",
        "input_schema": {
            "type": "object",
            "properties": {
                "risk_level": {"type": "string", "enum": ["high", "medium", "low"]},
                "event_type": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    },
]

# INVESTIGATION_TOOLS: 5/5 slots (3 inherited + 2 new). This is the tool
# list at the hard cap. Adding a sixth tool here REQUIRES removing one
# first — see the 5-tool cap note above.
INVESTIGATION_TOOLS = COACHING_TOOLS + [
    {
        "name": "get_feedback",
        "description": "Get recent operator feedback (tp/fp verdicts)",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}},
            "required": [],
        },
    },
    {
        "name": "get_drift_report",
        "description": "Get the current drift monitoring report with precision metrics",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# REPORT_TOOLS: 5/5 slots. The report agent aggregates across the buffer,
# so it needs the query and both count-by tools. Do NOT add policy/get_event
# here — reports summarize patterns, not individual events, and the extra
# tools degrade pattern-level reasoning.
REPORT_TOOLS = [
    {
        "name": "get_recent_events",
        "description": "Get recent events, optionally filtered by risk_level or event_type",
        "input_schema": {
            "type": "object",
            "properties": {
                "risk_level": {"type": "string", "enum": ["high", "medium", "low"]},
                "event_type": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": [],
        },
    },
    {
        "name": "count_by_type",
        "description": "Get event counts grouped by event_type",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "count_by_risk",
        "description": "Get event counts grouped by risk_level",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_drift_report",
        "description": "Get drift monitoring precision report",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_feedback",
        "description": "Get recent operator feedback",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 100}},
            "required": [],
        },
    },
]


# ===========================================================================
# SECTION 3 — Agent execution engine
# ===========================================================================


@dataclass
class AgentResult:
    """Structured outcome of one agent run.

    This is what the HTTP handlers in ``server.py`` serialize back to the
    dashboard. Fields are intentionally flat and JSON-friendly.

    Attributes:
        agent_type: "coaching" | "investigation" | "report".
        output: Final text (usually a JSON object the LLM produced per the
            system prompt). Empty string on error.
        steps: Number of agent loop iterations actually executed (<=
            MAX_STEPS).
        tool_calls: Human-readable log of every tool invocation, used for
            debugging and displayed in the admin UI timeline.
        latency_ms: Wall-clock time from prompt send to final answer.
        success: True if the run completed cleanly OR reached MAX_STEPS;
            False only on LLM API failure.
        error: Exception string when ``success`` is False.
    """

    agent_type: str
    output: str
    steps: int
    tool_calls: list[str]
    latency_ms: float
    success: bool
    # ``= None`` here — dataclass default value. Fields with defaults must
    # come after fields without defaults, which is why ``error`` is last.
    error: str | None = None

    def as_dict(self) -> dict:
        """Return a JSON-serializable dict for FastAPI response encoding."""
        return {
            "agent_type": self.agent_type,
            "output": self.output,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "latency_ms": round(self.latency_ms, 1),
            "success": self.success,
            "error": self.error,
        }


class AgentExecutor:
    """Runs a tool-calling agent loop using the Anthropic messages API.

    LIFECYCLE
    ---------
    A single ``AgentExecutor`` is constructed in ``server.py`` during the
    FastAPI lifespan startup hook. It captures three closures/handles from
    the server's internal state and is reused across all HTTP requests
    that hit ``/api/agents/*``. Because tools are pure reads, concurrent
    runs are safe — each ``run()`` builds its own messages list and holds
    no state across calls.

    STATE
    -----
      * ``_event_lookup`` — closure that returns an event dict by id from
        the server's in-memory ring buffer.
      * ``_events_source`` — closure that returns the whole recent-events
        list (used by aggregation tools).
      * ``_drift_monitor`` — optional ``DriftMonitor`` instance (see
        ``services/drift.py``). Investigation & report agents skip the
        drift tool gracefully if this is None.

    THE LOOP (``run()``)
    --------------------
      1. Send the system + user prompt (+ tool definitions) to the LLM.
      2. If the response contains ``tool_use`` blocks, execute each tool,
         collect results, and send them back as ``tool_result`` messages.
      3. Repeat until the model returns a pure text response (no more tool
         calls) OR ``MAX_STEPS`` is reached (runaway-loop guard).

    The loop delegates all Anthropic-API specifics to the ``AsyncAnthropic``
    client, but routes the API key resolution and observability recording
    through ``services/llm.py`` + ``services/llm_obs.py`` so it stays on
    the single shared LLM pipeline.
    """

    def __init__(
        self,
        event_lookup: Callable[[str], dict | None],
        events_source: Callable[[], list[dict]],
        drift_monitor=None,
    ):
        """Store closures and optional drift monitor reference.

        Args:
            event_lookup: ``server.py`` closure for "event by id" lookups.
                Using a closure avoids a circular import between server.py
                and this module and lets the buffer stay server-internal.
            events_source: ``server.py`` closure returning the live event
                buffer. Also a closure for the same reason.
            drift_monitor: Shared ``DriftMonitor`` instance or None when
                drift tracking is disabled (tests, minimal deployments).
        """
        # Underscore prefix — Python convention: "private, don't touch".
        # Not enforced by the language, but documented.
        self._event_lookup = event_lookup
        self._events_source = events_source
        self._drift_monitor = drift_monitor

    def _dispatch_tool(self, name: str, args: dict) -> str:
        """Execute a tool call and return the result as a JSON string.

        The Anthropic tool-use API expects the tool result to be a string.
        We serialize every tool return value via ``json.dumps(..., default=str)``
        so non-JSON-native types (datetimes, Paths) degrade to their string
        form rather than raising ``TypeError``.

        Args:
            name: Tool name exactly as declared in the agent's tool list.
                Unknown names return a JSON error instead of raising so
                the agent can see the failure and self-correct.
            args: Parsed JSON arguments the model supplied for this call.

        Returns:
            JSON string the LLM will see as the ``tool_result`` content.
        """
        if name == "get_event":
            result = tool_get_event(self._event_lookup, args.get("event_id", ""))
            return json.dumps(result or {"error": "event not found"}, default=str)
        if name == "get_recent_events":
            result = tool_get_recent_events(
                self._events_source,
                risk_level=args.get("risk_level"),
                event_type=args.get("event_type"),
                limit=args.get("limit", 50),
            )
            return json.dumps(result, default=str)
        if name == "get_policy":
            return tool_get_policy(args.get("filename"))
        if name == "get_feedback":
            result = tool_get_feedback(args.get("limit", 100))
            return json.dumps(result, default=str)
        if name == "get_drift_report":
            if self._drift_monitor:
                result = tool_get_drift_report(self._drift_monitor)
                return json.dumps(result, default=str)
            return '{"error": "drift monitor not available"}'
        if name == "count_by_type":
            return json.dumps(tool_count_by_type(self._events_source))
        if name == "count_by_risk":
            return json.dumps(tool_count_by_risk(self._events_source))
        return json.dumps({"error": f"unknown tool: {name}"})

    async def run(
        self,
        agent_type: str,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 1024,
    ) -> AgentResult:
        """Execute one agent run to completion or MAX_STEPS.

        Args:
            agent_type: Tag used in tool-call logs and observer records
                (e.g. "coaching"). Drives the ``call_type`` label sent to
                ``llm_observer`` for cost attribution.
            system_prompt: Top-level instruction, including required output
                shape. Not sent as a user message — Anthropic API has a
                dedicated ``system=`` parameter.
            user_prompt: The single initial user turn (e.g. "investigate
                event evt-123"). Further turns are appended as the loop
                runs.
            tools: The per-agent tool list (MUST be one of
                ``COACHING_TOOLS`` / ``INVESTIGATION_TOOLS`` /
                ``REPORT_TOOLS`` — the 5-tool cap applies).
            model: Anthropic model id. Haiku is the default because the
                agent loop is latency-sensitive and the tasks are
                well-structured.
            max_tokens: Output cap per completion. Raised to 2048 for the
                report agent which produces a longer summary.

        Returns:
            ``AgentResult`` — never raises, always produces a result the
            HTTP handler can serialize. Failures are reflected in
            ``success=False`` + ``error``.
        """
        # Delayed (lazy) imports: only pay the ``anthropic`` import cost
        # when someone actually invokes an agent. Keeps server cold-start
        # fast and lets the module load even without the SDK available.
        from anthropic import AsyncAnthropic
        # Even though we construct an Anthropic client here, we deliberately
        # pull the API key from ``services/llm.py`` — the same module the
        # enrichment pipeline uses. This keeps key management and "is the
        # LLM layer configured?" in one place. If you add agent routing
        # for a second provider, wire it through that module; do NOT read
        # env vars directly here.
        from road_safety.services.llm import _ANTHROPIC_KEY, llm_configured

        if not llm_configured() or not _ANTHROPIC_KEY:
            # Fail fast with a structured result — the dashboard can show
            # "LLM not configured" without crashing.
            return AgentResult(
                agent_type=agent_type, output="LLM not configured",
                steps=0, tool_calls=[], latency_ms=0, success=False,
                error="no LLM backend",
            )

        client = AsyncAnthropic(api_key=_ANTHROPIC_KEY)
        # ``messages`` is the conversation we grow across loop iterations.
        # We seed it with just the user's prompt; the system prompt goes
        # in a separate parameter on every call.
        messages: list[dict] = [{"role": "user", "content": user_prompt}]
        tool_call_log: list[str] = []
        # ``time.monotonic()`` — the right clock for measuring durations
        # (immune to wall-clock jumps like NTP sync or DST).
        t0 = time.monotonic()

        # The core tool-calling loop. ``range(MAX_STEPS)`` gives us step
        # indices 0..MAX_STEPS-1. If the model keeps asking for tools past
        # that, we break out with a graceful "max steps reached" response.
        for step in range(MAX_STEPS):
            try:
                # ``await`` — suspends this coroutine until the HTTP call
                # completes, letting FastAPI serve other requests meanwhile.
                resp = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                )
            except Exception as exc:
                # Any failure (network, auth, rate limit, model error)
                # lands here. We still record latency to ``llm_observer``
                # so failed-call cost is visible on the admin dashboard.
                elapsed = (time.monotonic() - t0) * 1000
                llm_observer.record(
                    call_type=f"agent_{agent_type}", model=model,
                    latency_ms=elapsed, success=False, error=str(exc),
                )
                return AgentResult(
                    agent_type=agent_type, output="",
                    steps=step, tool_calls=tool_call_log,
                    latency_ms=elapsed, success=False, error=str(exc),
                )

            # Anthropic responses come back as a list of content blocks,
            # each tagged by ``.type``. We partition them into the two
            # kinds we care about: tool calls and plain text.
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            text_blocks = [b for b in resp.content if b.type == "text"]

            if not tool_uses:
                # No more tool calls — the model has produced its final
                # answer. Join all text blocks into a single string and
                # return.
                elapsed = (time.monotonic() - t0) * 1000
                output = "\n".join(b.text for b in text_blocks)
                # ``getattr(obj, name, default)`` — safe attribute access
                # that returns ``default`` instead of raising when the
                # attribute is missing. Older SDK versions may not expose
                # ``usage`` on every response.
                usage = getattr(resp, "usage", None)
                llm_observer.record(
                    call_type=f"agent_{agent_type}", model=model,
                    input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                    output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                    latency_ms=elapsed, success=True,
                )
                return AgentResult(
                    agent_type=agent_type, output=output,
                    steps=step + 1, tool_calls=tool_call_log,
                    latency_ms=elapsed, success=True,
                )

            # The model requested one or more tools. Append its full
            # content (including tool_use blocks) to the conversation so
            # our tool_result replies can reference them by ``tool_use_id``.
            messages.append({"role": "assistant", "content": resp.content})

            # Execute every requested tool this turn, in order. Anthropic
            # allows parallel tool calls in one assistant turn; we satisfy
            # all of them before looping.
            tool_results = []
            for tu in tool_uses:
                # Human-readable line for the UI timeline.
                tool_call_log.append(f"{tu.name}({json.dumps(tu.input)})")
                result_str = self._dispatch_tool(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    # 4000-char truncation — keeps giant tool outputs
                    # from blowing the context window. Tools are expected
                    # to return pre-trimmed data; this is a safety net.
                    "content": result_str[:4000],
                })
            # Tool results are sent as a new "user" turn — that's the
            # shape Anthropic's tool-use protocol expects.
            messages.append({"role": "user", "content": tool_results})

        # Fell out of the loop without the model producing a final text
        # answer. We still return success=True because the LLM didn't
        # error — it just went over budget. Dashboard treats this as a
        # soft failure the operator can investigate.
        elapsed = (time.monotonic() - t0) * 1000
        llm_observer.record(
            call_type=f"agent_{agent_type}", model=model,
            latency_ms=elapsed, success=True,
        )
        output = "Agent reached maximum steps without final answer."
        return AgentResult(
            agent_type=agent_type, output=output,
            steps=MAX_STEPS, tool_calls=tool_call_log,
            latency_ms=elapsed, success=True,
        )


# ===========================================================================
# SECTION 4 — Pre-built system prompts
# ---------------------------------------------------------------------------
# System prompts encode the required output schema so the ``output`` field
# of ``AgentResult`` is machine-parseable JSON. The dashboard parses these
# client-side; if you change a schema here, update the matching TypeScript
# types in the frontend.
# ===========================================================================

COACHING_SYSTEM = (
    "You are a safety coaching assistant. Given a safety event, generate "
    "a structured coaching note for the road safety manager. Use the available tools "
    "to retrieve the event details and relevant road safety policy.\n\n"
    "Your output MUST be a JSON object with these fields:\n"
    '  "event_id": string,\n'
    '  "severity": "high" | "medium" | "low",\n'
    '  "what_happened": string (2-3 sentences describing the incident),\n'
    '  "why_it_matters": string (safety impact, citing policy if relevant),\n'
    '  "recommended_action": string (specific coaching for the driver),\n'
    '  "policy_reference": string | null (filename and section if applicable)\n'
    "\nReturn ONLY the JSON object, no markdown fences or preamble."
)

INVESTIGATION_SYSTEM = (
    "You are a safety investigator. Given an event_id, conduct a "
    "structured investigation by gathering event details, checking for "
    "similar recent events, reviewing operator feedback, consulting road safety "
    "policy, and checking drift reports.\n\n"
    "Your output MUST be a JSON object with these fields:\n"
    '  "event_id": string,\n'
    '  "summary": string (what happened),\n'
    '  "similar_events": list of event_ids with similar characteristics,\n'
    '  "pattern_detected": boolean,\n'
    '  "pattern_description": string | null,\n'
    '  "operator_feedback_summary": string,\n'
    '  "drift_status": string (current precision and trend),\n'
    '  "root_cause_hypothesis": string,\n'
    '  "recommended_action": string,\n'
    '  "confidence": "high" | "medium" | "low"\n'
    "\nReturn ONLY the JSON object, no markdown fences or preamble."
)

REPORT_SYSTEM = (
    "You are a safety report generator. Produce a structured safety "
    "summary using the available tools to gather event counts, drift data, "
    "and operator feedback.\n\n"
    "Your output MUST be a JSON object with these fields:\n"
    '  "period": string (description of the reporting period),\n'
    '  "total_events": integer,\n'
    '  "by_risk": {"high": int, "medium": int, "low": int},\n'
    '  "by_type": dict of event_type -> count,\n'
    '  "top_issues": list of strings (top 3 safety concerns),\n'
    '  "precision_status": string (current model precision and trend),\n'
    '  "operator_engagement": string (feedback volume and sentiment),\n'
    '  "recommendations": list of strings (top 3 action items)\n'
    "\nReturn ONLY the JSON object, no markdown fences or preamble."
)


# ===========================================================================
# SECTION 5 — Public entry points (called by ``server.py``)
# ---------------------------------------------------------------------------
# These are the thin wrappers the HTTP layer calls. Each is a one-liner
# around ``executor.run()`` with the matching system prompt and tool list.
# Kept explicit rather than generic so each agent's contract (which tools,
# which prompt, which token budget) is obvious and diff-able.
# ===========================================================================


async def run_coaching_agent(
    executor: AgentExecutor, event_id: str
) -> AgentResult:
    """Entry point for ``POST /api/agents/coaching``.

    Args:
        executor: The shared ``AgentExecutor`` from the lifespan hook.
        event_id: Which event to coach on. Passed into the user prompt
            where the model then decides to call ``get_event``.

    Returns:
        ``AgentResult`` whose ``output`` field is the JSON coaching note.
    """
    return await executor.run(
        agent_type="coaching",
        system_prompt=COACHING_SYSTEM,
        user_prompt=f"Generate a coaching note for event: {event_id}",
        tools=COACHING_TOOLS,
    )


async def run_investigation_agent(
    executor: AgentExecutor, event_id: str
) -> AgentResult:
    """Entry point for ``POST /api/agents/investigation``.

    Args:
        executor: The shared ``AgentExecutor``.
        event_id: Event under investigation; the prompt instructs the
            agent to pull context before forming conclusions.

    Returns:
        ``AgentResult`` with the investigation JSON as ``output``.
    """
    return await executor.run(
        agent_type="investigation",
        system_prompt=INVESTIGATION_SYSTEM,
        user_prompt=f"Investigate event: {event_id}. Gather all relevant context before forming conclusions.",
        tools=INVESTIGATION_TOOLS,
    )


async def run_report_agent(executor: AgentExecutor) -> AgentResult:
    """Entry point for ``POST /api/agents/report``.

    Args:
        executor: The shared ``AgentExecutor``.

    Returns:
        ``AgentResult`` whose ``output`` field is the JSON safety summary.
        Uses ``max_tokens=2048`` (double the agent default) because the
        report aggregates across the whole session and needs the room.
    """
    return await executor.run(
        agent_type="report",
        system_prompt=REPORT_SYSTEM,
        user_prompt="Generate a safety summary report for the current session. Query all available data sources.",
        tools=REPORT_TOOLS,
        max_tokens=2048,
    )
