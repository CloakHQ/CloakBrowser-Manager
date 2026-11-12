import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import type { LibraryExtension, Profile } from "../lib/api";
import { ProfileForm } from "./ProfileForm";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listExtensions: vi.fn(),
    },
  };
});

const mockApi = api as typeof api & { listExtensions: ReturnType<typeof vi.fn> };

function libraryExtension(overrides: Partial<LibraryExtension> = {}): LibraryExtension {
  return {
    id: "ddkjiahejlhfcafbddmgiahcphecmpfh",
    name: "uBlock Origin Lite",
    description: null,
    version: "2026.1.1",
    previous_version: null,
    manifest_version: 3,
    source_kind: "webstore",
    source_url: null,
    webstore_id: "ddkjiahejlhfcafbddmgiahcphecmpfh",
    icon_url: null,
    profile_count: 0,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

function profile(status: Profile["status"], overrides: Partial<Profile> = {}): Profile {
  return {
    id: "p1",
    name: "Work",
    fingerprint_seed: 1,
    proxy: null,
    timezone: null,
    locale: null,
    screen_width: 1920,
    screen_height: 1080,
    gpu_family: "auto",
    humanize: false,
    human_preset: "default",
    geoip: true,
    clipboard_sync: true,
    auto_launch: false,
    color_scheme: null,
    launch_args: [],
    extension_paths: [],
    extension_ids: [],
    allow_3p_cookies: true,
    set_google_default: true,
    capture_preview: true,
    restore_session: true,
    memory_saver: "off",
    transparent_proxy: false,
    portable_cookies: false,
    notes: null,
    tags: [],
    user_data_dir: "/data/profiles/p1",
    created_at: "",
    updated_at: "",
    status,
    ...overrides,
  } as unknown as Profile;
}

function renderForm(status: Profile["status"]) {
  const onDuplicate = vi.fn().mockResolvedValue(undefined);
  render(
    <ProfileForm
      profile={profile(status)}
      hostOs="linux"
      viewerMode="vnc"
      onSave={vi.fn()}
      onDuplicate={onDuplicate}
      onCancel={vi.fn()}
    />,
  );
  return onDuplicate;
}

beforeEach(() => {
  mockApi.listExtensions.mockResolvedValue([]);
});

describe("ProfileForm duplicate split button", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("Escape closes the menu and returns focus to the trigger", () => {
    renderForm("stopped");
    const trigger = screen.getByLabelText("Duplicate options");
    fireEvent.click(trigger);
    const item = screen.getByRole("menuitem", { name: /Settings and fingerprint only/ });
    item.focus();
    expect(document.activeElement).toBe(item);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("closes on an outside click", () => {
    renderForm("stopped");
    fireEvent.click(screen.getByLabelText("Duplicate options"));
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("offers browser state only for a stopped profile", () => {
    renderForm("running");
    fireEvent.click(screen.getByLabelText("Duplicate options"));
    const item = screen.getByRole("menuitem", { name: /With browser state/ }) as HTMLButtonElement;
    expect(item.disabled).toBe(true);
    expect(screen.getByText("Stop the profile first")).toBeTruthy();
  });

  it("the button itself is a config-only copy; the menu item asks for state", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const onDuplicate = renderForm("stopped");
    const trigger = screen.getByLabelText("Duplicate options") as HTMLButtonElement;

    fireEvent.click(screen.getByTitle("Duplicate settings and fingerprint only"));
    expect(onDuplicate).toHaveBeenLastCalledWith(false);
    // Both halves are disabled while a duplicate is in flight
    await waitFor(() => expect(trigger.disabled).toBe(false));

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: /With browser state/ }));
    expect(onDuplicate).toHaveBeenLastCalledWith(true);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("ProfileForm extension library", () => {
  it("includes a checked library extension in the saved extension_ids", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    mockApi.listExtensions.mockResolvedValue([libraryExtension()]);
    render(
      <ProfileForm
        profile={profile("stopped")}
        hostOs="linux"
        viewerMode="vnc"
        onSave={onSave}
        onManageExtensions={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const checkbox = await screen.findByRole("checkbox", { name: /uBlock Origin Lite/ });
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.lastCall?.[0].extension_ids).toEqual(["ddkjiahejlhfcafbddmgiahcphecmpfh"]);
  });

  it("shows only the selected extensions in read-only mode, without controls", async () => {
    const selected = libraryExtension();
    const other = libraryExtension({
      id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      name: "Other Extension",
    });
    mockApi.listExtensions.mockResolvedValue([selected, other]);

    render(
      <ProfileForm
        readOnly
        profile={profile("running", { extension_ids: [selected.id] })}
        hostOs="linux"
        viewerMode="vnc"
        onSave={vi.fn()}
        onManageExtensions={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(await screen.findByText("uBlock Origin Lite")).toBeTruthy();
    expect(screen.queryByText("Other Extension")).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /uBlock Origin Lite/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Manage library" })).toBeNull();
  });
});
