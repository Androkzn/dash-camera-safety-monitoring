# Pedestrian Right-of-Way — Summary Reference

Based on UVC §11-502 and typical state adoptions across N. America. Use for classifying `pedestrian_proximity` events.

## Core rules

- **Marked crosswalks**: Vehicles must yield to pedestrians within the crosswalk or stepping off the curb into it. Failure to yield = citable.
- **Unmarked crosswalks**: Every intersection has an implied crosswalk on each leg unless signed otherwise. Yielding obligations are the same as marked.
- **Mid-block**: Pedestrian must yield to vehicles, but drivers still owe a duty of care — sudden pedestrian entry does not excuse a preventable collision.
- **Turning vehicles**: Must yield to pedestrians in the conflicting crosswalk during a permissive turn (including right-on-red).
- **Stopped vehicle rule**: Do not pass a vehicle stopped at a crosswalk — assume a pedestrian is being yielded to.

## School zones

- Reduced speed limits (typically 15–25 mph) during posted hours.
- Yield regardless of crosswalk marking when children are present.
- Crossing guard signals override normal right-of-way.

## Stop-arm zones (school bus)

- When a school bus extends its stop-arm with red flashers, **all traffic in both directions must stop** (divided-highway exceptions vary by state).
- The stop-arm zone extends from the bus to where pedestrians (students) may cross — typically the full roadway width plus curb buffer.
- Passing a deployed stop-arm is a serious violation in every state; many jurisdictions treat it as a primary offense with escalated penalties.

## Flagging guidance

Escalate a `pedestrian_proximity` event to human review when **any** of:

- `risk_level=high` within 3s of a stop-arm extension event on the same vehicle.
- Pedestrian bbox overlaps crosswalk polygon AND vehicle speed > 5 mph.
- School-zone geofence active AND pedestrian within 10 ft of vehicle path.
- Any turning maneuver with a pedestrian in the conflicting crosswalk.
- Repeat proximity events (>2 in 60s) on the same route segment — suggests systemic sightline issue.
