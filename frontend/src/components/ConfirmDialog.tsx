import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

// 未挂 Provider 时（例如只渲染 hook 的测试）退回浏览器原生确认，行为保持一致。
const ConfirmContext = createContext<ConfirmFn>((options) => Promise.resolve(window.confirm(options.message)));

export function useConfirm(): ConfirmFn {
  return useContext(ConfirmContext);
}

interface Pending {
  options: ConfirmOptions;
  resolve: (confirmed: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }): React.JSX.Element {
  const [pending, setPending] = useState<Pending | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);

  const confirm = useCallback<ConfirmFn>((options) => new Promise<boolean>((resolve) => {
    setPending({ options, resolve });
  }), []);

  useEffect(() => {
    const element = dialog.current;
    if (!pending || !element) return undefined;
    if (!element.open) {
      // jsdom 与旧浏览器没有 showModal，退化为带 open 属性的对话框。
      if (typeof element.showModal === "function") element.showModal();
      else element.setAttribute("open", "");
    }
    // 危险操作默认聚焦“取消”，避免回车误触。
    (pending.options.danger ? cancelButton : confirmButton).current?.focus();
    return () => {
      if (!element.open) return;
      if (typeof element.close === "function") element.close();
      else element.removeAttribute("open");
    };
  }, [pending]);

  function settle(confirmed: boolean): void {
    if (!pending) return;
    pending.resolve(confirmed);
    setPending(null);
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <dialog
          ref={dialog}
          className="confirm-dialog"
          aria-labelledby="confirmTitle"
          onCancel={(event) => {
            event.preventDefault();
            settle(false);
          }}
          onClose={() => settle(false)}
          onClick={(event) => {
            if (event.target === event.currentTarget) settle(false);
          }}
        >
          <div className="confirm-body">
            <h2 id="confirmTitle">{pending.options.title}</h2>
            <p>{pending.options.message}</p>
            <div className="confirm-actions">
              <button ref={cancelButton} type="button" className="skills-action plain" onClick={() => settle(false)}>
                {pending.options.cancelLabel ?? "取消"}
              </button>
              <button
                ref={confirmButton}
                type="button"
                className={pending.options.danger ? "skills-action confirm-danger" : "skills-action"}
                onClick={() => settle(true)}
              >
                {pending.options.confirmLabel ?? "确定"}
              </button>
            </div>
          </div>
        </dialog>
      )}
    </ConfirmContext.Provider>
  );
}
