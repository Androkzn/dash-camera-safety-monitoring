/**
 * Shared UI primitives barrel.
 *
 * Adding a new primitive? Re-export it here so consumers can write a
 * single import:
 *   import { Button, Card, EmptyState } from "../../shared/ui";
 */

export { Button } from "./Button";
export type { ButtonProps, ButtonVariant, ButtonSize } from "./Button";

export { Input } from "./Input";
export type { InputProps, InputSize } from "./Input";

export { Card } from "./Card";

export { Section } from "./Section";

export { Tabs } from "./Tabs";
export type { TabSpec } from "./Tabs";

export { Skeleton } from "./Skeleton";

export { EmptyState } from "./EmptyState";

export { Spinner } from "./Spinner";

export { ErrorBoundary } from "./ErrorBoundary";

export { DialogProvider, useDialog, dialog } from "./Dialog";
export type { DialogApi, AlertOptions, ConfirmOptions, DialogVariant } from "./Dialog";

export { Pill } from "./Pill";
export { Dot } from "./Dot";
export { Tag } from "./Tag";
export { RiskBadge } from "./RiskBadge";
