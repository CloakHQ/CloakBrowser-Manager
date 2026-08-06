import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { SystemCheckPanel } from "./SystemCheckPanel";

vi.mock("../lib/api", () => ({
  api: {
    systemCheck: vi.fn(),
  },
}));

import { api } from "../lib/api";

const mockApi = api as unknown as { systemCheck: ReturnType<typeof vi.fn> };

function check(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    gpu_mode: "swiftshader",
    binary_version: "150.0.0",
    license_configured: true,
    kasmvnc_version: "1.5.0",
    disk_total_bytes: 100_000_000_000,
    disk_used_bytes: 40_000_000_000,
    disk_free_bytes: 60_000_000_000,
    disk_percent_used: 40.0,
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("SystemCheckPanel", () => {
  it("does not fetch until opened", () => {
    render(<SystemCheckPanel />);
    expect(mockApi.systemCheck).not.toHaveBeenCalled();
  });

  it("fetches and renders the check when opened", async () => {
    mockApi.systemCheck.mockResolvedValue(check());
    render(<SystemCheckPanel />);

    await act(async () => {
      screen.getByRole("button", { name: /container self-check/i }).click();
    });

    expect(mockApi.systemCheck).toHaveBeenCalledTimes(1);
    expect(screen.getByText("150.0.0")).toBeInTheDocument();
    expect(screen.getByText("Configured")).toBeInTheDocument();
    expect(screen.getByText("1.5.0")).toBeInTheDocument();
  });

  it("flags a missing license", async () => {
    mockApi.systemCheck.mockResolvedValue(check({ license_configured: false }));
    render(<SystemCheckPanel />);

    await act(async () => {
      screen.getByRole("button", { name: /container self-check/i }).click();
    });

    expect(screen.getByText("Not set (free tier)")).toBeInTheDocument();
  });

  it("shows an error message if the fetch fails", async () => {
    mockApi.systemCheck.mockRejectedValue(new Error("boom"));
    render(<SystemCheckPanel />);

    await act(async () => {
      screen.getByRole("button", { name: /container self-check/i }).click();
    });

    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("closes when the close button is clicked", async () => {
    mockApi.systemCheck.mockResolvedValue(check());
    render(<SystemCheckPanel />);

    await act(async () => {
      screen.getByRole("button", { name: /container self-check/i }).click();
    });
    expect(screen.getByText("Container Self-Check")).toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: /close/i }).click();
    });
    expect(screen.queryByText("Container Self-Check")).not.toBeInTheDocument();
  });

  it("maps each gpu_mode to a readable label", async () => {
    mockApi.systemCheck.mockResolvedValue(check({ gpu_mode: "nvidia" }));
    render(<SystemCheckPanel />);
    await act(async () => {
      screen.getByRole("button", { name: /container self-check/i }).click();
    });
    expect(screen.getByText("NVIDIA")).toBeInTheDocument();
  });
});
