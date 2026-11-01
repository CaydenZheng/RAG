import { useEffect, useRef, useState } from "react";
import { response } from "../api/workspaceClient";
import { toPublicMessage } from "../api/errors";
import type { ConversationTurn } from "../app/conversationState";

const MAX_EXPORT_BYTES = 2 * 1024 * 1024;

export function ConversationExport({ id, disabled, turns }: {
  id: string;
  disabled: boolean;
  turns: ConversationTurn[];
}): React.JSX.Element {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const controller = useRef<AbortController | null>(null);
  const urls = useRef(new Set<string>());

  useEffect(() => () => {
    controller.current?.abort();
    for (const url of urls.current) URL.revokeObjectURL(url);
  }, []);

  const completed = turns.filter((turn) => turn.status === "completed");
  if (!completed.length) return <></>;

  async function download(): Promise<void> {
    if (disabled || busy) return;
    const operation = new AbortController();
    controller.current = operation;
    setBusy(true);
    setNotice("");
    try {
      const blob: Blob = await (await response(`/conversations/${encodeURIComponent(id)}/export`, operation.signal)).blob();
      if (operation.signal.aborted) return;
      if (blob.size > MAX_EXPORT_BYTES) {
        setNotice("此会话超过 2 MiB 导出上限。");
        return;
      }
      const url = URL.createObjectURL(blob);
      urls.current.add(url);
      const link = document.createElement("a");
      link.href = url;
      link.download = "conversation.md";
      link.click();
      setTimeout(() => {
        URL.revokeObjectURL(url);
        urls.current.delete(url);
      }, 1000);
      setNotice("已生成 Markdown 文件；仅含已完成问答。");
    } catch (error) {
      if (!operation.signal.aborted) setNotice(toPublicMessage(error));
    } finally {
      if (!operation.signal.aborted) setBusy(false);
    }
  }

  return <div className="conversation-export">
    <button className="skills-action" disabled={disabled || busy} onClick={() => void download()}>
      {busy ? "正在导出…" : "导出此会话"}
    </button>
    {notice && <span role="status">{notice}</span>}
  </div>;
}
