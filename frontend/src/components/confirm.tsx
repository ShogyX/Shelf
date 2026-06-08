// In-app confirm dialog — a styled, themed replacement for the browser's jarring window.confirm().
// Usage: const confirm = useConfirm(); … if (await confirm({ message, danger: true })) doIt();
import React, { createContext, useCallback, useContext, useRef, useState } from "react";
import { Button, Modal } from "./ui";

export interface ConfirmOptions {
  title?: React.ReactNode;
  message: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean; // red confirm button for destructive actions
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmCtx = createContext<ConfirmFn>(async () => false);

export const useConfirm = (): ConfirmFn => useContext(ConfirmCtx);

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (o) =>
      new Promise<boolean>((resolve) => {
        resolver.current = resolve;
        setOpts(o);
      }),
    []
  );

  const settle = (v: boolean) => {
    setOpts(null);
    resolver.current?.(v);
    resolver.current = null;
  };

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {opts && (
        <Modal
          title={opts.title ?? "Are you sure?"}
          onClose={() => settle(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => settle(false)}>
                {opts.cancelText ?? "Cancel"}
              </Button>
              <Button variant={opts.danger ? "danger" : "primary"} onClick={() => settle(true)} autoFocus>
                {opts.confirmText ?? (opts.danger ? "Delete" : "Confirm")}
              </Button>
            </>
          }
        >
          <div className="text-sm text-muted">{opts.message}</div>
        </Modal>
      )}
    </ConfirmCtx.Provider>
  );
}
