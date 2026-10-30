import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface RowMenuItem {
  label: string;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

const MENU_WIDTH = 152;
const VIEWPORT_MARGIN = 8;

interface RowMenuProps {
  label: string;
  items: RowMenuItem[];
  className?: string;
  triggerData?: Record<string, string>;
}

export function RowMenu({
  label,
  items,
  className = "history-more",
  triggerData,
}: RowMenuProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event: PointerEvent): void => {
      const target = event.target as Node;
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector("button")?.focus();
  }, [open]);

  function toggle(): void {
    const rect = trigger.current?.getBoundingClientRect();
    if (rect) {
      setPosition({
        top: Math.min(rect.bottom + 6, window.innerHeight - VIEWPORT_MARGIN),
        left: Math.max(
          VIEWPORT_MARGIN,
          Math.min(rect.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - VIEWPORT_MARGIN),
        ),
      });
    }
    setOpen((value) => !value);
  }

  function select(item: RowMenuItem): void {
    setOpen(false);
    if (!item.disabled) item.onSelect();
  }

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={className}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggle}
        {...triggerData}
      >
        ⋯
      </button>
      {open && createPortal(
        <div
          ref={menu}
          className="menu-pop"
          role="menu"
          aria-label={label}
          style={{ top: position.top, left: position.left }}
        >
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className={item.danger ? "danger" : undefined}
              disabled={item.disabled}
              onClick={() => select(item)}
            >
              {item.label}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
