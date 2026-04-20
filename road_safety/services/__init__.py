"""Business logic services: LLM, agents, vehicle registry, drift, redaction.

This package groups every *non-hot-path* service that supports the main
perception loop in ``road_safety.core``. Code in this package NEVER blocks or
owns the real-time frame pipeline; it provides enrichment, observability,
operational state, and privacy guarantees that surround it.

Modules
-------
``llm``
    LLM enrichment entrypoints (``enrich_event``, ``narrate_event``, ``chat``)
    with provider failover (Anthropic <-> Azure OpenAI), token-bucket rate
    budget, circuit breaker, and self-consistency ALPR. Also the single
    choke-point where raw license-plate text is hashed and stripped before
    it can reach any in-memory event buffer.

``llm_obs``
    Observability for every LLM call: input/output tokens, latency, cost
    estimate, error/skip classification. Exposed via ``/api/llm/stats`` and
    ``/api/llm/recent``.

``redact``
    Pre-egress PII redaction. Produces two thumbnails per event:
    ``*_internal.jpg`` (unredacted, disk-only, requires DSAR token) and
    ``*_public.jpg`` (blurred, safe for Slack / cloud / SSE). Shared
    channels MUST only emit the ``_public`` variant.

``agents``
    Bounded-tool operator copilots (max 5 tools each). Keeping the tool set
    small is deliberate: tool-overload hallucination rises sharply past ~5.

``registry``
    Driver safety-score model with scheduled decay.

``drift``
    Distribution-shift monitor over detection / event statistics.

``watchdog``
    Groups repeated errors into fingerprinted incidents (impact, cause,
    owner, debug commands). Designed as an incident queue, not a log tail.

``digest``
    Periodic operator-facing summaries.

``test_runner``
    Developer-facing harness for offline replay / regression runs.

Invariants to preserve
----------------------
- Every LLM call routes through ``services.llm`` so it inherits failover,
  rate budget, circuit breaker, and cost tracking.
- Raw plate text is scrubbed at INGEST in ``enrich_event``, not at egress.
- Only ``_public`` thumbnails ever leave the host.
"""
