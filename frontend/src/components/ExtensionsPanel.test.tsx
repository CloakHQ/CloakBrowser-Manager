import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import type { LibraryExtension } from "../lib/api";
import { ExtensionsPanel } from "./ExtensionsPanel";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listExtensions: vi.fn(),
      addExtension: vi.fn(),
      uploadExtension: vi.fn(),
      updateExtension: vi.fn(),
      rollbackExtension: vi.fn(),
      deleteExtension: vi.fn(),
    },
  };
});

const mockApi = api as typeof api & {
  listExtensions: ReturnType<typeof vi.fn>;
  addExtension: ReturnType<typeof vi.fn>;
  uploadExtension: ReturnType<typeof vi.fn>;
  updateExtension: ReturnType<typeof vi.fn>;
  rollbackExtension: ReturnType<typeof vi.fn>;
  deleteExtension: ReturnType<typeof vi.fn>;
};

function extension(overrides: Partial<LibraryExtension> = {}): LibraryExtension {
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

function rowFor(name: string): HTMLElement {
  return screen.getByText(name).closest(".rounded-md") as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.listExtensions.mockResolvedValue([]);
});

describe("ExtensionsPanel", () => {
  it("adds the pasted source and shows the new row", async () => {
    const added = extension();
    const onChanged = vi.fn();
    mockApi.addExtension.mockResolvedValue(added);

    render(<ExtensionsPanel onClose={vi.fn()} onChanged={onChanged} />);
    const input = await screen.findByLabelText("Chrome Web Store link, extension ID, or CRX/ZIP URL");
    fireEvent.change(input, { target: { value: "ddkjiahejlhfcafbddmgiahcphecmpfh" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(mockApi.addExtension).toHaveBeenCalledWith("ddkjiahejlhfcafbddmgiahcphecmpfh"),
    );
    expect(await screen.findByText("uBlock Origin Lite")).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows 'Already up to date' when the update changes nothing", async () => {
    const installed = extension();
    mockApi.listExtensions.mockResolvedValue([installed]);
    mockApi.updateExtension.mockResolvedValue({ extension: installed, updated: false });

    render(<ExtensionsPanel onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Update" }));

    await waitFor(() => expect(mockApi.updateExtension).toHaveBeenCalledWith(installed.id, null));
    expect(await screen.findByText("Already up to date")).toBeTruthy();
  });

  it("flags MV2 extensions as disabled by Chromium", async () => {
    mockApi.listExtensions.mockResolvedValue([
      extension({ id: "cccccccccccccccccccccccccccccccc", name: "uBlock Origin", manifest_version: 2 }),
    ]);

    render(<ExtensionsPanel onClose={vi.fn()} onChanged={vi.fn()} />);

    expect(await screen.findByText("MV2 — disabled by Chromium")).toBeTruthy();
  });

  it("deletes only after a second click, and never an extension in use", async () => {
    const unused = extension({ id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", name: "Unused ext" });
    const used = extension({ id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", name: "Used ext", profile_count: 2 });
    mockApi.listExtensions.mockResolvedValue([unused, used]);
    mockApi.deleteExtension.mockResolvedValue({ ok: true });

    render(<ExtensionsPanel onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText("Unused ext");

    const usedDelete = within(rowFor("Used ext")).getByRole("button", {
      name: "Delete",
    }) as HTMLButtonElement;
    expect(usedDelete.disabled).toBe(true);
    expect(usedDelete.title).toContain("Used by 2 profile(s)");

    const unusedRow = rowFor("Unused ext");
    fireEvent.click(within(unusedRow).getByRole("button", { name: "Delete" }));
    expect(mockApi.deleteExtension).not.toHaveBeenCalled();

    fireEvent.click(within(unusedRow).getByRole("button", { name: "Confirm: Delete" }));

    await waitFor(() => expect(mockApi.deleteExtension).toHaveBeenCalledWith(unused.id));
    await waitFor(() => expect(screen.queryByText("Unused ext")).toBeNull());
  });

  it("rolls back to the previous version when one is kept", async () => {
    const current = extension({ version: "2.0.0", previous_version: "1.0.0" });
    mockApi.listExtensions.mockResolvedValue([current]);
    mockApi.rollbackExtension.mockResolvedValue(
      extension({ version: "1.0.0", previous_version: "2.0.0" }),
    );

    render(<ExtensionsPanel onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Roll back to 1.0.0" }));

    await waitFor(() => expect(mockApi.rollbackExtension).toHaveBeenCalledWith(current.id));
    expect(await screen.findByText("1.0.0")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Roll back to 2.0.0" })).toBeTruthy();
  });
});
