import { ArrowUp, Paperclip, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { isConversationActive, type ConversationStatus, type WorkspaceMode } from "../api/types";
import type { ModelChoice, ModelOption } from "../api/workspaceClient";
import { readSkills, type SkillItem } from "../api/workspaceClient";
import { IS_PAGES_DEMO } from "../app/runtime";
import { ModelSelect } from "./ModelSelect";

const SKILL_COMMAND = /^\/([a-z0-9-]{1,64})(?:\s+([\s\S]*))?$/;

interface SkillCommand {
  name: string;
  body: string;
}

function parseSkillCommand(value: string): SkillCommand | null {
  const match = SKILL_COMMAND.exec(value);
  if (!match) return null;
  return { name: match[1], body: (match[2] ?? "").trim() };
}

interface ComposerProps {
  mode: WorkspaceMode;
  status: ConversationStatus;
  resetting: boolean;
  unconfirmed?: boolean;
  modelUnavailable?: boolean;
  value: string;
  onValueChange: (value: string) => void;
  onSend: (message: string, skillIds?: readonly string[]) => Promise<void>;
  onStop: () => void;
  onReconcile?: () => void;
  skillsEnabled?: boolean;
  model?: ModelChoice;
  models?: ModelOption[];
  onModelChange?: (model: ModelChoice) => void;
}

export function Composer({
  mode,
  status,
  resetting,
  unconfirmed = false,
  modelUnavailable = false,
  value,
  onValueChange,
  onSend,
  onStop,
  onReconcile,
  skillsEnabled = false,
  model = "flash",
  models,
  onModelChange,
}: ComposerProps): React.JSX.Element {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const running = isConversationActive(status);
  const modelLocked = running || resetting || unconfirmed;
  const blocked = modelLocked || modelUnavailable;
  const blockedMessage = unconfirmed
    ? "本轮保存状态未确认，请先核对历史"
    : resetting ? "会话操作中，暂不可发送"
    : modelUnavailable ? "当前模型未配置，请选择可用模型" : "";
  const wasBlocked = useRef(blocked);

  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [skillsFailed, setSkillsFailed] = useState(false);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [skillIndex, setSkillIndex] = useState(0);

  // 内置技能（skill-creator）只解锁"整理为技能"，不参与普通聊天选择。
  const selectable = skills.filter((skill) => skill.enabled && !skill.builtin);
  const command = mode === "agent" ? parseSkillCommand(value) : null;
  const matched = command ? selectable.find((skill) => skill.name === command.name) ?? null : null;
  const commandUnresolved = command !== null && !matched && skillsLoaded;
  const bodyMissing = command !== null && matched !== null && !command.body;
  const commandHint = command !== null && skillsEnabled && !skillsLoaded
    ? skillsFailed ? "技能列表暂不可用，请重试加载后再发送" : "正在加载技能，请稍候"
    : bodyMissing
    ? "请补充任务内容后再发送"
    : commandUnresolved ? "所选技能不可用，请刷新技能列表后重试" : "";

  const slashToken = value.startsWith("/") ? value.slice(1) : "";
  const slashQuery = slashToken.trim().toLowerCase();
  // 选中技能后会变成 "/name "，此时收起列表，只保留一次选择机会。
  const slashOpen = mode === "agent" && skillsEnabled && !blocked
    && value.startsWith("/")
    && !/\s/.test(slashToken);
  const slashItems = selectable.filter((skill) => (
    !slashQuery
    || skill.name.toLowerCase().includes(slashQuery)
    || skill.id.toLowerCase().includes(slashQuery)
  ));

  useEffect(() => {
    const currentFocus = document.activeElement;
    const ownsFocus = currentFocus === document.body || currentFocus === null ||
      textarea.current?.closest("form")?.contains(currentFocus);
    if (wasBlocked.current && !blocked && ownsFocus) textarea.current?.focus();
    wasBlocked.current = blocked;
  }, [blocked]);

  const needsSkills = !blocked && (slashOpen || command !== null) && !skillsLoaded && !skillsFailed;
  useEffect(() => {
    if (!skillsEnabled || mode !== "agent" || !needsSkills) return undefined;
    const controller = new AbortController();
    void readSkills(controller.signal)
      .then((items) => { if (!controller.signal.aborted) { setSkills(items); setSkillsLoaded(true); } })
      .catch(() => { if (!controller.signal.aborted) setSkillsFailed(true); });
    return () => controller.abort();
  }, [mode, needsSkills, skillsEnabled]);

  useEffect(() => {
    setSkillIndex(0);
  }, [slashQuery]);

  function chooseSkill(skill: SkillItem): void {
    onValueChange(`/${skill.name} `);
    textarea.current?.focus();
  }

  const needsSkillsRetry = skillsFailed && (slashOpen || command !== null);
  const notice = blockedMessage || commandHint || (needsSkillsRetry ? "技能列表暂不可用，请重试加载" : "");
  const noticeId = blockedMessage ? "composerBlockedReason" : undefined;

  const submit = (): void => {
    if (blocked || commandHint || slashOpen) return;
    if (matched && command) {
      if (!command.body) return;
      void onSend(command.body, [matched.id]);
      return;
    }
    const message = value.trim();
    if (!message) return;
    void onSend(message);
  };

  return (
    <div className="composer-wrap">
      <form
        className="composer"
        aria-busy={blocked}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        {slashOpen && (
          <div className="slash-menu" role="listbox" aria-label="选择技能">
            <p className="slash-title">技能</p>
            {slashItems.length === 0 ? (
              <p className="slash-empty">{skillsFailed ? "技能列表暂不可用" : !skillsLoaded ? "正在加载技能…" : "没有匹配的已启用技能"}</p>
            ) : slashItems.map((skill, index) => (
              <button
                key={skill.id}
                className="slash-item"
                type="button"
                role="option"
                aria-selected={index === skillIndex}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseSkill(skill)}
              >
                <span>{skill.name}</span>
                <small>{skill.description}</small>
              </button>
            ))}
          </div>
        )}
        <label className="sr-only" htmlFor="messageInput">
          {mode === "rag" ? "向知识库提问" : "向 Agent 发送任务"}
        </label>
        <textarea
          id="messageInput"
          ref={textarea}
          rows={2}
          maxLength={2000}
          value={value}
          readOnly={blocked}
          aria-describedby={noticeId}
          placeholder={
            mode === "rag"
              ? "向知识库提问"
              : "描述需要 Agent 完成的任务，输入 / 选择技能"
          }
          onChange={(event) => onValueChange(event.target.value)}
          onKeyDown={(event) => {
            if (slashOpen && slashItems.length > 0) {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setSkillIndex((current) => (current + 1) % slashItems.length);
                return;
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setSkillIndex((current) => (current - 1 + slashItems.length) % slashItems.length);
                return;
              }
              if (event.key === "Enter") {
                event.preventDefault();
                chooseSkill(slashItems[Math.min(skillIndex, slashItems.length - 1)]);
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                onValueChange("");
                return;
              }
            }
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
            if (event.key === "Escape" && running) onStop();
          }}
        />
        <div className="composer-actions">
          <div className="composer-placeholders">
            <button type="button" className="icon-button" disabled aria-label="添加附件，即将支持">
              <Paperclip size={15} aria-hidden="true" />
            </button>
            {models && onModelChange ? (
              <ModelSelect value={model} items={models} disabled={modelLocked} onChange={onModelChange} />
            ) : (
              <button type="button" className="model-placeholder" disabled>
                默认模型 · 即将支持
              </button>
            )}
          </div>
          <span className="character-count" aria-label={`已输入 ${value.length} 个字符`}>
            {value.length}/2000
          </span>
          {running ? (
            <button className="stop-button" type="button" onClick={onStop}>
              <Square size={12} fill="currentColor" aria-hidden="true" />
              停止
            </button>
          ) : (
            <button className="send-button" type="submit" disabled={blocked || slashOpen || Boolean(commandHint) || !value.trim()}>
              <ArrowUp size={16} aria-hidden="true" />
              <span className="sr-only">{notice || "发送"}</span>
            </button>
          )}
        </div>
      </form>
      {notice && (
        <div className="composer-notice" role="status">
          <p id={noticeId}>{notice}</p>
          {needsSkillsRetry && <button type="button" disabled={blocked} onClick={() => {
            setSkillsFailed(false); textarea.current?.focus();
          }}>重试技能列表</button>}
          {unconfirmed && onReconcile && (
            <button type="button" onClick={onReconcile}>核对本轮历史</button>
          )}
        </div>
      )}
      <p className="ai-disclaimer">{IS_PAGES_DEMO ? "预设模拟内容，非真实模型回答；不执行任何工具。" : "内容由 AI 生成，请核对引用与执行结果。"}</p>
    </div>
  );
}
