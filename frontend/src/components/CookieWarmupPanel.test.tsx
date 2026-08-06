import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { CookieWarmupPanel } from "./CookieWarmupPanel";

vi.mock("../lib/api", () => ({
  api: {
    cookieWarmupStatus: vi.fn(),
    startCookieWarmup: vi.fn(),
    stopCookieWarmup: vi.fn(),
  },
}));

import { api } from "../lib/api";

const mockApi = api as unknown as {
  cookieWarmupStatus: ReturnType<typeof vi.fn>;
  startCookieWarmup: ReturnType<typeof vi.fn>;
  stopCookieWarmup: ReturnType<typeof vi.fn>;
};

function status(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    state: "idle",
    sites_total: 20,
    sites_visited: 0,
    current_site: null,
    elapsed_seconds: null,
    remaining_seconds: null,
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  mockApi.cookieWarmupStatus.mockResolvedValue(status());
});

describe("CookieWarmupPanel", () => {
  it("prompts to launch the profile first when it is not running", () => {
    render(<CookieWarmupPanel profileId="p1" isRunning={false} />);
    expect(screen.getByText(/launch this profile first/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /warm up cookies/i })).not.toBeInTheDocument();
  });

  it("shows a Warm up cookies button when idle and running", async () => {
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning />);
    });
    expect(screen.getByRole("button", { name: /warm up cookies/i })).toBeInTheDocument();
  });

  it("clicking Warm up cookies calls the start API", async () => {
    mockApi.startCookieWarmup.mockResolvedValue(status({ state: "idle" }));
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning />);
    });

    await act(async () => {
      screen.getByRole("button", { name: /warm up cookies/i }).click();
    });
    expect(mockApi.startCookieWarmup).toHaveBeenCalledWith("p1");
  });

  it("shows progress and a Stop button while running", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(
      status({ state: "running", sites_visited: 3, remaining_seconds: 300, current_site: "https://www.google.com" }),
    );
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning />);
    });

    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
    expect(screen.getByText(/3\/20 sites/)).toBeInTheDocument();
    expect(screen.getByText(/5m00s remaining/)).toBeInTheDocument();
    expect(screen.getByText(/visiting www\.google\.com/)).toBeInTheDocument();
  });

  it("clicking Stop calls the stop API", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "running", sites_visited: 3 }));
    mockApi.stopCookieWarmup.mockResolvedValue(status({ state: "cancelled", sites_visited: 3 }));
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning />);
    });

    await act(async () => {
      screen.getByRole("button", { name: /stop/i }).click();
    });
    expect(mockApi.stopCookieWarmup).toHaveBeenCalledWith("p1");
  });

  it("shows a done summary once finished", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "done", sites_visited: 20 }));
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning />);
    });
    expect(screen.getByText(/done — visited 20 sites/i)).toBeInTheDocument();
  });

  it("shows an error message on failure", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "error", error: "context is already closed" }));
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning />);
    });
    expect(screen.getByText(/failed: context is already closed/i)).toBeInTheDocument();
  });
});

describe("CookieWarmupPanel compact mode", () => {
  it("renders nothing when not running — no dangling hint in a tight top bar", () => {
    const { container } = render(<CookieWarmupPanel profileId="p1" isRunning={false} compact />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an icon-only Warm up cookies button when idle and running", async () => {
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning compact />);
    });
    const button = screen.getByRole("button", { name: /warm up cookies/i });
    expect(button).toBeInTheDocument();
    expect(button).not.toHaveTextContent("Warm up cookies"); // icon only, per compact
  });

  it("shows a short progress readout while running", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(
      status({ state: "running", sites_visited: 4, sites_total: 20, remaining_seconds: 125 }),
    );
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning compact />);
    });
    expect(screen.getByText(/4\/20 sites/)).toBeInTheDocument();
    expect(screen.getByText(/2m05s remaining/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop cookie warmup/i })).toBeInTheDocument();
  });

  it("shows a terse done label instead of the full sentence", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "done", sites_visited: 20 }));
    await act(async () => {
      render(<CookieWarmupPanel profileId="p1" isRunning compact />);
    });
    expect(screen.getByText("Warmed up")).toBeInTheDocument();
    expect(screen.queryByText(/visited 20 sites/)).not.toBeInTheDocument();
  });
});
