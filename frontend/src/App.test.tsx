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
      stopProfile: vi.fn(),
      profileResources: vi.fn(),
      logout: vi.fn(),
      // ProfileForm (rendered by the "edit"/"create" views) fetches these on
      // mount regardless of what a given test is exercising.
      listExtensions: vi.fn().mockResolvedValue([]),
      getStatus: vi.fn().mockResolvedValue({
        running_count: 0, binary_version: "0.0.0-test", profiles_total: 0,
        binary_downloading: false, binary_download_percent: null,
        binary_download_state: null, default_idle_timeout_seconds: 3600,
      }),
      systemCheck: vi.fn().mockResolvedValue({
        gpu_mode: "swiftshader", binary_version: "0.0.0-test",
        license_configured: false, kasmvnc_version: "1.5.0",
        disk_total_bytes: 1, disk_used_bytes: 0, disk_free_bytes: 1,
        disk_percent_used: 0,
      }),
      // The top bar's compact CookieWarmupPanel fetches this on mount for
      // any running profile — see "cookie warmup control in the top bar"
      // below.
      cookieWarmupStatus: vi.fn().mockResolvedValue({
        state: "idle", sites_total: 20, sites_visited: 0, current_site: null,
        elapsed_seconds: null, remaining_seconds: null, error: null,
      }),
      startCookieWarmup: vi.fn(),
      stopCookieWarmup: vi.fn(),
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
    auto_restart: false,
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

describe("cookie warmup control in the top bar", () => {
  // Selecting a running profile always opens the viewer, never the edit
  // form (see App.tsx's VIEW_ON_SELECT) — so the actionable "Warm up
  // cookies" button has to live in the top bar, not inside ProfileForm,
  // or it would never be reachable for a profile it can actually run
  // against.
  it("shows Warm up cookies for a running profile", async () => {
    mockApi.listProfiles.mockResolvedValue([profile("running")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.getByRole("button", { name: /warm up cookies/i })).toBeInTheDocument();
  });

  it("does not show it for a stopped profile", async () => {
    mockApi.listProfiles.mockResolvedValue([profile("stopped")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.queryByRole("button", { name: /warm up cookies/i })).not.toBeInTheDocument();
  });
});

describe("Container self-check panel", () => {
  it("is available in the top bar with no profile selected", async () => {
    render(<App />);
    await flush();
    expect(screen.getByRole("button", { name: /container self-check/i })).toBeInTheDocument();
  });

  it("fetches and shows the check when opened", async () => {
    mockApi.systemCheck.mockResolvedValue({
      gpu_mode: "igpu", binary_version: "150.0.0", license_configured: true,
      kasmvnc_version: "1.5.0", disk_total_bytes: 100, disk_used_bytes: 10,
      disk_free_bytes: 90, disk_percent_used: 10,
    });
    render(<App />);
    await flush();

    await act(async () => {
      screen.getByRole("button", { name: /container self-check/i }).click();
    });
    await flush();

    expect(screen.getByText("150.0.0")).toBeInTheDocument();
  });
});

describe("Idle-timeout countdown", () => {
  async function selectRunningProfile() {
    mockApi.listProfiles.mockResolvedValue([profile("running")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
  }

  it("shows a countdown when the profile has idle timeout enabled", async () => {
    mockApi.profileResources.mockResolvedValue({
      cpu_percent: 1, memory_mb: 100, process_count: 2, idle_remaining_seconds: 125,
    });
    await selectRunningProfile();
    expect(screen.getByText(/auto-stop in 2m05s/)).toBeInTheDocument();
  });

  it("shows nothing when idle timeout is disabled for the profile", async () => {
    mockApi.profileResources.mockResolvedValue({
      cpu_percent: 1, memory_mb: 100, process_count: 2, idle_remaining_seconds: null,
    });
    await selectRunningProfile();
    expect(screen.queryByText(/auto-stop in/)).not.toBeInTheDocument();
  });

  it("ticks down between the 5s resource polls", async () => {
    mockApi.profileResources.mockResolvedValue({
      cpu_percent: 1, memory_mb: 100, process_count: 2, idle_remaining_seconds: 10,
    });
    await selectRunningProfile();
    expect(screen.getByText(/auto-stop in 0m10s/)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByText(/auto-stop in 0m07s/)).toBeInTheDocument();
  });

  it("disappears once the profile stops running", async () => {
    mockApi.profileResources.mockResolvedValue({
      cpu_percent: 1, memory_mb: 100, process_count: 2, idle_remaining_seconds: 30,
    });
    await selectRunningProfile();
    expect(screen.getByText(/auto-stop in/)).toBeInTheDocument();

    mockApi.listProfiles.mockResolvedValue([profile("stopped")]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    await flush();
    expect(screen.queryByText(/auto-stop in/)).not.toBeInTheDocument();
  });
});

describe("Restart button", () => {
  it("shows Restart for a running profile, not for a stopped one", async () => {
    mockApi.listProfiles.mockResolvedValue([profile("running")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();
    expect(screen.getByRole("button", { name: /^restart$/i })).toBeInTheDocument();

    mockApi.listProfiles.mockResolvedValue([profile("stopped")]);
    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });
    await flush();
    expect(screen.queryByRole("button", { name: /^restart$/i })).not.toBeInTheDocument();
  });

  it("stops then relaunches, showing one Restarting state throughout", async () => {
    mockApi.listProfiles.mockResolvedValue([profile("running")]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();

    let resolveStop: () => void = () => {};
    mockApi.stopProfile.mockImplementation(
      () => new Promise<{ ok: boolean }>((resolve) => {
        resolveStop = () => resolve({ ok: true });
      }),
    );
    mockApi.launchProfile.mockResolvedValue({ profile_id: "p1", status: "running" });
    mockApi.createViewerToken.mockResolvedValue({
      token: "tok-restart", viewer_url: "/viewer/tok-restart/", expires_in: 3600,
    });

    const clickPromise = act(async () => {
      screen.getByRole("button", { name: /^restart$/i }).click();
    });
    await flush();

    // Mid-flight: exactly one busy control, not Stop/Launch flashing too.
    expect(screen.getByRole("button", { name: /restarting/i })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^stop$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^launch$/i })).not.toBeInTheDocument();

    resolveStop();
    await clickPromise;
    await flush();

    expect(mockApi.stopProfile).toHaveBeenCalledWith("p1");
    expect(mockApi.launchProfile).toHaveBeenCalledWith("p1");
    expect(screen.getByRole("button", { name: /^restart$/i })).not.toBeDisabled();
  });

  it("headless: restarting stays on the edit form instead of opening a viewer", async () => {
    mockApi.listProfiles.mockResolvedValue([
      { ...profile("running"), headless: true, vnc_ws_port: null, cdp_url: "/api/profiles/p1/cdp" },
    ]);
    render(<App />);
    await flush();
    await act(async () => {
      screen.getByText("Test Profile").click();
    });
    await flush();

    mockApi.stopProfile.mockResolvedValue({ ok: true });
    mockApi.launchProfile.mockResolvedValue({ profile_id: "p1", status: "running" });

    await act(async () => {
      screen.getByRole("button", { name: /^restart$/i }).click();
    });
    await flush();

    expect(screen.queryByTitle("Browser session")).not.toBeInTheDocument();
    expect(mockApi.createViewerToken).not.toHaveBeenCalled();
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
