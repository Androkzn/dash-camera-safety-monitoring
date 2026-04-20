/**
 * Button — the one button primitive used everywhere.
 *
 * Variants: default | primary | danger | warning | ghost | subtle.
 * Sizes:    sm | md | lg.
 * Special:  iconOnly for square icon buttons.
 *
 * Forwards every other native <button> attribute (type, onClick,
 * disabled, aria-*, …) so callers don't lose any HTML semantics.
 */
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

import styles from "./Button.module.css";

export type ButtonVariant =
  | "default"
  | "primary"
  | "danger"
  | "warning"
  | "ghost"
  | "subtle";

export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  iconOnly?: boolean;
  leftIcon?: ReactNode;
  rightIcon?: ReactNode;
}

const variantClass: Record<ButtonVariant, string> = {
  default: "",
  primary: styles.primary ?? "",
  danger: styles.danger ?? "",
  warning: styles.warning ?? "",
  ghost: styles.ghost ?? "",
  subtle: styles.subtle ?? "",
};

const sizeClass: Record<ButtonSize, string> = {
  sm: styles.sizeSm ?? "",
  md: styles.sizeMd ?? "",
  lg: styles.sizeLg ?? "",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "default",
    size = "md",
    iconOnly,
    leftIcon,
    rightIcon,
    className,
    children,
    type = "button",
    ...rest
  },
  ref,
) {
  const cls = [
    styles.btn,
    variantClass[variant],
    sizeClass[size],
    iconOnly ? styles.iconOnly : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button ref={ref} type={type} className={cls} {...rest}>
      {leftIcon}
      {children}
      {rightIcon}
    </button>
  );
});
