import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { ProfileViewer } from "./ProfileViewer";

vi.mock("../lib/api", () => ({
  api: {
    getClipboard: vi.fn(),
    setClipboard: vi.fn(),
    toggleKeePassXCWindow: vi.fn(),
  },
}));

vi.mock("@novnc/novnc/core/rfb.js", () => ({
  default: class MockRFB {
    scaleViewport = false;
    resizeSession = false;
    showDotCursor = false;

    addEventListener(type: string, listener: () => void) {
      if (type === "connect") {
        queueMicrotask(listener);
      }
    }

    removeEventListener() {}
    disconnect() {}
  },
}));

const mockedApi = api as {
  getClipboard: ReturnType<typeof vi.fn>;
  setClipboard: ReturnType<typeof vi.fn>;
  toggleKeePassXCWindow: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  mockedApi.getClipboard.mockResolvedValue({ text: "" });
  mockedApi.setClipboard.mockResolvedValue({ ok: true });
  mockedApi.toggleKeePassXCWindow.mockResolvedValue({ state: "shown" });
});

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("ProfileViewer recovery and navigation", () => {
  it("returns to settings without stopping the browser", () => {
    const onDisconnect = vi.fn();
    render(<ProfileViewer profileId="one" cdpUrl={null} clipboardSync={false} keepassxcEnabled={false} onDisconnect={onDisconnect} />);
    fireEvent.click(screen.getByLabelText("Back to profile settings"));
    expect(onDisconnect).toHaveBeenCalledOnce();
  });

  it("keeps polling after a transient clipboard failure", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderViewer(false);
    const toggle = screen.getByTitle("Enable clipboard sync") as HTMLButtonElement;
    await waitFor(() => expect(toggle.disabled).toBe(false));
    mockedApi.getClipboard.mockRejectedValueOnce(new Error("temporary failure")).mockResolvedValue({ text: "recovered" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    fireEvent.click(toggle);
    await vi.advanceTimersByTimeAsync(2000);
    expect(mockedApi.getClipboard).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(mockedApi.getClipboard).toHaveBeenCalledTimes(2);
    expect(writeText).toHaveBeenCalledWith("recovered");
  });
});

function renderViewer(keepassxcEnabled: boolean) {
  return render(
    <ProfileViewer
      profileId="profile-1"
      cdpUrl={null}
      clipboardSync={false}
      keepassxcEnabled={keepassxcEnabled}
      onDisconnect={vi.fn()}
    />,
  );
}

describe("ProfileViewer KeePassXC window action", () => {
  it("hides the button when KeePassXC is not enabled", () => {
    renderViewer(false);

    expect(screen.queryByLabelText("Toggle KeePassXC window")).toBeNull();
  });

  it("toggles the KeePassXC window for an enabled profile", async () => {
    renderViewer(true);
    const button = screen.getByLabelText("Toggle KeePassXC window") as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));

    fireEvent.click(button);

    await waitFor(() => {
      expect(mockedApi.toggleKeePassXCWindow).toHaveBeenCalledWith("profile-1");
      expect(button.title).toBe("Minimize KeePassXC and maximize Chromium");
    });
  });
});
