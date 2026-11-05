import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProfileFile } from "../lib/api";
import { renderWithProviders as render } from "../test/render";
import { ProfileFilesButton, formatSize } from "./ProfileFiles";

const file = (over: Partial<ProfileFile> = {}): ProfileFile => ({
  id: "a1", profile_id: "p1", name: "weekly report.csv", size: 2048,
  kind: "upload", state: "ready", content_type: "text/csv",
  created_at: "2026-09-12T00:00:00Z", container_path: "/data/artifacts/p1/a1/weekly report.csv",
  ...over,
});

function renderButton(over: Partial<Parameters<typeof ProfileFilesButton>[0]> = {}) {
  const onUpload = vi.fn().mockResolvedValue(undefined);
  const onRemove = vi.fn().mockResolvedValue(undefined);
  render(
    <ProfileFilesButton
      profileId="p1"
      profileName="Work profile"
      files={[file()]}
      uploading={false}
      error={null}
      onUpload={onUpload}
      onRemove={onRemove}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
      onClearError={vi.fn()}
      {...over}
    />,
  );
  return { onUpload, onRemove };
}

describe("formatSize", () => {
  it.each([[512, "512 B"], [2048, "2.0 KB"], [5 * 1024 * 1024, "5.0 MB"], [20 * 1024, "20 KB"]])(
    "%i -> %s", (bytes, expected) => expect(formatSize(bytes as number)).toBe(expected),
  );
});

describe("ProfileFilesButton", () => {
  it("shows how many files the profile has", () => {
    renderButton();
    expect(screen.getByText("1")).toBeTruthy();
  });

  it("lists the files with their real names once opened", () => {
    renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(screen.getByText("weekly report.csv")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
    // The hint names the sidebar entry the page's own file dialog will show.
    expect(screen.getByText(/Uploads — Work profile/)).toBeTruthy();
  });

  it("uploads every chosen file, not just the first", () => {
    const { onUpload } = renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    const a = new File([new Uint8Array([1])], "a.csv");
    const b = new File([new Uint8Array([2])], "b.csv");
    fireEvent.change(screen.getByTestId("profile-file-input"), { target: { files: [a, b] } });
    expect(onUpload).toHaveBeenCalledWith([a, b]);
  });

  it("names itself for a screen reader without collapsing to the badge count", () => {
    renderButton();
    expect(screen.getByLabelText("Tệp của hồ sơ, 1 tệp")).toBeTruthy();
  });

  it("refreshes as soon as the panel opens instead of waiting for the poll", () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    renderButton({ onRefresh });
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(onRefresh).toHaveBeenCalled();
  });

  it("uploads the chosen file", () => {
    const { onUpload } = renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    const chosen = new File([new Uint8Array([1])], "menu.csv", { type: "text/csv" });
    fireEvent.change(screen.getByTestId("profile-file-input"), { target: { files: [chosen] } });
    expect(onUpload).toHaveBeenCalledWith([chosen]);
  });

  it("removes a file", () => {
    const { onRemove } = renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    fireEvent.click(screen.getByLabelText("Xóa weekly report.csv"));
    expect(onRemove).toHaveBeenCalledWith("a1");
  });

  it("offers a download link straight to the bytes", () => {
    renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(screen.getByTitle("Tải về máy này").getAttribute("href")).toBe(
      "/api/profiles/p1/files/a1",
    );
  });

  it("says what to do when the profile has no files", () => {
    renderButton({ files: [] });
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(screen.getByText(/kéo thả vào màn hình hoặc tải xuống trong trình duyệt/)).toBeTruthy();
  });

  it("Escape closes the panel and returns focus to the trigger", () => {
    renderButton();
    const trigger = screen.getByTitle("Các tệp của hồ sơ này");
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("shows a download still in flight, with no way to fetch it yet", () => {
    renderButton({ files: [file({ kind: "download", state: "pending", size: 0, name: "export.xlsx" })] });
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(screen.getByText("Đang tải xuống...")).toBeTruthy();
    expect(screen.queryByTitle("Tải về máy này")).toBeNull();
    expect(screen.getByLabelText("Trình duyệt đã tải xuống")).toBeTruthy();
  });

  it("shows a failed download", () => {
    renderButton({ files: [file({ kind: "download", state: "failed" })] });
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(screen.getByText("Tải xuống thất bại")).toBeTruthy();
    expect(screen.queryByTitle("Tải về máy này")).toBeNull();
  });

  it("marks an uploaded file differently from a downloaded one", () => {
    renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    expect(screen.getByLabelText("Đã tải lên")).toBeTruthy();
  });

describe("panel steadiness", () => {
  it("gives its controls a real hit target, not a 14px one", () => {
    renderButton();
    fireEvent.click(screen.getByTitle("Các tệp của hồ sơ này"));
    for (const label of ["Đóng", "Xóa weekly report.csv"]) {
      expect(screen.getByLabelText(label).className).toContain("p-1");
    }
  });

  it("Close returns focus to the trigger, like Escape does", () => {
    renderButton();
    const trigger = screen.getByTitle("Các tệp của hồ sơ này");
    fireEvent.click(trigger);
    fireEvent.click(screen.getByLabelText("Đóng"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});
});
