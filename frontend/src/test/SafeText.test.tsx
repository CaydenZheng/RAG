import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SafeText } from "../components/SafeText";

describe("SafeText", () => {
  it("creates links only for HTTP and HTTPS protocols", () => {
    render(
      <p>
        <SafeText value="https://example.test 安全，javascript:alert(1) 保持文本" />
      </p>,
    );
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://example.test");
    expect(screen.getByText(/javascript:alert/)).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });
});
