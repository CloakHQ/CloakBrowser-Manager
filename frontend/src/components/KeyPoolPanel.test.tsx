import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LicenseKeyEntry } from "../lib/api";
import { KeyPoolPanel } from "./KeyPoolPanel";

const entry = (over: Partial<LicenseKeyEntry> = {}): LicenseKeyEntry => ({
  id: "k1",
  key_masked: "cb_aaa…aaaa",
  label: null,
  state: "valid",
  plan: "free",
  expires: null,
  added_at: "2026-10-01T00:00:00Z",
  checked_at: "2026-10-01T00:00:00Z",
  leased_to: null,
  ...over,
});

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    listKeys: vi.fn(),
    addKeys: vi.fn(),
    deleteKey: vi.fn(),
    checkKeys: vi.fn(),
  },
}));

// The panel only touches these four calls, so the whole api surface is stubbed.
vi.mock("../lib/api", () => ({ api: apiMock }));

describe("KeyPoolPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listKeys.mockResolvedValue([entry()]);
    apiMock.addKeys.mockResolvedValue([entry()]);
    apiMock.checkKeys.mockResolvedValue([]);
    apiMock.deleteKey.mockResolvedValue({ ok: true });
  });

  it("shows each pooled key masked, never the raw key", async () => {
    render(<KeyPoolPanel />);
    expect(await screen.findByText("cb_aaa…aaaa")).toBeTruthy();
    expect(screen.getByText("valid")).toBeTruthy();
  });

  it("marks a leased key as in use and refuses to delete it", async () => {
    apiMock.listKeys.mockResolvedValue([entry({ leased_to: "Work profile" })]);
    render(<KeyPoolPanel />);
    expect(await screen.findByText("in use: Work profile")).toBeTruthy();
    const remove = screen.getByTitle("Stop the profile using this key first");
    expect((remove as HTMLButtonElement).disabled).toBe(true);
  });

  it("adds every line of the textarea in one request", async () => {
    render(<KeyPoolPanel />);
    await screen.findByText("cb_aaa…aaaa");
    fireEvent.change(screen.getByPlaceholderText(/One key per line/), {
      target: { value: "cb_one\ncb_two\n" },
    });
    fireEvent.click(screen.getByText("Add keys"));
    await waitFor(() =>
      expect(apiMock.addKeys).toHaveBeenCalledWith("cb_one\ncb_two\n"),
    );
  });

  it("re-validates the whole pool and reloads the list", async () => {
    render(<KeyPoolPanel />);
    await screen.findByText("cb_aaa…aaaa");
    fireEvent.click(screen.getByText("Validate all"));
    await waitFor(() => expect(apiMock.checkKeys).toHaveBeenCalledWith());
    expect(apiMock.listKeys).toHaveBeenCalledTimes(2);
  });

  it("surfaces a delete refusal instead of dropping the row", async () => {
    apiMock.deleteKey.mockRejectedValue(
      new Error("Key is in use by running profile p1; stop it first"),
    );
    render(<KeyPoolPanel />);
    await screen.findByText("cb_aaa…aaaa");
    fireEvent.click(screen.getByTitle("Remove key"));
    expect(await screen.findByText(/stop it first/)).toBeTruthy();
  });
});
