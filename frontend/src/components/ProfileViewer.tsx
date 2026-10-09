import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ClipboardCopy, Code2, Download, Folder, KeyRound, Maximize2, Minimize2, Upload } from "lucide-react";
import { api } from "../lib/api";

interface ProfileViewerProps {
  profileId: string;
  cdpUrl: string | null;
  clipboardSync: boolean;
  keepassxcEnabled: boolean;
  onDisconnect: () => void;
  onFiles?: () => void;
}

// X11 keysym for V key (Ctrl is already held in VNC by the time we intercept)
const XK_v = 0x0076;
type ClipboardActionState = "idle" | "busy" | "success" | "error";
type KeePassXCWindowState = "unknown" | "shown" | "minimized";

async function clipboardWithTimeout<T>(action: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  try {
    return await Promise.race([action, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error("Clipboard access timed out")), 10_000);
    })]);
  } finally {
    clearTimeout(timer!);
  }
}

export function ProfileViewer({
  profileId,
  cdpUrl,
  clipboardSync: initialClipboardSync,
  keepassxcEnabled,
  onDisconnect,
  onFiles,
}: ProfileViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<any>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [clipboardSync, setClipboardSync] = useState(initialClipboardSync);
  const [cdpCopied, setCdpCopied] = useState(false);
  const [setClipboardState, setSetClipboardState] = useState<ClipboardActionState>("idle");
  const [readClipboardState, setReadClipboardState] = useState<ClipboardActionState>("idle");
  const [keepassxcActionState, setKeepassxcActionState] = useState<ClipboardActionState>("idle");
  const [keepassxcWindowState, setKeepassxcWindowState] = useState<KeePassXCWindowState>("unknown");

  useEffect(() => {
    let rfb: any = null;
    let cancelled = false;

    async function connect() {
      try {
        // Import noVNC dynamically
        const { default: RFB } = await import("@novnc/novnc/core/rfb.js");

        if (cancelled) return;

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/api/profiles/${profileId}/vnc`;

        rfb = new RFB(containerRef.current!, wsUrl, {
          wsProtocols: ["binary"],
        });
        rfbRef.current = rfb;

        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.showDotCursor = true;

        rfb.addEventListener("connect", () => {
          if (!cancelled) setConnected(true);
        });

        rfb.addEventListener("disconnect", () => {
          if (!cancelled) {
            setConnected(false);
            onDisconnect();
          }
        });

        rfb.addEventListener("securityfailure", (e: any) => {
          setError(`Security failure: ${e.detail.reason}`);
        });
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to connect");
        }
      }
    }

    connect();

    return () => {
      cancelled = true;
      if (rfb) {
        try {
          rfb.disconnect();
        } catch (err) {
          console.debug("[vnc] disconnect cleanup failed:", err);
        }
      }
      rfbRef.current = null;
    };
  }, [profileId, onDisconnect]);

  // Host→VNC: intercept Ctrl+V/Cmd+V at keydown (capture phase)
  // Must fire BEFORE noVNC's canvas listener to prevent the race condition
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !clipboardSync || !connected) return;

    const handleKeyDown = async (e: KeyboardEvent) => {

      const isPaste =
        e.key === "v" && (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey;
      if (!isPaste) return;


      // Block noVNC from sending the keystroke before clipboard is updated
      e.stopPropagation();
      e.preventDefault();

      const rfb = rfbRef.current;
      if (!rfb) {
        return;
      }

      try {
        const text = await clipboardWithTimeout(navigator.clipboard.readText());
        if (text) {
          await api.setClipboard(profileId, text);
        }
      } catch (err) {
        console.warn("[clipboard] error:", err);
        return;
      }

      // Send full Ctrl+V sequence to VNC. We can't rely on Ctrl still being
      // held because the user may have released it during the async API call.
      rfb.sendKey(0xffe3, "ControlLeft", true);   // Ctrl press
      rfb.sendKey(XK_v, "KeyV", true);             // V press
      rfb.sendKey(XK_v, "KeyV", false);            // V release
      rfb.sendKey(0xffe3, "ControlLeft", false);   // Ctrl release
    };

    // capture: true ensures we fire before noVNC's canvas listener
    container.addEventListener("keydown", handleKeyDown, true);
    return () => container.removeEventListener("keydown", handleKeyDown, true);
  }, [profileId, clipboardSync, connected]);

  // VNC→Host: keep a best-effort listener for standard noVNC clipboard
  // events. KasmVNC CutText transport is disabled server-side to prevent
  // private BinaryClipboard type 180 messages from reaching noVNC.
  useEffect(() => {
    const rfb = rfbRef.current;
    if (!rfb || !clipboardSync || !connected) return;

    const handleClipboard = (e: any) => {
      const text = e.detail?.text;
      if (text) {
        navigator.clipboard.writeText(text).then(() => {
        }).catch((err) => {
          console.warn("[clipboard] writeText failed:", err);
        });
      }
    };

    rfb.addEventListener("clipboard", handleClipboard);
    return () => {
      rfb.removeEventListener("clipboard", handleClipboard);
    };
  }, [clipboardSync, connected]);

  // VNC→Host polling: Chrome doesn't write to X11 clipboard under KasmVNC.
  // Poll via Playwright CDP instead of relying on KasmVNC clipboard messages.
  useEffect(() => {
    if (!clipboardSync || !connected) return;

    let cancelled = false;
    let lastText = "";
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      if (cancelled) return;
      try {
        const { text } = await api.getClipboard(profileId);
        if (!cancelled && text && text !== lastText) {
          await clipboardWithTimeout(navigator.clipboard.writeText(text));
          lastText = text;
        }
      } catch (err) {
        console.warn("[clipboard] poll failed; retrying:", err);
      }
      if (!cancelled) {
        timer = setTimeout(poll, 2000);
      }
    };

    // Start polling after a short delay
    timer = setTimeout(poll, 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [profileId, clipboardSync, connected]);

  const toggleFullscreen = () => {
    if (!containerRef.current) return;
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
      setFullscreen(true);
    } else {
      document.exitFullscreen();
      setFullscreen(false);
    }
  };

  const settleActionState = (
    setter: (state: ClipboardActionState) => void,
    state: ClipboardActionState,
  ) => {
    setter(state);
    if (state !== "busy") {
      window.setTimeout(() => setter("idle"), 2000);
    }
  };

  const actionClass = (state: ClipboardActionState) => {
    const tone = state === "success"
      ? "text-emerald-400"
      : state === "error"
        ? "text-red-400"
        : "text-gray-500 hover:text-gray-300";
    return `p-1 disabled:cursor-not-allowed disabled:opacity-40 ${state === "busy" ? "animate-pulse" : ""} ${tone}`;
  };

  const handleSetClipboard = async () => {
    settleActionState(setSetClipboardState, "busy");
    try {
      let text = "";
      try {
        if (!navigator.clipboard?.readText) {
          throw new Error("Clipboard API is not available");
        }
        text = await clipboardWithTimeout(navigator.clipboard.readText());
      } catch (err) {
        console.warn("[clipboard] manual set readText unavailable:", err);
        const fallback = window.prompt("Paste text to send to browser clipboard", "");
        if (fallback === null) {
          settleActionState(setSetClipboardState, "idle");
          return;
        }
        text = fallback;
      }

      await api.setClipboard(profileId, text);
      settleActionState(setSetClipboardState, "success");
    } catch (err) {
      console.warn("[clipboard] manual set failed:", err);
      settleActionState(setSetClipboardState, "error");
    }
  };

  const handleReadClipboard = async () => {
    settleActionState(setReadClipboardState, "busy");
    try {
      const { text } = await api.getClipboard(profileId);
      try {
        await clipboardWithTimeout(navigator.clipboard.writeText(text));
      } catch (err) {
        console.warn("[clipboard] manual read writeText failed:", err);
        window.prompt("Copy browser clipboard text", text);
      }
      settleActionState(setReadClipboardState, "success");
    } catch (err) {
      console.warn("[clipboard] manual read failed:", err);
      settleActionState(setReadClipboardState, "error");
    }
  };

  const handleToggleKeePassXC = async () => {
    settleActionState(setKeepassxcActionState, "busy");
    try {
      const { state } = await api.toggleKeePassXCWindow(profileId);
      setKeepassxcWindowState(state);
      settleActionState(setKeepassxcActionState, "success");
    } catch (err) {
      console.warn("[keepassxc] window toggle failed:", err);
      settleActionState(setKeepassxcActionState, "error");
    }
  };

  useEffect(() => {
    const handleFsChange = () => {
      setFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener("fullscreenchange", handleFsChange);
    return () => document.removeEventListener("fullscreenchange", handleFsChange);
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
    };

    container.addEventListener("wheel", handleWheel, { passive: false });
    return () => container.removeEventListener("wheel", handleWheel);
  }, []);

  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <p className="text-red-400 text-sm mb-2">Connection failed</p>
          <p className="text-gray-500 text-xs">{error}</p>
          <button type="button" onClick={onDisconnect} className="btn-secondary mt-3">Back to profile settings</button>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-surface-1 border-b border-border">
        <div className="flex items-center gap-2">
          <button type="button" onClick={onDisconnect} className="btn-secondary p-1" title="Back to profile settings" aria-label="Back to profile settings"><ArrowLeft className="h-4 w-4" /></button>
          <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-yellow-400 animate-pulse"}`} />
          <span className="text-xs text-gray-400">
            {connected ? "Connected" : "Connecting..."}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {onFiles && <button type="button" onClick={onFiles} className="p-1 text-gray-500 hover:text-gray-300" title="Browser files" aria-label="Browser files"><Folder className="h-4 w-4" /></button>}
          {keepassxcEnabled && (
            <button
              onClick={handleToggleKeePassXC}
              className={actionClass(keepassxcActionState)}
              title={
                keepassxcActionState === "error"
                  ? "Failed to toggle KeePassXC window"
                  : keepassxcWindowState === "shown"
                    ? "Minimize KeePassXC and maximize Chromium"
                    : "Show and maximize KeePassXC"
              }
              aria-label="Toggle KeePassXC window"
              disabled={!connected || keepassxcActionState === "busy"}
            >
              <KeyRound className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            onClick={handleSetClipboard}
            className={actionClass(setClipboardState)}
            title="Set Clipboard (frontend → CloakBrowser)"
            disabled={!connected || setClipboardState === "busy"}
          >
            <Upload className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={handleReadClipboard}
            className={actionClass(readClipboardState)}
            title="Read Clipboard (CloakBrowser → frontend)"
            disabled={!connected || readClipboardState === "busy"}
          >
            <Download className="h-3.5 w-3.5" />
          </button>
          {cdpUrl && (
            <button
              onClick={() => {
                const base = `${window.location.protocol}//${window.location.host}${cdpUrl}`;
                navigator.clipboard?.writeText(base).then(() => {
                  setCdpCopied(true);
                  setTimeout(() => setCdpCopied(false), 2000);
                }).catch((err) => console.warn("[cdp] copy failed:", err));
              }}
              className={`p-1 ${cdpCopied ? "text-emerald-400" : "text-gray-500 hover:text-gray-300"}`}
              title={cdpCopied ? "Copied!" : "Copy CDP endpoint URL"}
            >
              <Code2 className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            onClick={() => { setClipboardSync(!clipboardSync); }}
            className={`p-1 ${clipboardSync ? "text-accent" : "text-gray-500 hover:text-gray-300"}`}
            title={clipboardSync ? "Disable clipboard sync" : "Enable clipboard sync"}
            disabled={!connected}
          >
            <ClipboardCopy className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={toggleFullscreen}
            className="text-gray-500 hover:text-gray-300 p-1"
            title={fullscreen ? "Exit fullscreen" : "Fullscreen"}
          >
            {fullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>

      {/* VNC canvas container */}
      <div
        ref={containerRef}
        className="flex-1 bg-black overflow-hidden"
        style={{ minHeight: 0 }}
      />
    </div>
  );
}
