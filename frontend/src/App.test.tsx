import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import App from "./App";
import type { ProfileLifecycle } from "./lib/api";

vi.mock("./lib/api", () => {
  class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
      this.name = "ApiError";
    }
  }
  return {
    api: {
      authStatus: vi.fn(),
      listProfiles: vi.fn(),
      profileStatus: vi.fn(),
      createViewerToken: vi.fn(),
      launchProfile: vi.fn(),
      logout: vi.fn(),
      // ProfileForm (rendered by the "edit"/"create" views) fetches these on
      // mount regardless of what a given test is exercising.
      listExtensions: vi.fn().mockResolvedValue([]),
      getStatus: vi.fn().mockResolvedValue({
        running_count: 0, binary_version: "0.0.0-test", profiles_total: 0,
        binary_downloading: false, binary_download_percent: null,
        binary_download_state: null, default_idle_timeout_seconds: 3600,
      }),
    },
    setOnUnauthorized: vi.fn(),
    ApiError,
  };
});

import { api } from "./lib/api";

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function profile(status: ProfileLifecycle) {
  return {
    id: "p1",
    name: "Test Profile",
    fingerprint_seed: 1,
    proxy: null,
    timezone: null,
    locale: null,
    platform: "windows",
    user_agent: null,
    screen_width: 1920,
    screen_height: 1080,
    gpu_vendor: null,
    gpu_renderer: null,
    hardware_concurrency: null,
    humanize: false,
    human_preset: "default",
    headless: false,
    geoip: false,
    clipboard_sync: true,
    auto_launch: false,
    color_scheme: null,
    launch_args: [],
    notes: null,
    user_data_dir: "/data/profiles/p1",
    created_at: "",
    updated_at: "",
    tags: [],
    status,
    vnc_ws_port: status === "running" ? 6100 : null,
    cdp_url: status === "running" ? "/api/profiles/p1/cdp" : null,
  };
}

async function flush() {
  await act(async () => {});
  await act(async () => {});
  await act(async () => {});
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  mockApi.authStatus.mockResolvedValue({ auth_required: false, authenticated: true });
  mockApi.listProfiles.mockResolvedValue([profile("running")]);
  mockApi.profileStatus.mockResolvedValue({
    status: "stopped",
    xvnc_alive: null,
    browser_alive: null,
  });
  mockApi.createViewerToken.mockResolvedValue({
    token: "tok-1",
    viewer_url: "/viewer/tok-1/",
    expires_in: 3600,
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("selecting a profile routes to the pane that can actually work", () => {
  it("opens the viewer for a running profile", async () => {
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.getByTitle("Browser session")).toBeInTheDocument();
  });

  it("opens the viewer for a starting profile and waits it out", async () => {
    mockApi.listProfiles.mockResolvedValue([profile("starting")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.getByTitle("Browser session")).toBeInTheDocument();
  });

  it("opens the form, not the viewer, for a stopping profile", async () => {
    // Teardown is in flight: the manager has already dropped the profile out
    // of `running`, so /viewer-token 404s. Routing here into the viewer mounts
    // a session only to replace it with "Browser session ended" one round-trip
    // later — and burns a token request doing it.
    mockApi.listProfiles.mockResolvedValue([profile("stopping")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.queryByTitle("Browser session")).not.toBeInTheDocument();
    expect(mockApi.createViewerToken).not.toHaveBeenCalled();
    expect(screen.getByText("Save")).toBeInTheDocument();
  });

  it("opens the form for a stopped profile", async () => {
    mockApi.listProfiles.mockResolvedValue([profile("stopped")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.queryByTitle("Browser session")).not.toBeInTheDocument();
    expect(screen.getByText("Save")).toBeInTheDocument();
  });
});

describe("viewer lifecycle vs the 3s profile poll", () => {
  it("keeps the terminal overlay mounted when the poll reports the profile stopped", async () => {
    render(<App />);
    await flush();

    // open the viewer for the running profile
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    const iframe = screen.getByTitle("Browser session") as HTMLIFrameElement;
    expect(iframe).toBeInTheDocument();

    const fromClient = (value: string) => {
      const ev = new Event("message");
      Object.defineProperty(ev, "data", { value: { action: "connection_state", value } });
      Object.defineProperty(ev, "source", { value: iframe.contentWindow });
      window.dispatchEvent(ev);
    };

    await act(async () => fromClient("connected"));
    await flush();

    // the browser dies: the viewer's own machine reaches session-ended...
    await act(async () => fromClient("disconnected"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    await flush();

    // ...and the 3s poll then reports "stopped". The viewer must survive that:
    // gating its render on the polled status blanks the whole content pane.
    mockApi.listProfiles.mockResolvedValue([profile("stopped")]);
    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });
    await flush();

    expect(screen.getByText("Browser session ended")).toBeInTheDocument();
    expect(screen.getByText("Back to profile")).toBeInTheDocument();
  });

  it("relaunching from a terminal overlay starts a fresh viewer session", async () => {
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    const iframe = screen.getByTitle("Browser session") as HTMLIFrameElement;

    const fromClient = (value: string) => {
      const ev = new Event("message");
      Object.defineProperty(ev, "data", { value: { action: "connection_state", value } });
      Object.defineProperty(ev, "source", { value: iframe.contentWindow });
      window.dispatchEvent(ev);
    };
    await act(async () => fromClient("connected"));
    await flush();
    await act(async () => fromClient("disconnected"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    await flush();
    expect(screen.getByText("Browser session ended")).toBeInTheDocument();

    // the profile is now stopped, so the top bar offers Launch
    mockApi.listProfiles.mockResolvedValue([profile("stopped")]);
    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });
    await flush();

    mockApi.launchProfile.mockResolvedValue({ profile_id: "p1", status: "running" });
    mockApi.listProfiles.mockResolvedValue([profile("running")]);
    mockApi.profileStatus.mockResolvedValue({
      status: "running",
      xvnc_alive: true,
      browser_alive: true,
    });
    mockApi.createViewerToken.mockResolvedValue({
      token: "tok-2",
      viewer_url: "/viewer/tok-2/",
      expires_in: 3600,
    });
    await act(async () => {
      screen.getByText("Launch").click();
    });
    await flush();

    // a relaunch must not leave the dead overlay up
    expect(screen.queryByText("Browser session ended")).not.toBeInTheDocument();
    expect(mockApi.createViewerToken).toHaveBeenCalledTimes(2);
    expect((screen.getByTitle("Browser session") as HTMLIFrameElement).src).toContain("tok-2");
  });
});

describe("headless profiles have no viewer", () => {
  it("selecting a RUNNING headless profile opens the form, not the viewer", async () => {
    // A headless profile allocates no display and no Xvnc, so /viewer-token
    // answers 409 and the viewer could only ever render "Connection failed"
    // for a browser that is running perfectly well.
    mockApi.listProfiles.mockResolvedValue([
      { ...profile("running"), headless: true },
    ]);
    render(<App />);
    await flush();

    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();

    expect(screen.queryByTitle("Browser session")).not.toBeInTheDocument();
    expect(mockApi.createViewerToken).not.toHaveBeenCalled();
  });

  it("a headed running profile still opens the viewer", async () => {
    mockApi.listProfiles.mockResolvedValue([
      { ...profile("running"), headless: false },
    ]);
    render(<App />);
    await flush();

    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();

    expect(screen.getByTitle("Browser session")).toBeInTheDocument();
    expect(mockApi.createViewerToken).toHaveBeenCalledWith("p1");
  });
});
