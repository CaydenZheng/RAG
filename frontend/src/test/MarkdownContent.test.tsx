import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownContent } from "../components/MarkdownContent";

describe("MarkdownContent", () => {
  it("renders Markdown without raw HTML or unsafe links", () => {
    const { container } = render(
      <MarkdownContent
        value={'**重点** [安全链接](https://example.test) [危险链接](javascript:alert(1)) <script data-secret="no">alert(1)</script>'}
      />,
    );

    expect(screen.getByText("重点").tagName).toBe("STRONG");
    expect(screen.getByRole("link", { name: "安全链接" })).toHaveAttribute(
      "href",
      "https://example.test",
    );
    expect(screen.getByRole("link", { name: "安全链接" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.queryByRole("link", { name: "危险链接" })).toBeNull();
    expect(container).toHaveTextContent("危险链接");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[node]")).toBeNull();
    expect(container.innerHTML).not.toContain("data-secret");
  });
});
