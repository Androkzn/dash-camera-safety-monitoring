/**
 * validation feature — public surface.
 *
 * The shadow-mode dual-model validator UI split out of the Monitoring
 * tab: a toggle/status card plus the primary-detector feed annotated
 * with validator verdicts (verified / disputed / pending) and a
 * shadow-only list for catches the primary missed.
 */
export { ValidationPage } from "./ValidationPage";
export { useDriftCount } from "./hooks/useDriftCount";
