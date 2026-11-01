import { useCallback, useState } from "react";

import type { WorkspaceMode } from "../api/types";
import { isConversationActive } from "../api/types";
import type { ConversationTurn } from "./conversationState";

export interface LocalConversation {
  id: string;
  title: string;
  turns: ConversationTurn[];
  savedAt: number;
}

const EMPTY: Readonly<Record<WorkspaceMode, LocalConversation[]>> = Object.freeze({ rag: [], agent: [] });

function titleOf(turns: ConversationTurn[]): string {
  const first = turns.find((turn) => turn.question.trim());
  const text = (first?.question ?? "").trim().replace(/\s+/g, " ");
  if (!text) return "未命名对话";
  return text.length > 24 ? `${text.slice(0, 24)}…` : text;
}

/**
 * 匿名／演示模式的“最近对话”。
 * 只存在内存里：刷新即丢，不能跨标签页，也不能继续追加——切回去是只读回放。
 */
export function useLocalArchive(): {
  archive: Readonly<Record<WorkspaceMode, LocalConversation[]>>;
  archiveCurrent: (mode: WorkspaceMode, turns: ConversationTurn[]) => void;
  remove: (mode: WorkspaceMode, id: string) => void;
  rename: (mode: WorkspaceMode, id: string, title: string) => void;
} {
  const [archive, setArchive] = useState<Record<WorkspaceMode, LocalConversation[]>>({ ...EMPTY });

  const archiveCurrent = useCallback((mode: WorkspaceMode, turns: ConversationTurn[]): void => {
    if (!turns.length) return;
    const entry: LocalConversation = {
      id: crypto.randomUUID(),
      title: titleOf(turns),
      // 归档时把还在进行中的回合标记为已停止，回放里不再显示“正在准备回答”。
      turns: turns.map((turn) => ({
        ...turn,
        status: isConversationActive(turn.status) ? "cancelled" : turn.status,
      })),
      savedAt: Date.now(),
    };
    setArchive((previous) => ({ ...previous, [mode]: [entry, ...previous[mode]].slice(0, 20) }));
  }, []);

  const remove = useCallback((mode: WorkspaceMode, id: string): void => {
    setArchive((previous) => ({ ...previous, [mode]: previous[mode].filter((entry) => entry.id !== id) }));
  }, []);

  const rename = useCallback((mode: WorkspaceMode, id: string, title: string): void => {
    const next = title.trim();
    if (!next) return;
    setArchive((previous) => ({
      ...previous,
      [mode]: previous[mode].map((entry) => entry.id === id ? { ...entry, title: next } : entry),
    }));
  }, []);

  return { archive, archiveCurrent, remove, rename };
}
