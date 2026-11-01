import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConfirmProvider, useConfirm } from "../components/ConfirmDialog";

function Trigger({ onResult }: { onResult: (confirmed: boolean) => void }): React.JSX.Element {
  const confirm = useConfirm();
  return <button type="button" onClick={() => {
    void confirm({ title: "删除会话", message: "此操作不能撤销。", confirmLabel: "删除", danger: true }).then(onResult);
  }}>打开</button>;
}

describe("confirm dialog", () => {
  it("asks in a dialog instead of window.confirm and resolves both outcomes", async () => {
    const results: boolean[] = [];
    render(<ConfirmProvider><Trigger onResult={(value) => results.push(value)} /></ConfirmProvider>);

    fireEvent.click(screen.getByRole("button", { name: "打开" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeVisible();
    expect(screen.getByText("此操作不能撤销。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => expect(results).toEqual([true]));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "打开" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消" }));
    await waitFor(() => expect(results).toEqual([true, false]));
  });
});
