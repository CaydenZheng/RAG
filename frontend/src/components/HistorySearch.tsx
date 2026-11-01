import { RotateCw, Search } from "lucide-react";
import type { RefObject } from "react";

interface HistorySearchProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onClear: () => void;
  onRefresh: () => void;
  inputRef?: RefObject<HTMLInputElement | null>;
  placeholder?: string;
  refreshLabel?: string;
  disabled?: boolean;
}

/** 真实目录与本地演示目录共用同一行搜索：输入、清除、提交、刷新。 */
export function HistorySearch({
  value,
  onChange,
  onSubmit,
  onClear,
  onRefresh,
  inputRef,
  placeholder = "搜索标题",
  refreshLabel = "刷新目录",
  disabled = false,
}: HistorySearchProps): React.JSX.Element {
  return <form
    className="history-search"
    role="search"
    onSubmit={(event) => { event.preventDefault(); onSubmit(); }}
  >
    <label className="sr-only" htmlFor="historySearch">搜索历史标题</label>
    <input
      ref={inputRef}
      id="historySearch"
      value={value}
      maxLength={80}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
      }}
    />
    {value && <button className="history-clear" type="button" aria-label="清除搜索" onClick={onClear}>✕</button>}
    <button className="history-clear" type="submit" aria-label="搜索" disabled={disabled}><Search size={13} aria-hidden="true" /></button>
    <button className="history-clear" type="button" aria-label={refreshLabel} disabled={disabled} onClick={onRefresh}><RotateCw size={13} aria-hidden="true" /></button>
  </form>;
}
