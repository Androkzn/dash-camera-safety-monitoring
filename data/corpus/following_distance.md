# Safe Following Distance — Road Safety Policy

Governs classification and coaching thresholds for `vehicle_close_interaction` events.

## Baseline: 3-second rule

- Pick a fixed reference point ahead. When the lead vehicle passes it, count "one-thousand-one, one-thousand-two, one-thousand-three." Your vehicle should not reach the reference before the count completes.
- Applies to passenger vehicles in dry, daylight, free-flow conditions.

## Condition-based extensions

- **Wet pavement / light rain**: 4 seconds.
- **Heavy rain / standing water**: 5 seconds.
- **Snow or ice**: 6+ seconds.
- **Night, unlit roads**: +1 second over baseline for condition.
- **Towing or loaded trailer**: +1 second.
- **Following a motorcycle**: 4 seconds minimum (reaction + stability margin).

## Commercial vehicle extensions

- Class 7/8 trucks: 1 second per 10 ft of vehicle length under 40 mph; add 1 second over 40 mph.
- Buses (transit/school): 4 seconds baseline; 6 seconds in adverse weather.
- A loaded Class 8 at 65 mph needs ~525 ft to stop on dry pavement — double a typical passenger-car gap.

## How to map to events

The detector emits `vehicle_close_interaction` with `risk_level` in {low, medium, high} based on edge-to-edge pixel distance at the current camera focal length (1080p forward dashcam, ~55° HFOV).

- `risk_level=low`: 120–200 px edge distance ≈ 2–3s following — within policy in dry conditions.
- `risk_level=medium`: 60–120 px ≈ 1–2s following — sub-policy; log for weekly review.
- `risk_level=high`: ≤60 px ≈ <1s following — **tailgating**. Triggers an automatic driver coaching flag per `road_policy.md`.

High-risk events sustained >2s or repeated >3x in a trip escalate directly to the safety manager. Weather context (from vehicle telematics) adjusts thresholds up one tier when wipers are active.
