"""Native subprocess browsers and the official Camoufox launch adapter."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .profile_files import profile_home

CHROMIUM_EXTENSION = Path("/opt/cloakbrowser/extensions/keepassxc-browser")
FIREFOX_EXTENSION_XPI = Path("/opt/cloakbrowser/extensions/keepassxc-browser-firefox.xpi")
KEEPASSXC_ADDON_ID = "keepassxc-browser@keepassxc.org"
PROXY_ADDON_ID = "proxy-auth@cloakbrowser-manager.local"
logger = logging.getLogger("cloakbrowser.manager.engines")


def gpu_device(profile: dict[str, Any]) -> str | None:
    """Use an accessible render node only when the profile allows it."""
    if profile.get("browser_type", "cloakbrowser") == "cloakbrowser" or not profile.get("use_gpu", True):
        return None
    return next((str(node) for node in sorted(Path("/dev/dri").glob("renderD*")) if os.access(node, os.R_OK | os.W_OK)), None)


def gpu_available(profile: dict[str, Any]) -> bool:
    return gpu_device(profile) is not None


def browser_environment(profile: dict[str, Any], env: dict[str, str]) -> dict[str, str]:
    env = env.copy()
    # Browser networking is governed by the profile, not the manager's proxy.
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(name, None)
    if profile.get("timezone"):
        env["TZ"] = profile["timezone"]
    if not gpu_available(profile):
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    else:
        env.pop("LIBGL_ALWAYS_SOFTWARE", None)
    return env


def playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


def firefox_preferences(profile: dict[str, Any], proxy: str | None) -> dict[str, Any]:
    prefs: dict[str, Any] = {
        "browser.shell.checkDefaultBrowser": False,
        "browser.startup.homepage_override.mstone": "ignore",
        "browser.aboutwelcome.enabled": False,
        "browser.startup.homepage": "about:blank",
        "browser.startup.page": 0,
        "browser.warnOnQuit": False,
        "browser.tabs.warnOnClose": False,
        "browser.sessionstore.resume_from_crash": True,
        "extensions.autoDisableScopes": 0,
        "extensions.enabledScopes": 15,
        "network.proxy.type": 0,
        "browser.download.dir": str(profile_home(profile) / "Downloads"),
        "browser.download.folderList": 2,
    }
    if profile.get("browser_type") == "firefox":
        prefs.update({
            "browser.contentblocking.category": "strict",
            "privacy.trackingprotection.enabled": True,
            "privacy.trackingprotection.pbmode.enabled": True,
            "privacy.trackingprotection.socialtracking.enabled": True,
            "privacy.trackingprotection.cryptomining.enabled": True,
            "privacy.trackingprotection.fingerprinting.enabled": True,
            "privacy.fingerprintingProtection": True,
            "network.cookie.cookieBehavior": 5,
            "privacy.query_stripping.enabled": True,
            "privacy.globalprivacycontrol.enabled": True,
        })
    if profile.get("locale"):
        prefs["intl.accept_languages"] = profile["locale"]
        prefs["intl.locale.requested"] = profile["locale"]
    if profile.get("user_agent"):
        prefs["general.useragent.override"] = profile["user_agent"]
    scheme = profile.get("color_scheme")
    if scheme in {"light", "dark"}:
        prefs["ui.systemUsesDarkTheme"] = int(scheme == "dark")
        prefs["layout.css.prefers-color-scheme.content-override"] = 0 if scheme == "dark" else 1
    prefs["layers.acceleration.disabled"] = not gpu_available(profile)
    if gpu_available(profile):
        prefs["gfx.webrender.all"] = True
        prefs["media.ffmpeg.vaapi.enabled"] = True
    prefs.update(profile.get("firefox_preferences") or {})
    if proxy:
        parsed = urlsplit(proxy)
        prefs["network.proxy.type"] = 1
        prefs["network.proxy.no_proxies_on"] = ""
        if parsed.scheme == "socks5":
            if parsed.username is not None:
                raise ValueError("Firefox does not support authenticated SOCKS5 proxies; use an HTTP(S) proxy.")
            prefs.update({
                "network.proxy.socks": parsed.hostname,
                "network.proxy.socks_port": parsed.port,
                "network.proxy.socks_version": 5,
                "network.proxy.socks_remote_dns": True,
            })
        else:
            for protocol in ("http", "ssl"):
                prefs[f"network.proxy.{protocol}"] = parsed.hostname
                prefs[f"network.proxy.{protocol}_port"] = parsed.port
            prefs["network.proxy.proxy_over_tls"] = parsed.scheme == "https"
    return prefs


def _proxy_auth_extension(directory: Path, proxy: str, *, firefox: bool) -> Path:
    """Supply credentials only to this proxy's challenges, never website auth."""
    parsed = urlsplit(proxy)
    credentials = {"username": unquote(parsed.username or ""), "password": unquote(parsed.password or "")}
    manifest: dict[str, Any] = {
        "manifest_version": 2 if firefox else 3,
        "name": "Profile proxy authentication", "version": "1.0",
        "permissions": ["webRequest", "webRequestBlocking", "proxy", "<all_urls>"] if firefox else ["webRequest", "webRequestAuthProvider"],
        "background": {"scripts": ["background.js"]} if firefox else {"service_worker": "background.js"},
    }
    if firefox:
        manifest["browser_specific_settings"] = {"gecko": {"id": PROXY_ADDON_ID}}
    else:
        manifest["host_permissions"] = ["<all_urls>"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # A bad password must not create an infinite retry loop.
    script = (
        f"const credentials = {json.dumps(credentials)};\n"
        f"const host = {json.dumps(parsed.hostname)}; const port = {parsed.port};\n"
        "const attempts = new Map();\n"
        "const api = globalThis.browser || globalThis.chrome;\n"
        "function authenticate(details) {\n"
        "  if (!details.isProxy || details.challenger.host !== host || details.challenger.port !== port) return {};\n"
        "  if (attempts.has(details.requestId)) return {cancel: true};\n"
        "  attempts.set(details.requestId, true); return {authCredentials: credentials};\n"
        "}\n"
    )
    if firefox:
        script += "api.webRequest.onAuthRequired.addListener(authenticate, {urls: ['<all_urls>']}, ['blocking']);\n"
    else:
        script += "api.webRequest.onAuthRequired.addListener((details, callback) => callback(authenticate(details)), {urls: ['<all_urls>']}, ['asyncBlocking']);\n"
    script += "for (const event of [api.webRequest.onCompleted, api.webRequest.onErrorOccurred]) event.addListener(d => attempts.delete(d.requestId), {urls: ['<all_urls>']});\n"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "background.js").write_text(script)
    (directory / "background.js").chmod(0o600)
    return directory


def _write_firefox_preferences(directory: Path, prefs: dict[str, Any]) -> None:
    """Replace our own user.js block and preserve all user-supplied lines."""
    path = directory / "user.js"
    start, end = "// BEGIN CloakBrowser Manager", "// END CloakBrowser Manager"
    existing = path.read_text() if path.exists() else ""
    if start in existing and end in existing:
        before, rest = existing.split(start, 1)
        managed, after = rest.split(end, 1)
        existing = before + after.lstrip("\n")
        old_keys = {json.loads(match) for match in re.findall(r'user_pref\(("(?:[^"\\]|\\.)*")\s*,', managed)}
        removed = old_keys - prefs.keys()
        # Firefox persists user.js settings into prefs.js. Clearing a managed
        # option must also clear that persisted value, without touching others.
        stored = directory / "prefs.js"
        if removed and stored.exists():
            lines = stored.read_text().splitlines()
            def keep(line: str) -> bool:
                match = re.match(r'user_pref\(("(?:[^"\\]|\\.)*")\s*,', line)
                return not match or json.loads(match[1]) not in removed
            stored.write_text("\n".join(line for line in lines if keep(line)) + "\n")
    lines = [f"user_pref({json.dumps(key)}, {json.dumps(value)});" for key, value in prefs.items()]
    path.write_text(existing.rstrip() + "\n" + start + "\n" + "\n".join(lines) + "\n" + end + "\n")


def native_command(profile: dict[str, Any], proxy: str | None, keepassxc: bool) -> list[str]:
    directory = Path(profile["user_data_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    width, height = profile.get("screen_width", 1920), profile.get("screen_height", 1080)
    args = profile.get("launch_args") or []
    # Native profiles have no automation/debugging endpoint, including custom flags.
    forbidden = ("--remote-debugging", "--enable-automation", "--test-type", "--marionette", "-marionette", "--juggler", "-juggler", "--start-debugger-server", "-start-debugger-server", "--headless", "-headless")
    if profile.get("headless") or any(arg.startswith(forbidden) for arg in args):
        raise ValueError("Native browsers require normal windowed launch without headless, automation or remote-debugging arguments.")
    if profile["browser_type"] == "firefox":
        prefs = firefox_preferences(profile, proxy)
        extensions = directory / "extensions"
        extensions.mkdir(exist_ok=True)
        _install_firefox_keepassxc(directory, keepassxc)
        proxy_addon = extensions / f"{PROXY_ADDON_ID}.xpi"
        if proxy and urlsplit(proxy).username is not None:
            auth_dir = _proxy_auth_extension(Path(profile["runtime_dir"]) / "proxy-auth", proxy, firefox=True)
            with zipfile.ZipFile(proxy_addon, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in auth_dir.iterdir():
                    archive.write(path, path.name)
            proxy_addon.chmod(0o600)
            # ESR supports unsigned local extensions without enabling automation.
            prefs["xpinstall.signatures.required"] = False
        else:
            proxy_addon.unlink(missing_ok=True)
        _write_firefox_preferences(directory, prefs)
        command = ["/usr/bin/firefox-esr", "--no-remote", "--new-instance", "--profile", str(directory), "--width", str(width), "--height", str(height)]
    else:
        command = ["/usr/bin/chromium", f"--user-data-dir={directory}", "--no-sandbox", "--no-first-run", "--no-default-browser-check", "--disable-background-mode", f"--window-size={width},{height}"]
        if not gpu_available(profile):
            command.append("--use-angle=swiftshader")
        else:
            command.append("--use-gl=angle")
            command.append("--use-angle=gl")
        preferences = directory / "Default" / "Preferences"
        data = json.loads(preferences.read_text()) if preferences.exists() else {}
        data.setdefault("download", {})["default_directory"] = str(profile_home(profile) / "Downloads")
        if profile.get("locale"):
            command.append(f"--lang={profile['locale']}")
            data.setdefault("intl", {})["accept_languages"] = profile["locale"]
        else:
            data.get("intl", {}).pop("accept_languages", None)
        preferences.parent.mkdir(exist_ok=True)
        preferences.write_text(json.dumps(data))
        if profile.get("user_agent"):
            command.append(f"--user-agent={profile['user_agent']}")
        if profile.get("color_scheme") == "dark":
            command.append("--force-dark-mode")
        extensions_to_load = [str(CHROMIUM_EXTENSION)] if keepassxc else []
        if proxy:
            parsed_proxy = playwright_proxy(proxy)
            command.append(f"--proxy-server={parsed_proxy['server']}")
            if urlsplit(proxy).username is not None:
                if urlsplit(proxy).scheme == "socks5":
                    raise ValueError("Chromium does not support authenticated SOCKS5 proxies; use an HTTP(S) proxy.")
                extensions_to_load.append(str(_proxy_auth_extension(Path(profile["runtime_dir"]) / "proxy-auth", proxy, firefox=False)))
        # Merge custom extension lists to avoid last-flag-wins disabling KeePassXC.
        remaining = []
        for arg in args:
            if arg.startswith("--load-extension=") or arg.startswith("--disable-extensions-except="):
                extensions_to_load.extend(arg.split("=", 1)[1].split(","))
            else:
                remaining.append(arg)
        args = remaining
        if extensions_to_load:
            paths = ",".join(dict.fromkeys(extensions_to_load))
            command.extend([f"--load-extension={paths}", f"--disable-extensions-except={paths}"])
    return command + args


def _install_firefox_keepassxc(directory: Path, enabled: bool) -> None:
    """Install the signed persistent addon for Firefox and Camoufox alike."""
    extensions = directory / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    extension = extensions / f"{KEEPASSXC_ADDON_ID}.xpi"
    if enabled:
        if not FIREFOX_EXTENSION_XPI.is_file():
            raise RuntimeError(f"Bundled Firefox KeePassXC extension not found: {FIREFOX_EXTENSION_XPI}")
        shutil.copyfile(FIREFOX_EXTENSION_XPI, extension)
    else:
        extension.unlink(missing_ok=True)


def _request_window_close(display: str, window_ids: list[str]) -> None:
    """Send the standard WM_DELETE_WINDOW protocol directly, without a WM."""
    class Data(ctypes.Union):
        _fields_ = [("l", ctypes.c_long * 5)]

    class ClientMessage(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
            ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
            ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
            ("format", ctypes.c_int), ("data", Data),
        ]

    class Event(ctypes.Union):
        _fields_ = [("client", ClientMessage), ("padding", ctypes.c_long * 24)]

    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(Event)]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    error_handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    error_handler = error_handler_type(lambda *_: 0)
    x11.XSetErrorHandler.argtypes = [error_handler_type]
    x11.XSetErrorHandler(error_handler)
    connection = x11.XOpenDisplay(display.encode())
    if not connection:
        return
    try:
        protocol = x11.XInternAtom(connection, b"WM_PROTOCOLS", 0)
        delete = x11.XInternAtom(connection, b"WM_DELETE_WINDOW", 0)
        for window in window_ids:
            event = Event()
            event.client.type = 33  # ClientMessage
            event.client.display = connection
            event.client.window = int(window)
            event.client.message_type = protocol
            event.client.format = 32
            event.client.data.l[0] = delete
            x11.XSendEvent(connection, int(window), 0, 0, ctypes.byref(event))
        x11.XFlush(connection)
    finally:
        x11.XCloseDisplay(connection)


class NativeBrowserContext:
    """Manage a plain browser process; clipboard access uses X11, not automation."""

    pages: list[Any] = []

    def __init__(self, process: asyncio.subprocess.Process, env: dict[str, str], browser_type: str = "chromium"):
        self.process = process
        self._env = env
        self._browser_type = browser_type
        self._callbacks: list[Callable[[], Any]] = []
        self._watch_task = asyncio.create_task(self._watch())

    def on(self, event: str, callback: Callable[[], Any]) -> None:
        if event == "close":
            self._callbacks.append(callback)
            if self._watch_task.done():
                callback()

    async def _watch(self) -> None:
        returncode = await self.process.wait()
        logger.info("Native %s process exited (pid=%s, code=%s)", self._browser_type, self.process.pid, returncode)
        for callback in self._callbacks:
            callback()

    async def close(self) -> None:
        # Closing browser windows lets SQLite/LevelDB flush cookies and storage.
        # SIGTERM alone can lose writes that are still buffered in Chromium.
        if self.process.returncode is None:
            try:
                search_args = ["xdotool", "search", "--all", "--pid", str(self.process.pid)]
                if self._browser_type == "firefox":
                    # Firefox also owns an invisible application window. Only
                    # its Navigator windows represent closable browser windows.
                    search_args.extend(["--classname", "Navigator"])
                search = await asyncio.create_subprocess_exec(
                    *search_args,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self._env,
                )
                output, _ = await asyncio.wait_for(search.communicate(), 2)
                window_ids = [line for line in output.decode().splitlines() if line.isdigit()]
                if window_ids:
                    # Xlib's default error handler exits on a stale window ID.
                    # Isolate it so a window disappearing cannot stop the API.
                    closer = await asyncio.create_subprocess_exec(
                        sys.executable, "-c",
                        "import sys; from backend.browser_backends import _request_window_close; "
                        "_request_window_close(sys.argv[1], sys.argv[2:])",
                        self._env["DISPLAY"], *window_ids,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        env=self._env,
                    )
                    await asyncio.wait_for(closer.wait(), timeout=2)
                    await asyncio.wait_for(asyncio.shield(self._watch_task), timeout=30)
            except (OSError, asyncio.TimeoutError):
                pass
        if self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self._watch_task), timeout=15)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await self._watch_task


async def launch_native(profile: dict[str, Any], proxy: str | None, env: dict[str, str], keepassxc: bool) -> NativeBrowserContext:
    command = native_command(profile, proxy, keepassxc)
    log_path = Path(profile["runtime_dir"]) / "browser.log"
    with log_path.open("ab") as log:
        process = await asyncio.create_subprocess_exec(*command, env=browser_environment(profile, env), stdout=log, stderr=log, start_new_session=True)
    context = NativeBrowserContext(process, browser_environment(profile, env), profile["browser_type"])
    try:
        await asyncio.sleep(0.5)
        if process.returncode is not None:
            raise RuntimeError(f"{profile['browser_type']} exited during startup (code {process.returncode}); see {log_path}.")
        return context
    except BaseException:
        await context.close()
        raise


class CamoufoxContext:
    """Own both a persistent Playwright context and its driver lifecycle."""

    def __init__(self, context: Any, playwright: Any):
        self._context = context
        self._playwright = playwright
        self._stop_task: asyncio.Task | None = None
        self._closing = False
        context.on("close", self._on_closed)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def _stop_driver(self) -> asyncio.Task:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._playwright.stop())
        return self._stop_task

    def _on_closed(self) -> None:
        if not self._closing:
            self._stop_driver()

    async def close(self) -> None:
        self._closing = True
        try:
            await self._context.close()
        finally:
            await self._stop_driver()


def camoufox_options(profile: dict[str, Any], proxy: str | None, env: dict[str, str], keepassxc: bool) -> dict[str, Any]:
    from camoufox.utils import launch_options

    directory = Path(profile["user_data_dir"])
    _install_firefox_keepassxc(directory, keepassxc)
    keys = ("fingerprint_seed", "platform", "screen_width", "screen_height", "gpu_vendor", "gpu_renderer", "hardware_concurrency", "user_agent", "camoufox_config")
    identity = hashlib.sha256(json.dumps({key: profile.get(key) for key in keys}, sort_keys=True).encode()).hexdigest()
    path = directory / "camoufox-fingerprint.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    config = saved.get("config", {}).copy() if saved.get("identity") == identity else {}
    if saved.get("locale") != profile.get("locale"):
        config = {key: value for key, value in config.items() if not key.startswith("locale:") and key not in {"navigator.language", "navigator.languages", "headers.Accept-Language"}}
    config.update(profile.get("camoufox_config") or {})
    config.update({"screen.width": profile.get("screen_width", 1920), "screen.height": profile.get("screen_height", 1080)})
    if profile.get("timezone"):
        config["timezone"] = profile["timezone"]
    if profile.get("fingerprint_seed") is not None:
        config.setdefault("fonts:spacing_seed", profile["fingerprint_seed"] % 1_073_741_824)
    if profile.get("hardware_concurrency"):
        config["navigator.hardwareConcurrency"] = profile["hardware_concurrency"]
    if profile.get("user_agent"):
        config["navigator.userAgent"] = profile["user_agent"]
    # Runtime options must reflect edits rather than retaining values from the
    # saved identity; the remaining generated fingerprint stays stable.
    for key in ("humanize", "humanize:maxTime", "timezone"):
        if key not in (profile.get("camoufox_config") or {}):
            config.pop(key, None)
    if profile.get("timezone"):
        config["timezone"] = profile["timezone"]
    if bool(profile.get("gpu_vendor")) != bool(profile.get("gpu_renderer")):
        raise ValueError("Camoufox requires both GPU vendor and GPU renderer, or neither.")
    options = launch_options(
        config=config, os=profile.get("platform", "windows"),
        webgl_config=(profile["gpu_vendor"], profile["gpu_renderer"]) if profile.get("gpu_vendor") else None,
        headless=bool(profile.get("headless")), humanize=bool(profile.get("humanize")),
        geoip=bool(profile.get("geoip")), locale=profile.get("locale") or None,
        proxy=playwright_proxy(proxy), env=browser_environment(profile, env),
        addons=[],
        firefox_user_prefs=firefox_preferences(profile, None),
        window=(profile.get("screen_width", 1920), profile.get("screen_height", 1080)),
        args=profile.get("launch_args") or [],
        i_know_what_im_doing=True,
    )
    # Persist the generated identity, rather than rotating it on each launch.
    config_json = "".join(options["env"][f"CAMOU_CONFIG_{index}"] for index in range(1, 100) if f"CAMOU_CONFIG_{index}" in options["env"])
    if config_json:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity, "locale": profile.get("locale"), "config": json.loads(config_json)}))
    options["user_data_dir"] = str(directory)
    options["no_viewport"] = True
    if profile.get("color_scheme"):
        options["color_scheme"] = profile["color_scheme"]
    return options


async def launch_camoufox(profile: dict[str, Any], proxy: str | None, env: dict[str, str], keepassxc: bool) -> CamoufoxContext:
    from playwright.async_api import async_playwright

    options = await asyncio.to_thread(camoufox_options, profile, proxy, env, keepassxc)
    playwright = await async_playwright().start()
    try:
        context = await playwright.firefox.launch_persistent_context(**options)
        return CamoufoxContext(context, playwright)
    except BaseException:
        await playwright.stop()
        raise
