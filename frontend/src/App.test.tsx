import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { api } from "./lib/api";
import { useProfiles } from "./hooks/useProfiles";

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      authStatus: vi.fn(),
      getConfig: vi.fn(),
      getStatus: vi.fn(),
      checkUpdate: vi.fn(),
      logout: vi.fn(),
    },
    setOnUnauthorized: vi.fn(),
  };
});

vi.mock("./hooks/useProfiles", () => ({
  useProfiles: vi.fn(),
}));

const mockApi = api as typeof api & {
  authStatus: ReturnType<typeof vi.fn>;
  getConfig: ReturnType<typeof vi.fn>;
  getStatus: ReturnType<typeof vi.fn>;
  checkUpdate: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};
const mockUseProfiles = useProfiles as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockApi.authStatus.mockResolvedValue({ auth_required: false, authenticated: false });
  mockApi.getConfig.mockResolvedValue({ sidebar_width: "20rem" });
  mockApi.getStatus.mockRejectedValue(new Error("not available in unit test"));
  mockApi.checkUpdate.mockRejectedValue(new Error("not available in unit test"));
  mockApi.logout.mockResolvedValue({ ok: true });
  mockUseProfiles.mockReturnValue({
    profiles: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    reorder: vi.fn(),
    launch: vi.fn(),
    stop: vi.fn(),
    reset: vi.fn(),
    duplicate: vi.fn(),
  });
});

describe("App", () => {
  it("applies the configured sidebar width", async () => {
    render(<App />);

    const sidebar = await screen.findByLabelText("Profile sidebar");

    await waitFor(() => expect(mockApi.getConfig).toHaveBeenCalled());
    await waitFor(() => expect(sidebar.getAttribute("style")).toContain("width: 20rem"));
  });
});
