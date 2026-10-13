import { useMemo, useState, type FormEvent } from "react";
import { ExternalLink, ShieldCheck } from "lucide-react";

import { safeExternalUrl } from "../api/safeUrls";
import { isRecord } from "../api/sse";
import type {
  AgentInteraction,
  FormElicitationInteraction,
  InteractionDecision,
} from "../api/types";
import { agentInteractionKey } from "../api/types";

interface InteractionProps {
  interaction: AgentInteraction;
  onDecision: (decision: InteractionDecision) => Promise<void>;
}

const STATUS_LABELS = Object.freeze({
  pending: "等待处理",
  submitting: "正在提交",
  approved: "已允许",
  rejected: "已拒绝",
  accepted: "已完成",
  declined: "已拒绝",
  cancelled: "已取消",
  closed: "已结束",
  outcome_unknown: "结果未知",
});

function InteractionFrame({ interaction, children }: InteractionProps & { children: React.ReactNode }) {
  const headingId = `interaction-${interaction.kind}-${interaction.id}`;
  return (
    <section className="interaction-card" aria-labelledby={headingId}>
      <div className="interaction-heading">
        <h3 id={headingId}>
          {interaction.kind === "approval" ? "需要工具审批" : "需要你的输入"}
        </h3>
        <span data-status={interaction.status}>{STATUS_LABELS[interaction.status]}</span>
      </div>
      {children}
      {interaction.error && <p className="interaction-error" role="alert">{interaction.error}</p>}
    </section>
  );
}

function ApprovalCard(props: InteractionProps & { interaction: Extract<AgentInteraction, { kind: "approval" }> }) {
  const { interaction, onDecision } = props;
  const locked = interaction.status !== "pending";
  return (
    <InteractionFrame {...props}>
      <dl className="interaction-facts">
        <div><dt>工具</dt><dd>{interaction.toolName}</dd></div>
        <div><dt>来源</dt><dd>{interaction.source}</dd></div>
        <div><dt>Provider</dt><dd>{interaction.provider ?? "未提供"}</dd></div>
        <div><dt>分类</dt><dd>{interaction.category}</dd></div>
      </dl>
      <details className="parameter-details">
        <summary>查看参数快照</summary>
        <pre>{JSON.stringify(interaction.params, null, 2)}</pre>
      </details>
      <div className="interaction-actions">
        <button type="button" disabled={locked} onClick={() => void onDecision({ kind: "approval", id: interaction.id, action: "approve" })}>允许一次</button>
        <button type="button" disabled={locked} onClick={() => void onDecision({ kind: "approval", id: interaction.id, action: "reject" })}>拒绝</button>
        <button className="danger-action" type="button" disabled={locked} onClick={() => void onDecision({ kind: "approval", id: interaction.id, action: "cancel" })}>取消任务</button>
      </div>
    </InteractionFrame>
  );
}

interface FormField {
  name: string;
  title: string;
  kind: "string" | "integer" | "number" | "boolean" | "enum" | "string-array";
  required: boolean;
  options: string[];
}

function formFields(interaction: FormElicitationInteraction): FormField[] | null {
  const schema = interaction.requestedSchema;
  if (schema.type !== "object" || !isRecord(schema.properties)) return null;
  const required = new Set(Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === "string") : []);
  const fields: FormField[] = [];
  for (const [name, rawProperty] of Object.entries(schema.properties)) {
    if (!isRecord(rawProperty)) return null;
    const title = typeof rawProperty.title === "string" && rawProperty.title ? rawProperty.title : name;
    const options = Array.isArray(rawProperty.enum)
      ? rawProperty.enum.filter((item): item is string => typeof item === "string")
      : [];
    let kind: FormField["kind"] | null = null;
    if (rawProperty.type === "string") kind = options.length ? "enum" : "string";
    if (rawProperty.type === "integer") kind = "integer";
    if (rawProperty.type === "number") kind = "number";
    if (rawProperty.type === "boolean") kind = "boolean";
    if (rawProperty.type === "array" && isRecord(rawProperty.items) && rawProperty.items.type === "string") kind = "string-array";
    if (!kind) return null;
    fields.push({ name, title, kind, required: required.has(name), options });
  }
  return fields;
}

function FormCard(props: InteractionProps & { interaction: FormElicitationInteraction }) {
  const { interaction, onDecision } = props;
  const fields = useMemo(() => formFields(interaction), [interaction]);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [validation, setValidation] = useState<Record<string, string>>({});
  const locked = interaction.status !== "pending";

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!fields || locked) return;
    const errors: Record<string, string> = {};
    const content: Record<string, string | number | boolean | string[] | null> = {};
    for (const field of fields) {
      const raw = values[field.name];
      if (field.kind === "boolean") {
        content[field.name] = raw === true;
        continue;
      }
      const text = typeof raw === "string" ? raw.trim() : "";
      if (field.required && !text) {
        errors[field.name] = "此项为必填项。";
        continue;
      }
      if (!text) continue;
      if (field.kind === "integer" || field.kind === "number") {
        const number = Number(text);
        if (!Number.isFinite(number) || (field.kind === "integer" && !Number.isInteger(number))) {
          errors[field.name] = field.kind === "integer" ? "请输入整数。" : "请输入有效数字。";
        } else {
          content[field.name] = number;
        }
      } else if (field.kind === "string-array") {
        content[field.name] = text.split(",").map((item) => item.trim()).filter(Boolean);
      } else {
        content[field.name] = text;
      }
    }
    setValidation(errors);
    if (Object.keys(errors).length === 0) {
      void onDecision({ kind: "form", id: interaction.id, action: "accept", content });
    }
  }

  return (
    <InteractionFrame {...props}>
      <p>{interaction.message}</p>
      <p className="interaction-server">来源：{interaction.serverId}</p>
      {!fields ? (
        <p className="interaction-error" role="alert">此表单包含当前版本不支持的字段，请选择拒绝或取消。</p>
      ) : (
        <form className="elicitation-form" onSubmit={submit} noValidate>
          {Object.keys(validation).length > 0 && (
            <p className="interaction-error" role="alert">请检查表单中标出的字段。</p>
          )}
          {fields.map((field) => {
            const errorId = `${interaction.id}-${field.name}-error`;
            const inputId = `${interaction.id}-${field.name}`;
            return (
              <div className="elicitation-field" key={field.name}>
                <label htmlFor={inputId}>{field.title}{field.required && <span aria-hidden="true"> *</span>}</label>
                {field.kind === "boolean" ? (
                  <input
                    id={inputId}
                    type="checkbox"
                    checked={values[field.name] === true}
                    disabled={locked}
                    onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.checked }))}
                  />
                ) : field.kind === "enum" ? (
                  <select
                    id={inputId}
                    value={typeof values[field.name] === "string" ? values[field.name] as string : ""}
                    disabled={locked}
                    aria-invalid={validation[field.name] ? "true" : undefined}
                    aria-describedby={validation[field.name] ? errorId : undefined}
                    onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
                  >
                    <option value="">请选择</option>
                    {field.options.map((option) => <option key={option} value={option}>{option}</option>)}
                  </select>
                ) : (
                  <input
                    id={inputId}
                    type={field.kind === "integer" || field.kind === "number" ? "number" : "text"}
                    step={field.kind === "integer" ? "1" : field.kind === "number" ? "any" : undefined}
                    value={typeof values[field.name] === "string" ? values[field.name] as string : ""}
                    disabled={locked}
                    aria-invalid={validation[field.name] ? "true" : undefined}
                    aria-describedby={validation[field.name] ? errorId : undefined}
                    onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
                  />
                )}
                {validation[field.name] && <span className="field-error" id={errorId}>{validation[field.name]}</span>}
              </div>
            );
          })}
          <div className="interaction-actions">
            <button type="submit" disabled={locked || !fields}>提交并继续</button>
            <button type="button" disabled={locked} onClick={() => void onDecision({ kind: "form", id: interaction.id, action: "decline" })}>拒绝</button>
            <button className="danger-action" type="button" disabled={locked} onClick={() => void onDecision({ kind: "form", id: interaction.id, action: "cancel" })}>取消任务</button>
          </div>
        </form>
      )}
      {!fields && (
        <div className="interaction-actions">
          <button type="button" disabled={locked} onClick={() => void onDecision({ kind: "form", id: interaction.id, action: "decline" })}>拒绝</button>
          <button className="danger-action" type="button" disabled={locked} onClick={() => void onDecision({ kind: "form", id: interaction.id, action: "cancel" })}>取消任务</button>
        </div>
      )}
    </InteractionFrame>
  );
}

function UrlCard(props: InteractionProps & { interaction: Extract<AgentInteraction, { kind: "url" }> }) {
  const { interaction, onDecision } = props;
  const safeUrl = safeExternalUrl(interaction.url);
  const locked = interaction.status !== "pending";
  return (
    <InteractionFrame {...props}>
      <p>请核对目标站点后再打开外部页面，完成操作后返回此处继续。</p>
      <dl className="interaction-facts">
        <div><dt>来源</dt><dd>{interaction.serverId}</dd></div>
        <div><dt>目标站点</dt><dd>{safeUrl?.hostname ?? "不支持的地址"}</dd></div>
      </dl>
      {!safeUrl && <p className="interaction-error" role="alert">该地址不符合安全访问规则，无法打开。</p>}
      <div className="interaction-actions">
        <button
          type="button"
          disabled={locked || !safeUrl}
          onClick={() => { if (safeUrl) window.open(safeUrl.href, "_blank", "noopener,noreferrer"); }}
        >
          <ExternalLink size={16} aria-hidden="true" />打开安全页面
        </button>
        <button type="button" disabled={locked} onClick={() => void onDecision({ kind: "url", id: interaction.id, action: "accept" })}>已完成，继续</button>
        <button type="button" disabled={locked} onClick={() => void onDecision({ kind: "url", id: interaction.id, action: "decline" })}>拒绝</button>
        <button className="danger-action" type="button" disabled={locked} onClick={() => void onDecision({ kind: "url", id: interaction.id, action: "cancel" })}>取消任务</button>
      </div>
    </InteractionFrame>
  );
}

export function AgentInteractions({ interactions, onDecision }: {
  interactions: AgentInteraction[];
  onDecision: (decision: InteractionDecision) => Promise<void>;
}) {
  if (!interactions.length) return null;
  return (
    <div className="agent-interactions" aria-label="Agent 待处理请求">
      <div className="interaction-intro"><ShieldCheck size={18} aria-hidden="true" />所有工具仍由后端策略、Schema、审批与审计链控制。</div>
      {interactions.map((interaction) => {
        const props = { interaction, onDecision };
        const key = agentInteractionKey(interaction);
        if (interaction.kind === "approval") return <ApprovalCard key={key} {...props} interaction={interaction} />;
        if (interaction.kind === "form") return <FormCard key={key} {...props} interaction={interaction} />;
        return <UrlCard key={key} {...props} interaction={interaction} />;
      })}
    </div>
  );
}
