/**
 * Dialog — themed alert / confirm dialogs that replace the native
 * `window.alert` / `window.confirm` browser modals.
 *
 * Usage:
 * 1. Mount `<DialogProvider>` once near the root (already wired into
 *    `app/providers.tsx`).
 *
 * 2. From a component:
 *      const dialog = useDialog();
 *      const ok = await dialog.confirm({
 *        title: "Delete template?",
 *        message: `This will soft-delete "${name}".`,
 *        variant: "danger",
 *      });
 *
 * 3. From a non-component (helper function, error handler):
 *      import { dialog } from "@/shared/ui";
 *      await dialog.alert({ message: "Token rejected." });
 *
 * Both APIs return promises so callers can `await` the operator's
 * choice. Built on the native <dialog> element for focus management,
 * Esc-to-cancel, and a11y baked in.
 */

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import styles from "./Dialog.module.css";

export type DialogVariant = "info" | "warning" | "danger";

export interface AlertOptions {
  title?: string;
  message: ReactNode;
  okLabel?: string;
  variant?: DialogVariant;
}

export interface ConfirmOptions extends AlertOptions {
  cancelLabel?: string;
}

export interface PromptOptions extends ConfirmOptions {
  /** Placeholder shown in the empty input. */
  placeholder?: string;
  /** Pre-filled value when the dialog opens. */
  initialValue?: string;
  /** HTML input type — use "password" to mask tokens/secrets. */
  inputType?: "text" | "password";
}

export interface DialogApi {
  alert: (opts: AlertOptions) => Promise<void>;
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  /** Resolves with the entered string on OK, or ``null`` on cancel/Esc. */
  prompt: (opts: PromptOptions) => Promise<string | null>;
}

let _dialogApi: DialogApi | null = null;

export const dialog: DialogApi = {
  alert: (opts) => {
    if (!_dialogApi) {
      // eslint-disable-next-line no-alert
      window.alert(typeof opts.message === "string" ? opts.message : opts.title ?? "");
      return Promise.resolve();
    }
    return _dialogApi.alert(opts);
  },
  confirm: (opts) => {
    if (!_dialogApi) {
      // eslint-disable-next-line no-alert
      const ok = window.confirm(
        typeof opts.message === "string" ? opts.message : opts.title ?? "",
      );
      return Promise.resolve(ok);
    }
    return _dialogApi.confirm(opts);
  },
  prompt: (opts) => {
    if (!_dialogApi) {
      // eslint-disable-next-line no-alert
      const v = window.prompt(
        typeof opts.message === "string" ? opts.message : opts.title ?? "",
        opts.initialValue ?? "",
      );
      return Promise.resolve(v);
    }
    return _dialogApi.prompt(opts);
  },
};

const DialogContext = createContext<DialogApi | null>(null);

export function useDialog(): DialogApi {
  const ctx = useContext(DialogContext);
  if (ctx) return ctx;
  return dialog;
}

type AlertEntry = {
  id: number;
  kind: "alert";
  opts: AlertOptions;
  resolve: () => void;
};
type ConfirmEntry = {
  id: number;
  kind: "confirm";
  opts: ConfirmOptions;
  resolve: (value: boolean) => void;
};
type PromptEntry = {
  id: number;
  kind: "prompt";
  opts: PromptOptions;
  resolve: (value: string | null) => void;
};
type QueueEntry = AlertEntry | ConfirmEntry | PromptEntry;

let _entryCounter = 0;

export function DialogProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<QueueEntry[]>([]);
  const [promptValue, setPromptValue] = useState<string>("");
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const api = useMemo<DialogApi>(
    () => ({
      alert: (opts) =>
        new Promise<void>((resolve) => {
          setQueue((q) => [
            ...q,
            { id: ++_entryCounter, kind: "alert", opts, resolve: () => resolve() },
          ]);
        }),
      confirm: (opts) =>
        new Promise<boolean>((resolve) => {
          setQueue((q) => [
            ...q,
            { id: ++_entryCounter, kind: "confirm", opts, resolve },
          ]);
        }),
      prompt: (opts) =>
        new Promise<string | null>((resolve) => {
          setQueue((q) => [
            ...q,
            { id: ++_entryCounter, kind: "prompt", opts, resolve },
          ]);
        }),
    }),
    [],
  );

  useEffect(() => {
    _dialogApi = api;
    return () => {
      if (_dialogApi === api) _dialogApi = null;
    };
  }, [api]);

  const current = queue[0];

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (current && !el.open) el.showModal();
    if (!current && el.open) el.close();
    if (current?.kind === "prompt") {
      setPromptValue(current.opts.initialValue ?? "");
      // Autofocus the input after the native <dialog> is shown.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [current]);

  const dismiss = useCallback(
    (confirmed: boolean) => {
      const entry = queue[0];
      if (!entry) return;
      if (entry.kind === "alert") {
        entry.resolve();
      } else if (entry.kind === "confirm") {
        entry.resolve(confirmed);
      } else {
        entry.resolve(confirmed ? promptValue : null);
      }
      setQueue((q) => q.slice(1));
    },
    [queue, promptValue],
  );

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    const onCancel = (e: Event) => {
      e.preventDefault();
      dismiss(false);
    };
    el.addEventListener("cancel", onCancel);
    return () => el.removeEventListener("cancel", onCancel);
  }, [dismiss]);

  const variant = current?.opts.variant ?? "info";
  const variantClass =
    variant === "danger"
      ? styles.variantDanger
      : variant === "warning"
        ? styles.variantWarning
        : styles.variantInfo;

  return (
    <DialogContext.Provider value={api}>
      {children}
      <dialog ref={dialogRef} className={`${styles.dialog} ${variantClass}`}>
        {current && (
          <form
            method="dialog"
            onSubmit={(e) => {
              e.preventDefault();
              dismiss(true);
            }}
          >
            {current.opts.title && (
              <h2 className={styles.title}>{current.opts.title}</h2>
            )}
            <div className={styles.body}>
              {typeof current.opts.message === "string" ? (
                <p>{current.opts.message}</p>
              ) : (
                current.opts.message
              )}
              {current.kind === "prompt" && (
                <input
                  ref={inputRef}
                  className={styles.input}
                  type={current.opts.inputType ?? "text"}
                  placeholder={current.opts.placeholder}
                  value={promptValue}
                  onChange={(e) => setPromptValue(e.target.value)}
                />
              )}
            </div>
            <div className={styles.actions}>
              {(current.kind === "confirm" || current.kind === "prompt") && (
                <button
                  type="button"
                  className={styles.btnGhost}
                  onClick={() => dismiss(false)}
                >
                  {current.opts.cancelLabel ?? "Cancel"}
                </button>
              )}
              <button
                type="submit"
                autoFocus={current.kind !== "prompt"}
                className={
                  variant === "danger"
                    ? styles.btnDanger
                    : variant === "warning"
                      ? styles.btnWarning
                      : styles.btnPrimary
                }
                onClick={() => dismiss(true)}
              >
                {current.opts.okLabel ?? (current.kind === "alert" ? "Got it" : "OK")}
              </button>
            </div>
          </form>
        )}
      </dialog>
    </DialogContext.Provider>
  );
}
