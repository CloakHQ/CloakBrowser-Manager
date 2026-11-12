import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DangerZone } from "./DangerZone";

function renderZone() {
  const handlers = {
    onReset: vi.fn().mockResolvedValue(true),
    onClearData: vi.fn().mockResolvedValue(true),
    onDelete: vi.fn().mockResolvedValue(undefined),
  };
  const { container } = render(<DangerZone {...handlers} />);
  return { ...handlers, details: container.querySelector("details")! };
}

describe("DangerZone", () => {
  it("starts collapsed", () => {
    const { details } = renderZone();
    expect(details.open).toBe(false);
  });

  it("runs an action only after the second, confirming click", async () => {
    const { onClearData, onDelete } = renderZone();

    fireEvent.click(screen.getByRole("button", { name: "Clear browser data" }));
    expect(onClearData).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Confirm: Clear browser data" }));
    expect(onClearData).toHaveBeenCalledTimes(1);
    expect(onDelete).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("Browser data cleared")).toBeTruthy());
  });

  it("cancel backs out without running anything", () => {
    const { onDelete } = renderZone();

    fireEvent.click(screen.getByRole("button", { name: "Delete profile" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Confirm: Delete profile" })).toBeNull();
  });

  it("shows no success state when the action failed", async () => {
    const { onReset } = renderZone();
    onReset.mockResolvedValue(false);

    fireEvent.click(screen.getByRole("button", { name: "Reset identity" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm: Reset identity" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Reset identity" })).toBeTruthy());
    expect(screen.queryByText("Identity reset")).toBeNull();
  });
});
