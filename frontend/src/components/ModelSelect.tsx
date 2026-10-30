import type { ModelChoice, ModelOption } from "../api/workspaceClient";

export function ModelSelect({ value, items, disabled, onChange, id = "modelChoice" }: {
  value: ModelChoice;
  items: ModelOption[];
  disabled: boolean;
  onChange: (value: ModelChoice) => void;
  id?: string;
}): React.JSX.Element {
  return <div className="model-select">
    <label className="sr-only" htmlFor={id}>模型</label>
    <select
      id={id}
      value={value}
      disabled={disabled || items.length === 0}
      onChange={(event) => onChange(event.target.value === "pro" ? "pro" : "flash")}
    >
      {items.length === 0 && <option value={value}>模型目录暂不可用</option>}
      {items.map(item => (
        <option key={item.id} value={item.id} disabled={!item.available}>
          {item.label}{item.available ? "" : "（未配置）"}
        </option>
      ))}
    </select>
  </div>;
}
