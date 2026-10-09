"""Launch/stop/track isolated browser instances per profile."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cloakbrowser import launch_persistent_context_async

from .browser_backends import FIREFOX_EXTENSION_XPI, gpu_device, launch_camoufox, launch_native
from .clipboard import ClipboardManager
from .profile_files import profile_home
from .vnc_manager import VNCManager

logger = logging.getLogger("cloakbrowser.manager.browser")

KEEPASSXC_PASSWORD_ENV = "KEEPASSXC_DATABASE_PASSWORD"
KEEPASSXC_EXTENSION_DIR = Path("/opt/cloakbrowser/extensions/keepassxc-browser")
KEEPASSXC_EXTENSION_ARG = f"--load-extension={KEEPASSXC_EXTENSION_DIR}"
KEEPASSXC_RUNTIME_BASE = Path("/tmp/cbm")
KEEPASSXC_SOCKET_RELATIVE = Path(
    "app/org.keepassxc.KeePassXC/org.keepassxc.KeePassXC.BrowserServer"
)
KEEPASSXC_SOCKET_TIMEOUT_SECONDS = 15.0
KEEPASSXC_WINDOW_SEARCH_ATTEMPTS = 10
KEEPASSXC_WINDOW_SEARCH_INTERVAL_SECONDS = 0.1
CHROMIUM_WINDOW_SEARCH_ATTEMPTS = 20
CHROMIUM_WINDOW_SEARCH_INTERVAL_SECONDS = 0.1
CHROMIUM_WINDOW_SETTLE_SECONDS = 0.75
CHROMIUM_WINDOW_MAXIMIZE_ATTEMPTS = 2


class BrowserLaunchError(RuntimeError):
    """A user-actionable failure while starting a browser profile."""


class KeePassXCUnavailableError(RuntimeError):
    """The selected profile has no running KeePassXC instance."""


class KeePassXCWindowError(RuntimeError):
    """KeePassXC is running, but its X11 window could not be controlled."""


@dataclass(frozen=True)
class KeePassXCPaths:
    data_dir: Path
    database: Path
    config: Path
    local_config: Path
    runtime_dir: Path
    socket: Path
    log: Path


def require_keepassxc_password() -> str:
    """Return the configured database password or fail with a safe message."""
    password = os.environ.get(KEEPASSXC_PASSWORD_ENV, "")
    if not password:
        raise RuntimeError(
            f"{KEEPASSXC_PASSWORD_ENV} must be set to a non-empty value. "
            "Set it in docker-compose.yml or pass it to the container environment."
        )
    if "\n" in password or "\r" in password:
        raise RuntimeError(
            f"{KEEPASSXC_PASSWORD_ENV} must not contain newline characters."
        )
    return password


def _launch_args_include_keepassxc(launch_args: list[str] | None) -> bool:
    """Return whether launch args load the bundled KeePassXC-Browser extension."""
    args = launch_args or []
    for index, arg in enumerate(args):
        raw_paths: str | None = None
        if arg == "--load-extension" and index + 1 < len(args):
            raw_paths = args[index + 1]
        elif arg.startswith("--load-extension="):
            raw_paths = arg.split("=", 1)[1]

        if raw_paths is None:
            continue

        for raw_path in raw_paths.split(","):
            if Path(raw_path.strip()) == KEEPASSXC_EXTENSION_DIR:
                return True
    return False


def _keepassxc_paths(profile_id: str, user_data_dir: Path) -> KeePassXCPaths:
    data_dir = user_data_dir / "KeePassXC"
    # Unix-domain socket paths are limited to 108 bytes on Linux. A full UUID
    # plus KeePassXC's fixed BrowserServer suffix exceeds that limit, so use a
    # stable 128-bit URL-safe identifier for the isolated runtime directory.
    runtime_id = base64.urlsafe_b64encode(
        hashlib.sha256(profile_id.encode()).digest()[:16]
    ).decode().rstrip("=")
    runtime_dir = KEEPASSXC_RUNTIME_BASE / runtime_id
    return KeePassXCPaths(
        data_dir=data_dir,
        database=data_dir / "passwords.kdbx",
        config=data_dir / "keepassxc.ini",
        local_config=data_dir / "keepassxc-local.ini",
        runtime_dir=runtime_dir,
        socket=runtime_dir / KEEPASSXC_SOCKET_RELATIVE,
        log=Path(f"/tmp/keepassxc-{profile_id}.log"),
    )


def _remove_runtime_dir(path: Path) -> None:
    """Remove one validated per-profile runtime path without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _set_ini_value(lines: list[str], section: str, key: str, value: str) -> None:
    """Set one INI value while preserving unrelated KeePassXC settings."""
    section_header = f"[{section}]"
    section_start: int | None = None
    section_end = len(lines)

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            section_start = index
            continue
        if section_start is not None and stripped.startswith("[") and stripped.endswith("]"):
            section_end = index
            break

    if section_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([section_header, f"{key}={value}"])
        return

    for index in range(section_start + 1, section_end):
        candidate = lines[index].lstrip()
        if candidate.startswith(("#", ";")) or "=" not in candidate:
            continue
        candidate_key = candidate.split("=", 1)[0].strip()
        if candidate_key == key:
            lines[index] = f"{key}={value}"
            return

    lines.insert(section_end, f"{key}={value}")


def _set_ini_root_value(lines: list[str], key: str, value: str) -> None:
    """Set one root-level INI value before the first section."""
    first_section = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            first_section = index
            break

        candidate = line.lstrip()
        if candidate.startswith(("#", ";")) or "=" not in candidate:
            continue
        candidate_key = candidate.split("=", 1)[0].strip()
        if candidate_key == key:
            lines[index] = f"{key}={value}"
            return

    lines.insert(first_section, f"{key}={value}")


def _ensure_keepassxc_config(paths: KeePassXCPaths) -> None:
    """Persist settings required for unattended isolated browser integration."""
    paths.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.data_dir.chmod(0o700)

    if paths.config.exists():
        lines = paths.config.read_text(errors="replace").splitlines()
    else:
        lines = []

    _set_ini_root_value(lines, "SingleInstance", "false")
    # Managed instances must not open a first-run modal while another browser
    # takes focus during automatic database unlock.
    _set_ini_value(lines, "General", "UpdateCheckMessageShown", "true")
    _set_ini_value(lines, "GUI", "CheckForUpdates", "false")
    _set_ini_value(lines, "Browser", "Enabled", "true")
    _set_ini_value(lines, "Browser", "UpdateBinaryPath", "false")
    for key in (
        "LockDatabaseIdle",
        "LockDatabaseMinimize",
        "LockDatabaseScreenLock",
        "LockDatabaseOnUserSwitch",
    ):
        _set_ini_value(lines, "Security", key, "false")
    paths.config.write_text("\n".join(lines) + "\n")
    paths.config.chmod(0o600)

    paths.local_config.touch(exist_ok=True)
    paths.local_config.chmod(0o600)


def _build_profile_process_env(
    display: int,
    runtime_dir: Path,
    *,
    keepassxc_paths: KeePassXCPaths | None = None,
) -> dict[str, str]:
    """Build a child environment that never exposes the database password."""
    env = os.environ.copy()
    env.pop(KEEPASSXC_PASSWORD_ENV, None)
    env.update({
        "DISPLAY": f":{display}",
        "XDG_RUNTIME_DIR": str(runtime_dir),
    })

    if keepassxc_paths:
        env.update({
            "TMPDIR": str(runtime_dir),
            "KPXC_CONFIG": str(keepassxc_paths.config),
            "KPXC_CONFIG_LOCAL": str(keepassxc_paths.local_config),
            "XDG_CONFIG_HOME": str(keepassxc_paths.data_dir / "config"),
            "XDG_DATA_HOME": str(keepassxc_paths.data_dir / "share"),
            "XDG_CACHE_HOME": str(keepassxc_paths.data_dir / "cache"),
            "XDG_STATE_HOME": str(keepassxc_paths.data_dir / "state"),
        })

    return env


def _normalize_proxy(raw: str) -> str:
    """Convert common proxy formats to http://user:pass@host:port.

    Accepts:
      - http://user:pass@host:port  (already valid)
      - host:port:user:pass
      - host:port
    """
    if raw.startswith(("http://", "https://", "socks5://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"http://{user}:{passwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return raw


def _validate_proxy(url: str) -> None:
    """Validate that a normalized proxy URL has scheme, host, and port."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError(
            f"Invalid proxy scheme '{parsed.scheme}'. Must be http, https, or socks5."
        )
    if not parsed.hostname:
        raise ValueError(f"Proxy URL missing hostname: {url}")
    if not parsed.port:
        raise ValueError(f"Proxy URL missing port: {url}")


def _init_profile_defaults(user_data_dir: Path) -> None:
    """Set up bookmarks and DuckDuckGo search on first launch."""
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # --- Bookmarks (only on first launch) ---
    bookmarks_path = default_dir / "Bookmarks"
    if not bookmarks_path.exists():
        ts = str(int(time.time() * 1_000_000))  # Chrome timestamp format
        _id = 1

        def bm(name: str, url: str) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "url", "id": str(_id), "name": name, "url": url, "date_added": ts}

        def folder(name: str, children: list) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "folder", "id": str(_id), "name": name, "children": children, "date_added": ts, "date_modified": ts}

        bookmarks = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "type": "folder", "id": "1", "name": "Bookmarks bar",
                    "date_added": ts, "date_modified": ts,
                    "children": [
                        folder("Detection Tests", [
                            bm("Rebrowser Bot Detector", "https://bot-detector.rebrowser.net/"),
                            bm("Incolumitas", "https://bot.incolumitas.com/"),
                            bm("SannySort", "https://bot.sannysoft.com/"),
                            bm("BrowserScan Bot", "https://www.browserscan.net/bot-detection"),
                            bm("FingerprintJS Demo", "https://demo.fingerprint.com/web-scraping"),
                            bm("Pixelscan", "https://pixelscan.net/fingerprint-check"),
                            bm("CreepJS", "https://abrahamjuliot.github.io/creepjs/"),
                            bm("fingerprint-scan", "https://fingerprint-scan.com/"),
                            bm("DeviceInfo Bot", "https://deviceandbrowserinfo.com/are_you_a_bot"),
                        ]),
                        folder("Fingerprint", [
                            bm("BrowserLeaks Canvas", "https://browserleaks.com/canvas"),
                            bm("BrowserLeaks WebGL", "https://browserleaks.com/webgl"),
                            bm("BrowserLeaks Fonts", "https://browserleaks.com/fonts"),
                            bm("BrowserLeaks JS", "https://browserleaks.com/javascript"),
                            bm("FingerprintJS OSS", "https://fingerprintjs.github.io/fingerprintjs/"),
                            bm("Audio FP", "https://audiofingerprint.openwpm.com/"),
                            bm("DeviceInfo", "https://deviceandbrowserinfo.com/info_device"),
                        ]),
                        folder("Headers & TLS", [
                            bm("httpbin headers", "https://httpbin.org/headers"),
                            bm("httpbin IP", "https://httpbin.org/ip"),
                            bm("TLS Fingerprint", "https://tls.browserleaks.com/"),
                        ]),
                        folder("reCAPTCHA", [
                            bm("Google v3 Demo", "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php"),
                            bm("2captcha v3", "https://2captcha.com/demo/recaptcha-v3"),
                            bm("Turnstile", "https://peet.ws/turnstile-test/non-interactive.html"),
                        ]),
                    ],
                },
                "other": {"type": "folder", "id": "2", "name": "Other bookmarks", "children": []},
                "synced": {"type": "folder", "id": "3", "name": "Mobile bookmarks", "children": []},
            },
            "version": 1,
        }
        bookmarks_path.write_text(json.dumps(bookmarks, indent=2))
        logger.info("Created default bookmarks for %s", user_data_dir.name)

    # --- DuckDuckGo as default search engine ---
    prefs_path = default_dir / "Preferences"
    if not prefs_path.exists():
        prefs = {
            "default_search_provider_data": {
                "template_url_data": {
                    "keyword": "duckduckgo.com",
                    "short_name": "DuckDuckGo",
                    "url": "https://duckduckgo.com/?q={searchTerms}",
                    "suggestions_url": "https://duckduckgo.com/ac/?q={searchTerms}&type=list",
                    "favicon_url": "https://duckduckgo.com/favicon.ico",
                }
            },
            "default_search_provider": {
                "enabled": True,
            },
        }
        prefs_path.write_text(json.dumps(prefs, indent=2))
        logger.info("Set DuckDuckGo as default search for %s", user_data_dir.name)


BASE_CDP_PORT = 5100
CDP_PORT_RANGE = 100  # cycle through 5100-5199 to avoid TIME_WAIT collisions


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    display: int
    ws_port: int
    cdp_port: int | None
    browser_type: str = "cloakbrowser"
    user_data_dir: Path | None = None
    keepassxc_process: asyncio.subprocess.Process | None = None
    keepassxc_runtime_dir: Path | None = None
    bidi_port: int | None = None
    playwright_port: int | None = None


class BrowserManager:
    def __init__(self):
        self.running: dict[str, RunningProfile] = {}
        self._keepassxc_password: str | None = None
        self._launching: set[str] = set()  # profile IDs currently being launched
        self.vnc = VNCManager()
        self.clipboard = ClipboardManager()
        self._lock = asyncio.Lock()
        self._next_cdp_port = BASE_CDP_PORT
        self._auto_launch_task: asyncio.Task | None = None

    def load_keepassxc_password(self) -> None:
        """Load the Compose secret once, then remove it from child environments."""
        self._keepassxc_password = require_keepassxc_password()
        os.environ.pop(KEEPASSXC_PASSWORD_ENV, None)

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        """Launch a browser instance for the given profile."""
        profile_id = profile["id"]
        browser_type = profile.get("browser_type", "cloakbrowser")
        if browser_type not in {"cloakbrowser", "chromium", "firefox", "camoufox"}:
            raise ValueError(f"Unsupported browser type: {browser_type}")
        if browser_type == "camoufox" and profile.get("remote_debugging_enabled") and profile.get("playwright_enabled"):
            raise ValueError("Camoufox BiDi and Playwright endpoints are mutually exclusive.")
        keepassxc_enabled = profile.get("keepassxc_enabled")
        if keepassxc_enabled is None:
            keepassxc_enabled = (browser_type != "cloakbrowser" or _launch_args_include_keepassxc(profile.get("launch_args")))

        async with self._lock:
            if profile_id in self.running or profile_id in self._launching:
                raise RuntimeError(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)

        display, ws_port = await self.vnc.allocate()

        try:
            debugging = bool(profile.get("remote_debugging_enabled"))
            cdp_port = self._allocate_cdp_port() if browser_type == "cloakbrowser" or (browser_type == "chromium" and debugging) else None
            bidi_port = self._allocate_cdp_port() if browser_type in {"firefox", "camoufox"} and debugging else None
            playwright_port = self._allocate_cdp_port() if browser_type == "camoufox" and profile.get("playwright_enabled") else None
        except ValueError:
            async with self._lock:
                self._launching.discard(profile_id)
            await self.vnc.stop_vnc(display)
            raise

        # Clean stale Chromium lock files (left by previous container crashes)
        user_data_dir = Path(profile["user_data_dir"])
        if browser_type in {"cloakbrowser", "chromium"}:
            for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = user_data_dir / lock_file
                lock_path.unlink(missing_ok=True)

        # Set up bookmarks and search engine on first launch
        if browser_type in {"cloakbrowser", "chromium"}:
            _init_profile_defaults(user_data_dir)
        else:
            user_data_dir.mkdir(parents=True, exist_ok=True)

        context: Any | None = None
        keepassxc_process: asyncio.subprocess.Process | None = None
        keepassxc_runtime_dir = _keepassxc_paths(profile_id, user_data_dir).runtime_dir

        try:
            _remove_runtime_dir(keepassxc_runtime_dir)
            keepassxc_runtime_dir.mkdir(parents=True, mode=0o700)
            keepassxc_runtime_dir.chmod(0o700)

            # Start KasmVNC on the allocated display
            await self.vnc.start_vnc(
                display,
                ws_port,
                width=profile.get("screen_width", 1920),
                height=profile.get("screen_height", 1080),
                gpu_device=gpu_device(profile),
            )

            if keepassxc_enabled:
                extension = FIREFOX_EXTENSION_XPI if browser_type in {"firefox", "camoufox"} else KEEPASSXC_EXTENSION_DIR
                if not extension.exists():
                    raise BrowserLaunchError(f"Bundled KeePassXC-Browser extension not found at {extension}.")
                keepassxc_process, keepassxc_runtime_dir = await self._launch_keepassxc(
                    profile_id=profile_id,
                    user_data_dir=user_data_dir,
                    display=display,
                )

            # Normalize proxy format (host:port:user:pass → http://user:pass@host:port)
            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

            # Launch CloakBrowser on that display
            # DISPLAY is passed via env kwarg to avoid process-wide os.environ mutation
            browser_env = _build_profile_process_env(
                display,
                keepassxc_runtime_dir,
            )
            browser_env["HOME"] = str(profile_home(profile))
            if browser_type == "cloakbrowser":
                extra_args = self._build_fingerprint_args(profile)
                extra_args += profile.get("launch_args") or []
                if keepassxc_enabled and not _launch_args_include_keepassxc(extra_args):
                    extra_args.append(KEEPASSXC_EXTENSION_ARG)
                extra_args.append(f"--remote-debugging-port={cdp_port}")
                context = await launch_persistent_context_async(
                    user_data_dir=profile["user_data_dir"],
                    headless=bool(profile.get("headless", False)),
                    proxy=proxy,
                    args=extra_args,
                    timezone=profile.get("timezone") or None,
                    locale=profile.get("locale") or None,
                    humanize=bool(profile.get("humanize", False)),
                    human_preset=profile.get("human_preset", "default"),
                    geoip=bool(profile.get("geoip", False)),
                    color_scheme=profile.get("color_scheme") or None,
                    user_agent=profile.get("user_agent") or None,
                    viewport={
                        "width": profile.get("screen_width", 1920),
                        "height": profile.get("screen_height", 1080) - 133,
                    },
                    env=browser_env,
                )
            else:
                launch = launch_camoufox if browser_type == "camoufox" else launch_native
                context = await launch(
                    {**profile, "browser_type": browser_type, "runtime_dir": str(keepassxc_runtime_dir),
                     "remote_debugging_port": cdp_port or bidi_port, "playwright_port": playwright_port},
                    proxy, browser_env, bool(keepassxc_enabled),
                )

            # Inject clipboard listener: captures copied text on every page
            # so the GET /clipboard endpoint can read it via page.evaluate()
            _clipboard_init_js = """
                window.__clipboardText = '';
                document.addEventListener('copy', () => {
                    const sel = window.getSelection();
                    if (sel) window.__clipboardText = sel.toString();
                });
                document.addEventListener('keydown', (e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'c' && !e.altKey && !e.shiftKey) {
                        const sel = window.getSelection();
                        if (sel && sel.toString()) window.__clipboardText = sel.toString();
                    }
                });
            """
            if browser_type in {"cloakbrowser", "camoufox"} and hasattr(context, "add_init_script"):
                await context.add_init_script(_clipboard_init_js)
            # Also inject into already-open pages (about:blank created before init_script)
            for p in context.pages:
                try:
                    await p.evaluate(_clipboard_init_js)
                except Exception as exc:
                    logger.debug("Clipboard init failed on existing page: %s", exc)

            running = RunningProfile(
                profile_id=profile_id,
                context=context,
                display=display,
                ws_port=ws_port,
                cdp_port=cdp_port,
                browser_type=browser_type,
                user_data_dir=user_data_dir,
                keepassxc_process=keepassxc_process,
                keepassxc_runtime_dir=keepassxc_runtime_dir,
                bidi_port=bidi_port,
                playwright_port=playwright_port,
            )

            if not profile.get("headless", False):
                await self._maximize_chromium_window(
                    running,
                    keepassxc_runtime_dir,
                )

            if keepassxc_process is not None and keepassxc_process.returncode is not None:
                raise BrowserLaunchError("KeePassXC exited during browser startup.")

            # Auto-cleanup if browser crashes or user closes Chrome via VNC
            context.on("close", lambda: asyncio.ensure_future(
                self._on_browser_closed(profile_id)
            ))

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)

            if keepassxc_process is not None:
                asyncio.create_task(
                    self._watch_keepassxc(profile_id, keepassxc_process)
                )

            logger.info(
                "Launched profile %s on display :%d (ws_port=%d, cdp_port=%s)",
                profile_id, display, ws_port, cdp_port,
            )

            return running

        except BaseException:
            async with self._lock:
                self._launching.discard(profile_id)
            if context is not None:
                try:
                    await context.close()
                except Exception as exc:
                    logger.debug("Failed to close partially launched browser %s: %s", profile_id, exc)
            await self._cleanup_keepassxc(keepassxc_process, keepassxc_runtime_dir)
            await self.vnc.stop_vnc(display)
            raise

    async def _on_browser_closed(self, profile_id: str):
        """Called when browser exits (crash, user closed via VNC, or stop())."""
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if running:
            logger.info("Browser closed for profile %s, cleaning up", profile_id)
            await self.clipboard.cleanup(profile_id)
            await self._cleanup_keepassxc(
                running.keepassxc_process,
                running.keepassxc_runtime_dir,
            )
            await self.vnc.stop_vnc(running.display)

    async def stop(self, profile_id: str):
        """Stop a running browser instance."""
        # Pop before close so _on_browser_closed() finds nothing to clean up
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if not running:
            return

        logger.info("Stopping profile %s", profile_id)
        await self.clipboard.cleanup(profile_id)

        try:
            await running.context.close()
        except Exception as exc:
            logger.warning("Error closing context for %s: %s", profile_id, exc)

        await self._cleanup_keepassxc(
            running.keepassxc_process,
            running.keepassxc_runtime_dir,
        )
        await self.vnc.stop_vnc(running.display)

    async def _watch_keepassxc(
        self,
        profile_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Stop the paired browser if KeePassXC exits unexpectedly."""
        returncode = await process.wait()
        async with self._lock:
            running = self.running.get(profile_id)
            still_paired = running is not None and running.keepassxc_process is process

        if still_paired:
            logger.error(
                "KeePassXC exited unexpectedly for profile %s (returncode=%s)",
                profile_id,
                returncode,
            )
            await self.stop(profile_id)

    async def _launch_keepassxc(
        self,
        *,
        profile_id: str,
        user_data_dir: Path,
        display: int,
    ) -> tuple[asyncio.subprocess.Process, Path]:
        """Create/unlock a profile database and start its KeePassXC server."""
        password = self._keepassxc_password
        if password is None:
            raise RuntimeError("KeePassXC password was not initialized at manager startup.")
        paths = _keepassxc_paths(profile_id, user_data_dir)
        _ensure_keepassxc_config(paths)

        env = _build_profile_process_env(
            display,
            paths.runtime_dir,
            keepassxc_paths=paths,
        )
        process: asyncio.subprocess.Process | None = None

        try:
            if not paths.database.exists():
                await self._create_keepassxc_database(paths.database, password, env)
            await self._verify_keepassxc_database(paths.database, password, env)

            log_file = paths.log.open("w")
            try:
                process = await asyncio.create_subprocess_exec(
                    "keepassxc",
                    "--pw-stdin",
                    "--config", str(paths.config),
                    "--localconfig", str(paths.local_config),
                    str(paths.database),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            finally:
                log_file.close()

            if process.stdin is None:
                raise BrowserLaunchError("KeePassXC did not provide a password input stream.")
            process.stdin.write((password + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()

            await self._wait_for_keepassxc_socket(process, paths)
            await self._wait_for_keepassxc_window_ready(process, paths, display)
            logger.info(
                "Started KeePassXC for profile %s (runtime=%s)",
                profile_id,
                paths.runtime_dir,
            )
            return process, paths.runtime_dir
        except BaseException:
            await self._cleanup_keepassxc(process, paths.runtime_dir)
            raise

    async def _run_keepassxc_cli(
        self,
        args: list[str],
        stdin_text: str,
        env: dict[str, str],
    ) -> tuple[int, str]:
        """Run one KeePassXC CLI operation and return sanitized diagnostic output."""
        process = await asyncio.create_subprocess_exec(
            "keepassxc-cli",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await process.communicate(stdin_text.encode())
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        output = (stderr or stdout).decode(errors="replace").strip()
        return process.returncode or 0, output

    async def _create_keepassxc_database(
        self,
        database: Path,
        password: str,
        env: dict[str, str],
    ) -> None:
        returncode, output = await self._run_keepassxc_cli(
            ["db-create", "--quiet", "--set-password", str(database)],
            f"{password}\n{password}\n",
            env,
        )
        if returncode != 0:
            database.unlink(missing_ok=True)
            detail = output or "keepassxc-cli exited unsuccessfully"
            raise BrowserLaunchError(f"Failed to create KeePassXC database: {detail}")

    async def _verify_keepassxc_database(
        self,
        database: Path,
        password: str,
        env: dict[str, str],
    ) -> None:
        returncode, output = await self._run_keepassxc_cli(
            ["db-info", "--quiet", str(database)],
            f"{password}\n",
            env,
        )
        if returncode != 0:
            detail = output or "database could not be unlocked"
            raise BrowserLaunchError(
                "Failed to unlock the KeePassXC database with "
                f"{KEEPASSXC_PASSWORD_ENV}: {detail}"
            )

    async def _wait_for_keepassxc_socket(
        self,
        process: asyncio.subprocess.Process,
        paths: KeePassXCPaths,
    ) -> None:
        deadline = time.monotonic() + KEEPASSXC_SOCKET_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if paths.socket.is_socket():
                return
            if process.returncode is not None:
                try:
                    detail = paths.log.read_text(errors="replace").strip()
                except OSError:
                    detail = ""
                suffix = f": {detail[-1000:]}" if detail else ""
                raise BrowserLaunchError(
                    f"KeePassXC exited before browser integration became ready{suffix}"
                )
            await asyncio.sleep(0.1)

        raise BrowserLaunchError(
            "Timed out waiting for the KeePassXC browser integration socket at "
            f"{paths.socket}."
        )

    async def _wait_for_keepassxc_window_ready(
        self, process: asyncio.subprocess.Process, paths: KeePassXCPaths, display: int,
    ) -> None:
        # The integration socket is created before automatic unlock finishes.
        # Wait for the database window before browser native messaging connects.
        deadline = time.monotonic() + KEEPASSXC_SOCKET_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.returncode is not None:
                raise BrowserLaunchError("KeePassXC exited while unlocking its database.")
            code, title = await self._run_xdotool(
                display, paths.runtime_dir,
                ["search", "--all", "--pid", str(process.pid), "--name", " - KeePassXC$", "getwindowname"],
            )
            if code == 0 and title.strip() and "[locked]" not in title.lower():
                return
            await asyncio.sleep(0.1)
        raise BrowserLaunchError("Timed out waiting for KeePassXC to unlock its database.")

    async def _cleanup_keepassxc(
        self,
        process: asyncio.subprocess.Process | None,
        runtime_dir: Path | None,
    ) -> None:
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if runtime_dir is not None:
            _remove_runtime_dir(runtime_dir)

    async def _run_xdotool(
        self,
        display: int,
        runtime_dir: Path,
        args: list[str],
    ) -> tuple[int, str]:
        """Run xdotool in one profile display without exposing the DB password."""
        try:
            process = await asyncio.create_subprocess_exec(
                "xdotool",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_profile_process_env(display, runtime_dir),
            )
        except FileNotFoundError as exc:
            raise KeePassXCWindowError(
                "The xdotool window-control helper is not installed."
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise KeePassXCWindowError("Timed out while controlling the KeePassXC window.") from exc

        output = (stderr or stdout).decode(errors="replace").strip()
        return process.returncode or 0, output

    async def _find_keepassxc_window(
        self,
        running: RunningProfile,
        process: asyncio.subprocess.Process,
        runtime_dir: Path,
    ) -> tuple[str, list[str]]:
        """Find the profile's main KeePassXC window and all of its windows."""
        for attempt in range(KEEPASSXC_WINDOW_SEARCH_ATTEMPTS):
            all_returncode, all_output = await self._run_xdotool(
                running.display,
                runtime_dir,
                ["search", "--pid", str(process.pid)],
            )
            all_window_ids = [
                line for line in all_output.splitlines() if line.isdigit()
            ]
            if all_returncode == 0 and all_window_ids:
                main_returncode, main_output = await self._run_xdotool(
                    running.display,
                    runtime_dir,
                    [
                        "search",
                        "--all",
                        "--pid",
                        str(process.pid),
                        "--name",
                        "KeePassXC",
                    ],
                )
                main_window_ids = [
                    line for line in main_output.splitlines() if line.isdigit()
                ]
                main_window_id = (
                    main_window_ids[0]
                    if main_returncode == 0 and main_window_ids
                    else all_window_ids[0]
                )
                return main_window_id, all_window_ids

            if process.returncode is not None:
                raise KeePassXCUnavailableError("KeePassXC is no longer running.")
            if attempt + 1 < KEEPASSXC_WINDOW_SEARCH_ATTEMPTS:
                await asyncio.sleep(KEEPASSXC_WINDOW_SEARCH_INTERVAL_SECONDS)

        raise KeePassXCWindowError("Could not find the KeePassXC window.")

    async def _require_xdotool(
        self,
        running: RunningProfile,
        runtime_dir: Path,
        args: list[str],
        action: str,
    ) -> str:
        returncode, output = await self._run_xdotool(
            running.display,
            runtime_dir,
            args,
        )
        if returncode != 0:
            detail = output[-500:] if output else "xdotool exited unsuccessfully"
            raise KeePassXCWindowError(f"Failed to {action}: {detail}")
        return output

    async def _find_chromium_window(
        self,
        running: RunningProfile,
        runtime_dir: Path,
    ) -> str:
        """Find the profile's Chromium main window by its user-data directory."""
        user_data_dir = running.user_data_dir
        if user_data_dir is None:
            raise KeePassXCWindowError("The Chromium profile directory is unavailable.")

        expected_arg = f"--user-data-dir={user_data_dir}"
        for attempt in range(CHROMIUM_WINDOW_SEARCH_ATTEMPTS):
            for proc_dir in Path("/proc").iterdir():
                if not proc_dir.name.isdigit():
                    continue
                try:
                    raw_cmdline = (proc_dir / "cmdline").read_bytes()
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                args = [
                    part.decode(errors="replace")
                    for part in raw_cmdline.split(b"\0")
                    if part
                ]
                firefox_profile = any(
                    arg in {"--profile", "-profile"} and index + 1 < len(args) and args[index + 1] == str(user_data_dir)
                    for index, arg in enumerate(args)
                )
                if not args or (expected_arg not in args and not firefox_profile):
                    continue
                if any(arg.startswith("--type=") for arg in args[1:]):
                    continue
                if Path(args[0]).name not in {
                    "chrome",
                    "chromium",
                    "chromium-browser",
                    "firefox", "firefox-esr", "firefox-bin", "camoufox", "camoufox-bin",
                }:
                    continue

                searches = (
                    [
                        "search",
                        "--all",
                        "--onlyvisible",
                        "--pid",
                        proc_dir.name,
                        "--name",
                        "Firefox|Camoufox" if running.browser_type in {"firefox", "camoufox"} else "Chromium",
                    ],
                    ["search", "--onlyvisible", "--pid", proc_dir.name],
                )
                for search_args in searches:
                    returncode, output = await self._run_xdotool(
                        running.display,
                        runtime_dir,
                        search_args,
                    )
                    window_ids = [
                        line for line in output.splitlines() if line.isdigit()
                    ]
                    if returncode == 0 and window_ids:
                        return window_ids[0]

            if attempt + 1 < CHROMIUM_WINDOW_SEARCH_ATTEMPTS:
                await asyncio.sleep(CHROMIUM_WINDOW_SEARCH_INTERVAL_SECONDS)

        raise KeePassXCWindowError("Could not find the Chromium window.")

    async def _get_display_geometry(
        self,
        running: RunningProfile,
        runtime_dir: Path,
    ) -> tuple[int, int]:
        geometry = await self._require_xdotool(
            running,
            runtime_dir,
            ["getdisplaygeometry"],
            "read the VNC display size",
        )
        try:
            width, height = (int(value) for value in geometry.split()[:2])
            if width <= 0 or height <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise KeePassXCWindowError(
                f"Invalid VNC display geometry returned by xdotool: {geometry!r}"
            ) from exc
        return width, height

    async def _maximize_chromium_window(
        self,
        running: RunningProfile,
        runtime_dir: Path,
        *,
        window_id: str | None = None,
    ) -> None:
        """Restore, fill, and focus one profile's Chromium window."""
        width, height = await self._get_display_geometry(running, runtime_dir)
        chromium_window_id = window_id or await self._find_chromium_window(
            running,
            runtime_dir,
        )
        for attempt in range(CHROMIUM_WINDOW_MAXIMIZE_ATTEMPTS):
            commands = (
                (
                    ["windowmap", "--sync", chromium_window_id],
                    "restore the Chromium window",
                ),
                (
                    ["windowmove", "--sync", chromium_window_id, "0", "0"],
                    "move the Chromium window",
                ),
                (
                    [
                        "windowsize",
                        "--sync",
                        chromium_window_id,
                        str(width),
                        str(height),
                    ],
                    "maximize the Chromium window",
                ),
                (
                    ["windowraise", chromium_window_id],
                    "raise the Chromium window",
                ),
                (
                    ["windowfocus", "--sync", chromium_window_id],
                    "focus the Chromium window",
                ),
            )
            for args, action in commands:
                await self._require_xdotool(running, runtime_dir, args, action)

            await asyncio.sleep(CHROMIUM_WINDOW_SETTLE_SECONDS)
            geometry = await self._require_xdotool(
                running,
                runtime_dir,
                ["getwindowgeometry", "--shell", chromium_window_id],
                "verify the Chromium window size",
            )
            values = dict(
                line.split("=", 1)
                for line in geometry.splitlines()
                if "=" in line
            )
            if (
                values.get("X") == "0"
                and values.get("Y") == "0"
                and values.get("WIDTH") == str(width)
                and values.get("HEIGHT") == str(height)
            ):
                return

            logger.debug(
                "Chromium window bounds changed after maximize attempt %d: %s",
                attempt + 1,
                geometry.replace("\n", " "),
            )

        raise KeePassXCWindowError(
            "Chromium did not retain the requested VNC window bounds."
        )

    async def toggle_keepassxc_window(
        self,
        profile_id: str,
    ) -> Literal["shown", "minimized"]:
        """Show/maximize KeePassXC, or hide it when it currently has focus."""
        async with self._lock:
            running = self.running.get(profile_id)

        if running is None:
            raise KeyError(profile_id)

        process = running.keepassxc_process
        runtime_dir = running.keepassxc_runtime_dir
        if process is None or runtime_dir is None or process.returncode is not None:
            raise KeePassXCUnavailableError(
                "KeePassXC is not enabled for this running profile."
            )

        main_window_id, window_ids = await self._find_keepassxc_window(
            running,
            process,
            runtime_dir,
        )
        focus_returncode, focused_window = await self._run_xdotool(
            running.display,
            runtime_dir,
            ["getwindowfocus"],
        )
        focused_window_id = focused_window.splitlines()[-1].strip() if focused_window else ""
        pid_returncode = 1
        focused_pid = ""
        if focus_returncode == 0 and focused_window_id.isdigit():
            pid_returncode, focused_pid = await self._run_xdotool(
                running.display,
                runtime_dir,
                ["getwindowpid", focused_window_id],
            )
        focused_pid_lines = focused_pid.splitlines()
        is_foreground = pid_returncode == 0 and bool(focused_pid_lines) and (
            focused_pid_lines[-1].strip() == str(process.pid)
        )

        if is_foreground:
            chromium_window_id = await self._find_chromium_window(
                running,
                runtime_dir,
            )

            for window_id in reversed(window_ids):
                await self._require_xdotool(
                    running,
                    runtime_dir,
                    ["windowunmap", "--sync", window_id],
                    "minimize the KeePassXC windows",
                )

            await self._maximize_chromium_window(
                running,
                runtime_dir,
                window_id=chromium_window_id,
            )
            return "minimized"

        width, height = await self._get_display_geometry(running, runtime_dir)

        for window_id in window_ids:
            await self._require_xdotool(
                running,
                runtime_dir,
                ["windowmap", "--sync", window_id],
                "restore the KeePassXC windows",
            )

        commands = (
            (["windowmove", main_window_id, "0", "0"], "move the KeePassXC window"),
            (
                ["windowsize", main_window_id, str(width), str(height)],
                "maximize the KeePassXC window",
            ),
            (["windowraise", main_window_id], "raise the KeePassXC window"),
        )
        for args, action in commands:
            await self._require_xdotool(running, runtime_dir, args, action)

        # Keep modal dialogs (for example a first-run prompt) above the maximized
        # main window, then focus the topmost KeePassXC window.
        secondary_windows = [
            window_id for window_id in window_ids if window_id != main_window_id
        ]
        for window_id in secondary_windows:
            await self._require_xdotool(
                running,
                runtime_dir,
                ["windowraise", window_id],
                "raise a KeePassXC dialog",
            )
        focus_window_id = secondary_windows[-1] if secondary_windows else main_window_id
        await self._require_xdotool(
            running,
            runtime_dir,
            ["windowfocus", "--sync", focus_window_id],
            "focus the KeePassXC window",
        )
        return "shown"

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Get running status for a profile."""
        running = self.running.get(profile_id)
        if running:
            return {
                "status": "running",
                "vnc_ws_port": running.ws_port,
                "display": f":{running.display}",
                "cdp_url": f"/api/profiles/{profile_id}/cdp" if running.cdp_port is not None else None,
                "bidi_url": f"/api/profiles/{profile_id}/bidi" if running.bidi_port is not None else None,
                "playwright_url": f"/api/profiles/{profile_id}/playwright" if running.playwright_port is not None else None,
            }
        return {"status": "stopped", "vnc_ws_port": None, "display": None, "cdp_url": None, "bidi_url": None, "playwright_url": None}

    async def cleanup_all(self):
        """Stop all running profiles. Called on shutdown."""
        async with self._lock:
            profile_ids = list(self.running.keys())

        for pid in profile_ids:
            await self.stop(pid)

        await self.vnc.cleanup_all()

    async def cleanup_stale(self):
        """Kill orphan processes from previous container runs."""
        try:
            subprocess.run(
                ["pkill", "-f", r"keepassxc.*?/data/profiles/"],
                capture_output=True,
            )
            subprocess.run(
                ["pkill", "-f", r"keepassxc-proxy"],
                capture_output=True,
            )
        except FileNotFoundError:
            logger.debug("pkill not found, skipping stale KeePassXC cleanup")
        _remove_runtime_dir(KEEPASSXC_RUNTIME_BASE)
        await self.vnc.cleanup_stale()

    async def auto_launch_all(self):
        """Launch all profiles with auto_launch=True. Called on startup."""
        from . import database as db

        profiles = db.list_profiles()
        auto_profiles = [p for p in profiles if p.get("auto_launch")]
        if not auto_profiles:
            logger.info("No profiles configured for auto-launch")
            return

        logger.info("Auto-launching %d profile(s)...", len(auto_profiles))
        for profile in auto_profiles:
            try:
                await asyncio.wait_for(self.launch(profile), timeout=60)
                logger.info("Auto-launched profile %s (%s)", profile["name"], profile["id"])
            except Exception as exc:
                logger.error(
                    "Auto-launch failed for profile %s (%s): %s",
                    profile["name"], profile["id"], exc,
                )
        logger.info("Auto-launch complete: %d running", len(self.running))

    def _allocate_cdp_port(self) -> int:
        """Find a free CDP port using a rotating counter to avoid TIME_WAIT collisions."""
        for _ in range(CDP_PORT_RANGE):
            port = self._next_cdp_port
            self._next_cdp_port = BASE_CDP_PORT + (
                (self._next_cdp_port + 1 - BASE_CDP_PORT) % CDP_PORT_RANGE
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise ValueError("No free CDP ports available in range %d-%d" % (BASE_CDP_PORT, BASE_CDP_PORT + CDP_PORT_RANGE - 1))

    def _build_fingerprint_args(self, profile: dict[str, Any]) -> list[str]:
        """Build extra Chromium args from profile fingerprint settings."""
        args: list[str] = [
            "--disable-infobars",
            "--test-type",  # suppress "unsupported flag: --no-sandbox" bad flags warning
            "--use-angle=swiftshader",  # software GL for VNC (no GPU in container)
        ]

        seed = profile.get("fingerprint_seed")
        if seed is not None:
            args.append(f"--fingerprint={seed}")

        p = profile.get("platform")
        if p:
            # Map our "macos" to binary's "macos"
            args.append(f"--fingerprint-platform={p}")

        vendor = profile.get("gpu_vendor")
        if vendor:
            args.append(f"--fingerprint-gpu-vendor={vendor}")

        renderer = profile.get("gpu_renderer")
        if renderer:
            args.append(f"--fingerprint-gpu-renderer={renderer}")

        hw = profile.get("hardware_concurrency")
        if hw is not None:
            args.append(f"--fingerprint-hardware-concurrency={hw}")

        sw = profile.get("screen_width")
        sh = profile.get("screen_height")
        if sw:
            args.append(f"--fingerprint-screen-width={sw}")
        if sh:
            args.append(f"--fingerprint-screen-height={sh}")

        return args
