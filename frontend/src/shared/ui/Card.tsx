/**
 * Card — generic surface container. Composable header via `<Card.Header>`.
 *
 * Usage:
 *   <Card>
 *     <Card.Header title="Templates" actions={<Button size="sm">+</Button>} />
 *     ...body...
 *   </Card>
 */
import type { CSSProperties, HTMLAttributes, ReactNode } from "react";

import styles from "./Card.module.css";

export type CardPad = "sm" | "md" | "lg";

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  pad?: CardPad;
  elevated?: boolean;
}

interface HeaderProps {
  title?: ReactNode;
  actions?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

const padClass: Record<CardPad, string> = {
  sm: styles.padSm ?? "",
  md: styles.padMd ?? "",
  lg: styles.padLg ?? "",
};

export function Card({
  pad = "md",
  elevated,
  className,
  children,
  ...rest
}: CardProps) {
  const cls = [
    styles.card,
    padClass[pad],
    elevated ? styles.elevated : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} {...rest}>
      {children}
    </div>
  );
}

function CardHeader({ title, actions, className, style }: HeaderProps) {
  const cls = [styles.header, className ?? ""].filter(Boolean).join(" ");
  return (
    <div className={cls} style={style}>
      {typeof title === "string" ? <h3 className={styles.title}>{title}</h3> : title}
      {actions}
    </div>
  );
}

Card.Header = CardHeader;
