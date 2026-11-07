import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NativeWindowStatus } from "./NativeWindowStatus";


describe("NativeWindowStatus", () => {
  const writeText = vi.fn().mockResolvedValue(undefined);

  beforeEach(() => {
    writeText.mockClear();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
  });

  it("shows that the profile runs in a native window", () => {
    render(<NativeWindowStatus profileName="Work" cdpUrl={null} />);
    expect(screen.getByText("Đã mở trong cửa sổ gốc")).toBeTruthy();
    expect(screen.getByText(/Work đang chạy trực tiếp/)).toBeTruthy();
  });

  it("copies the Manager CDP endpoint", async () => {
    render(
      <NativeWindowStatus
        profileName="Work"
        cdpUrl="/api/profiles/profile-1/cdp"
      />,
    );
    fireEvent.click(screen.getByTitle("Sao chép URL CDP"));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        `${window.location.protocol}//${window.location.host}/api/profiles/profile-1/cdp`,
      );
    });
  });
});
