/**
 * shared/events/ — event-card primitives reused across features.
 *
 * Both Dashboard (public timeline) and Admin (history list) consume
 * `EventCard`; `FeedbackButtons` is embedded inside it.
 */

export { EventCard } from "./EventCard";
export { EventDialog } from "./EventDialog";
export { FeedbackButtons } from "./FeedbackButtons";
