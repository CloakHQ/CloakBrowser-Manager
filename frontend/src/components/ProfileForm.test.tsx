import { describe, it, expect, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { ProfileForm } from "./ProfileForm";

describe("ProfileForm headless control", () => {
  it("renders a headless checkbox and submits its value", async () => {
    // The form always SUBMITTED `headless` — it just never rendered a control
    // for it, so the option was unreachable from the UI and a headless profile
    // could only be created by calling the API directly.
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ProfileForm profile={null} onSave={onSave} onCancel={() => {}} />);

    const box = screen.getByRole("checkbox", { name: /headless/i });
    expect(box).toBeInTheDocument();
    expect(box).not.toBeChecked();

    await act(async () => box.click());
    expect(box).toBeChecked();
  });

  it("reflects an existing profile's headless value when editing", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const profile = {
      id: "p1", name: "H", fingerprint_seed: 1, proxy: null, timezone: null,
      locale: null, platform: "windows", user_agent: null, screen_width: 1920,
      screen_height: 1080, gpu_vendor: null, gpu_renderer: null,
      hardware_concurrency: null, humanize: false, human_preset: "default",
      headless: true, geoip: false, clipboard_sync: true, auto_launch: false,
      color_scheme: null, launch_args: [], notes: null,
      user_data_dir: "/data/profiles/p1", created_at: "", updated_at: "",
      tags: [], status: "stopped" as const, vnc_ws_port: null, cdp_url: null,
    };
    render(<ProfileForm profile={profile} onSave={onSave} onCancel={() => {}} />);
    expect(screen.getByRole("checkbox", { name: /headless/i })).toBeChecked();
  });
});

function makeProfile(overrides: Record<string, unknown> = {}) {
  return {
    id: "p1", name: "H", fingerprint_seed: 1, proxy: null, timezone: null,
    locale: null, platform: "windows", user_agent: null, screen_width: 1920,
    screen_height: 1080, gpu_vendor: null, gpu_renderer: null,
    hardware_concurrency: null, humanize: false, human_preset: "default",
    headless: true, geoip: false, clipboard_sync: true, auto_launch: false,
    color_scheme: null, launch_args: [], notes: null,
    user_data_dir: "/data/profiles/p1", created_at: "", updated_at: "",
    tags: [], status: "stopped" as const, vnc_ws_port: null, cdp_url: null,
    ...overrides,
  };
}

describe("ProfileForm duplicate control", () => {
  it("does not render Duplicate without an onDuplicate handler", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ProfileForm profile={makeProfile()} onSave={onSave} onCancel={() => {}} />);
    expect(screen.queryByRole("button", { name: /duplicate/i })).not.toBeInTheDocument();
  });

  it("does not render Duplicate in create mode even with a handler", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onDuplicate = vi.fn().mockResolvedValue(undefined);
    render(
      <ProfileForm profile={null} onSave={onSave} onDuplicate={onDuplicate} onCancel={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: /duplicate/i })).not.toBeInTheDocument();
  });

  it("asks for confirmation before duplicating, and skips it when cancelled", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onDuplicate = vi.fn().mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      render(
        <ProfileForm profile={makeProfile()} onSave={onSave} onDuplicate={onDuplicate} onCancel={() => {}} />,
      );
      await act(async () => screen.getByRole("button", { name: /duplicate/i }).click());
      expect(confirmSpy).toHaveBeenCalledOnce();
      expect(onDuplicate).not.toHaveBeenCalled();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it("calls onDuplicate once confirmed", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onDuplicate = vi.fn().mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      render(
        <ProfileForm profile={makeProfile()} onSave={onSave} onDuplicate={onDuplicate} onCancel={() => {}} />,
      );
      await act(async () => screen.getByRole("button", { name: /duplicate/i }).click());
      expect(onDuplicate).toHaveBeenCalledTimes(1);
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it("confirmation dialog states what is copied and what is not", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onDuplicate = vi.fn().mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      render(
        <ProfileForm profile={makeProfile()} onSave={onSave} onDuplicate={onDuplicate} onCancel={() => {}} />,
      );
      await act(async () => screen.getByRole("button", { name: /duplicate/i }).click());
      const message = confirmSpy.mock.calls[0][0];
      expect(message).toMatch(/copies all settings/i);
      expect(message).toMatch(/does not copy cookies/i);
      expect(message).toMatch(/fresh random fingerprint seed/i);
    } finally {
      confirmSpy.mockRestore();
    }
  });
});
