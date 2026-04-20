/**
 * watchdog feature — public surface.
 *
 * Other features should import only from this barrel.
 */

export { WatchdogProvider, useWatchdogCtx } from "./WatchdogContext";
export { WatchdogBadge } from "./components/WatchdogBadge";
export { WatchdogDrawer } from "./components/WatchdogDrawer";
export { watchdogApi, watchdogQueryKeys } from "./api";
