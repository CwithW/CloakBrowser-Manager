"""Per-profile WebRTC defaults and reversible browser preference ownership."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

WebRTCMode = Literal["disabled", "protect_ip", "enabled"]
FIREFOX_DEFAULTS = {
    "media.peerconnection.enabled": True,
    "media.peerconnection.ice.no_host": False,
    "media.peerconnection.ice.default_address_only": False,
    "media.peerconnection.ice.proxy_only": False,
}
EXTENSION_SOURCE = Path(__file__).with_name("webrtc_extension")


def default_mode(browser_type: str) -> WebRTCMode:
    return "enabled" if browser_type == "cloakbrowser" else "disabled"


def _state_path(profile: dict[str, Any]) -> Path:
    return Path(profile["user_data_dir"]) / "webrtc-state.json"


def _state(profile: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(profile)
    return json.loads(path.read_text()) if path.exists() else {}


def _save(profile: dict[str, Any], state: dict[str, Any]) -> None:
    path = _state_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(path)


def firefox_policy(profile: dict[str, Any]) -> dict[str, Any]:
    mode = profile.get("webrtc_mode", "enabled")
    state = _state(profile)
    if "firefox" not in state:
        if mode == "enabled":
            return {}
        original = {}
        directory = Path(profile["user_data_dir"])
        for name in ("prefs.js", "user.js"):
            path = directory / name
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                match = re.match(r'\s*user_pref\(("(?:[^"\\]|\\.)*")\s*,\s*(.*?)\);', line)
                if match and json.loads(match[1]) in FIREFOX_DEFAULTS:
                    try:
                        original[json.loads(match[1])] = json.loads(match[2])
                    except json.JSONDecodeError:
                        # User-written JavaScript remains in user.js; do not
                        # execute it or make it prevent browser startup.
                        continue
        state["firefox"] = original
        _save(profile, state)
    # Camoufox's launcher persists preferences itself, so explicitly restore
    # absent keys to engine defaults rather than leaving an old false value.
    prefs = FIREFOX_DEFAULTS.copy() if profile.get("browser_type") == "camoufox" else {}
    prefs.update(state["firefox"])
    if mode == "disabled":
        prefs["media.peerconnection.enabled"] = False
    elif mode == "protect_ip":
        prefs.update({
            "media.peerconnection.enabled": True,
            "media.peerconnection.ice.no_host": True,
            "media.peerconnection.ice.default_address_only": True,
            "media.peerconnection.ice.proxy_only": True,
        })
    return prefs


def chromium_policy(profile: dict[str, Any], preferences: dict[str, Any]) -> Path | None:
    mode = profile.get("webrtc_mode", "enabled")
    state = _state(profile)
    rtc = preferences.get("webrtc", {})
    if "chromium" not in state:
        if mode == "enabled":
            return None
        state["chromium"] = {"present": "ip_handling_policy" in rtc, "value": rtc.get("ip_handling_policy")}
        _save(profile, state)
    if mode == "enabled":
        original = state["chromium"]
        if original["present"]:
            preferences.setdefault("webrtc", {})["ip_handling_policy"] = original["value"]
        else:
            rtc.pop("ip_handling_policy", None)
    else:
        preferences.setdefault("webrtc", {})["ip_handling_policy"] = "disable_non_proxied_udp"
    # Keep a stable unpacked extension path across mode changes. An extension
    # previously installed in this persistent profile must become a no-op.
    directory = Path(profile.get("runtime_dir", profile["user_data_dir"])) / "webrtc-control"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_bytes((EXTENSION_SOURCE / "manifest.json").read_bytes())
    script = (EXTENSION_SOURCE / "block.js").read_text()
    (directory / "block.js").write_text(script.replace("__BLOCK_WEBRTC__", json.dumps(mode == "disabled")))
    (directory / "ready.js").write_bytes((EXTENSION_SOURCE / "ready.js").read_bytes())
    # Extension service-worker code is cached in persistent profiles. Read the
    # per-launch nonce from a resource instead of embedding it in cached code.
    (directory / "ready.json").write_text(json.dumps({"url": profile.get("webrtc_ready_url")}))
    return directory
