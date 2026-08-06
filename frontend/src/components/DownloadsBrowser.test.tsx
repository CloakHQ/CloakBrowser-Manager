import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { DownloadsBrowser } from "./DownloadsBrowser";

vi.mock("../lib/api", () => ({
  api: {
    listDownloads: vi.fn(),
    deleteDownload: vi.fn(),
  },
  downloadFileUrl: (id: string, path: string) => `/api/profiles/${id}/downloads${path}`,
  downloadsZipUrl: (id: string) => `/api/profiles/${id}/downloads-zip`,
}));

import { api } from "../lib/api";

const mockApi = api as unknown as {
  listDownloads: ReturnType<typeof vi.fn>;
  deleteDownload: ReturnType<typeof vi.fn>;
};

const windowOpenSpy = vi.fn();

beforeEach(() => {
  vi.resetAllMocks();
  windowOpenSpy.mockReset();
  vi.stubGlobal("open", windowOpenSpy);
  mockApi.listDownloads.mockResolvedValue([]);
});

describe("DownloadsBrowser bulk zip download", () => {
  it("disables Download All as ZIP when there are no files", async () => {
    mockApi.listDownloads.mockResolvedValue([]);
    await act(async () => {
      render(<DownloadsBrowser profileId="p1" />);
    });
    expect(screen.getByRole("button", { name: /download all as zip/i })).toBeDisabled();
  });

  it("enables the button once files are present", async () => {
    mockApi.listDownloads.mockResolvedValue([
      { name: "a.txt", isDirectory: false, path: "/a.txt", size: 10, updatedAt: "2026-01-01" },
    ]);
    await act(async () => {
      render(<DownloadsBrowser profileId="p1" />);
    });
    expect(screen.getByRole("button", { name: /download all as zip/i })).not.toBeDisabled();
  });

  it("opens the zip endpoint in a new tab when clicked", async () => {
    mockApi.listDownloads.mockResolvedValue([
      { name: "a.txt", isDirectory: false, path: "/a.txt", size: 10, updatedAt: "2026-01-01" },
    ]);
    await act(async () => {
      render(<DownloadsBrowser profileId="p1" />);
    });

    await act(async () => {
      screen.getByRole("button", { name: /download all as zip/i }).click();
    });
    expect(windowOpenSpy).toHaveBeenCalledWith("/api/profiles/p1/downloads-zip", "_blank");
  });

  it("stays disabled when the only entries are directories", async () => {
    mockApi.listDownloads.mockResolvedValue([
      { name: "sub", isDirectory: true, path: "/sub", size: null, updatedAt: "2026-01-01" },
    ]);
    await act(async () => {
      render(<DownloadsBrowser profileId="p1" />);
    });
    expect(screen.getByRole("button", { name: /download all as zip/i })).toBeDisabled();
  });
});
