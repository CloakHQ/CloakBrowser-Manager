import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { RestartButton } from "./RestartButton";

describe("RestartButton", () => {
  it("renders idle state and calls onClick", () => {
    const onClick = vi.fn();
    render(<RestartButton busy={false} onClick={onClick} />);
    const button = screen.getByRole("button", { name: /^restart$/i });
    expect(button).not.toBeDisabled();
    button.click();
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("renders a disabled busy state while restarting", () => {
    const onClick = vi.fn();
    render(<RestartButton busy onClick={onClick} />);
    const button = screen.getByRole("button", { name: /restarting/i });
    expect(button).toBeDisabled();
  });
});
