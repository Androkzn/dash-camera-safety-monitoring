/**
 * Section — a labelled grouping inside a page or card. The header
 * uses small uppercase muted text — used for "Evidence", "What To Check",
 * "Operational" sub-blocks etc.
 */
import type { HTMLAttributes, ReactNode } from "react";

import styles from "./Section.module.css";

interface SectionProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  title?: ReactNode;
  actions?: ReactNode;
}

export function Section({ title, actions, className, children, ...rest }: SectionProps) {
  const cls = [styles.section, className ?? ""].filter(Boolean).join(" ");
  return (
    <section className={cls} {...rest}>
      {(title || actions) && (
        <header className={styles.header}>
          {typeof title === "string" ? (
            <h4 className={styles.title}>{title}</h4>
          ) : (
            title
          )}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}
