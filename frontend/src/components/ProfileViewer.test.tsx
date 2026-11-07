import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProfileViewer } from "./ProfileViewer";

const { created, disconnect, showDotCursor } = vi.hoisted(() => ({
  created: vi.fn(),
  disconnect: vi.fn(),
  showDotCursor: vi.fn(),
}));

vi.mock("@novnc/novnc", () => ({
  default: class extends EventTarget {
    scaleViewport = false;
    resizeSession = true;
    disconnect = disconnect;

    constructor(target: HTMLElement, url: string, options: object) {
      super();
      created(this, target, url, options);
    }

    set showDotCursor(value: boolean) {
      showDotCursor(value);
    }
  },
}));

vi.mock("./ProfileFiles", () => ({
  ProfileFilesButton: () => null,
  useProfileFiles: () => ({
    files: [],
    transfers: [],
    uploading: false,
    error: null,
  }),
}));

function renderViewer() {
  return render(
    <ProfileViewer
      profileId="profile-1"
      profileName="Work"
      cdpUrl={null}
      clipboardSync={false}
      onClipboardSyncChange={vi.fn().mockResolvedValue(undefined)}
      onDisconnect={vi.fn()}
    />,
  );
}

describe("ProfileViewer noVNC integration", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("test_connects_using_the_public_novnc_export", async () => {
    renderViewer();
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    const [rfb, target, url, options] = created.mock.calls[0];
    expect(target).toBeInstanceOf(HTMLElement);
    expect(url).toBe(`ws://${window.location.host}/api/profiles/profile-1/vnc`);
    expect(options).toEqual({ wsProtocols: ["binary"] });
    expect(rfb.scaleViewport).toBe(true);
    expect(rfb.resizeSession).toBe(false);
    expect(showDotCursor).toHaveBeenCalledWith(true);
  });

  it("test_displays_the_connection_event", async () => {
    renderViewer();
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    act(() => created.mock.calls[0][0].dispatchEvent(new Event("connect")));
    expect(screen.getByText("Connected")).toBeTruthy();
  });

  it("test_disconnects_on_unmount", async () => {
    const view = renderViewer();
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    view.unmount();
    expect(disconnect).toHaveBeenCalledOnce();
  });
});
