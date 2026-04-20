# Safety Review Policy — Operator SOP

## Review SLAs

- **High-risk**: triaged within **15 minutes** of ingest — pager alert to on-duty operator.
- **Medium-risk**: **24 hours**, daily queue. **Low-risk**: **weekly batch**, Monday AM digest.
- SLA clock starts at event ingest timestamp, not capture timestamp.

## Driver coaching triggers

- 3+ high-risk events in a rolling 7-day window (any event type).
- Any `pedestrian_proximity` with `risk_level=high`.
- Any stop-arm violation (zero tolerance).
- 5+ medium `vehicle_close_interaction` events in a single trip.

## Auto-escalation to safety manager

- Two consecutive missed SLAs on the same driver.
- Any event with collision indicator (g-force > 2.5 or airbag telemetry).
- Stop-arm violation, regardless of other context.
- Pedestrian-proximity high-risk in a school-zone geofence.

## Retention

- **Low-risk**: 90 days, then purged.
- **Medium/high-risk**: 2 years.
- **Any event in a coaching file**: 2 years from coaching close-out.
- **Incident report or litigation hold**: indefinite.

## Redaction before external sharing

- Driver face blur (in-cab camera) and license plate blur on other vehicles — required for insurer, regulator, or legal.
- Audio stripped unless explicitly requested and legally cleared. Redaction is irreversible on exported copy.

## Copilot answer rules

- Prefer recent events (last 24h) over historical generalities; say so when you do.
- When listing events, include **SLA status** (on-track / due-soon / breached) per event.
- Cite specific incidents by `event_id`; never paraphrase an event without its id.
- Do not invent numbers — if a threshold isn't in the corpus, say so and ask.
