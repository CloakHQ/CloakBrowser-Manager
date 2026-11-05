import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Profile } from "../lib/api";
import { renderWithProviders as render } from "../test/render";
import { ProfileForm } from "./ProfileForm";

function profile(status: Profile["status"]): Profile {
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
    allow_3p_cookies: true,
    set_google_default: true,
    capture_preview: true,
    restore_session: true,
    transparent_proxy: false,
    portable_cookies: false,
    notes: null,
    tags: [],
    user_data_dir: "/data/profiles/p1",
    created_at: "",
    updated_at: "",
    status,
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

describe("ProfileForm duplicate split button", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("Escape closes the menu and returns focus to the trigger", () => {
    renderForm("stopped");
    const trigger = screen.getByLabelText(/sao ch.p/i);
    fireEvent.click(trigger);
    const item = screen.getByRole("menuitem", { name: /v.n tay/i });
    item.focus();
    expect(document.activeElement).toBe(item);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("closes on an outside click", () => {
    renderForm("stopped");
    fireEvent.click(screen.getByLabelText(/sao ch.p/i));
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("offers browser state only for a stopped profile", () => {
    renderForm("running");
    fireEvent.click(screen.getByLabelText(/sao ch.p/i));
    const item = screen.getByRole("menuitem", { name: /^K.m d. li.u tr.nh duy.t/i }) as HTMLButtonElement;
    expect(item.disabled).toBe(true);
    expect(screen.getByText(/D.ng h. s./i)).toBeTruthy();
  });

  it("the button itself is a config-only copy; the menu item asks for state", async () => {
    const onDuplicate = renderForm("stopped");
    const trigger = screen.getByLabelText(/sao ch.p/i) as HTMLButtonElement;

    fireEvent.click(screen.getByTitle(/Ch. sao ch.p/i));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: /^Sao ch.p$/i }),
    );
    await waitFor(() => expect(onDuplicate).toHaveBeenLastCalledWith(false));
    // Both halves are disabled while a duplicate is in flight
    await waitFor(() => expect(trigger.disabled).toBe(false));

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: /^K.m d. li.u tr.nh duy.t/i }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: /^Sao ch.p$/i }),
    );
    await waitFor(() => expect(onDuplicate).toHaveBeenLastCalledWith(true));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
