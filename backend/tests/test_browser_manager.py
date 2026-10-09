"""Tests for browser_manager pure functions — proxy parsing, fingerprint args, profile defaults."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import socket

from backend.browser_manager import (
    BASE_CDP_PORT,
    CDP_PORT_RANGE,
    KEEPASSXC_EXTENSION_ARG,
    KEEPASSXC_EXTENSION_DIR,
    KEEPASSXC_PASSWORD_ENV,
    BrowserLaunchError,
    KeePassXCPaths,
    KeePassXCUnavailableError,
    RunningProfile,
    _build_profile_process_env,
    _ensure_keepassxc_config,
    _init_profile_defaults,
    _keepassxc_paths,
    _launch_args_include_keepassxc,
    _normalize_proxy,
    _set_ini_root_value,
    _validate_proxy,
    require_keepassxc_password,
    BrowserManager,
)


# ── _normalize_proxy ─────────────────────────────────────────────────────────


def test_normalize_already_http():
    assert _normalize_proxy("http://user:pass@host:8080") == "http://user:pass@host:8080"


def test_normalize_already_https():
    assert _normalize_proxy("https://host:443") == "https://host:443"


def test_normalize_already_socks5():
    assert _normalize_proxy("socks5://host:1080") == "socks5://host:1080"


def test_normalize_host_port_user_pass():
    assert _normalize_proxy("proxy.com:8080:myuser:mypass") == "http://myuser:mypass@proxy.com:8080"


def test_normalize_host_port_only():
    assert _normalize_proxy("proxy.com:8080") == "http://proxy.com:8080"


def test_normalize_three_parts():
    # 3 parts doesn't match any pattern — returned as-is
    assert _normalize_proxy("a:b:c") == "a:b:c"


def test_normalize_five_parts():
    # 5 parts doesn't match — returned as-is
    assert _normalize_proxy("a:b:c:d:e") == "a:b:c:d:e"


def test_normalize_empty_parts():
    # host:port:user:pass with empty parts
    result = _normalize_proxy(":8080:user:pass")
    assert result == "http://user:pass@:8080"


# ── _validate_proxy ──────────────────────────────────────────────────────────


def test_validate_valid_http():
    _validate_proxy("http://proxy.com:8080")  # should not raise


def test_validate_valid_socks5():
    _validate_proxy("socks5://proxy.com:1080")  # should not raise


def test_validate_valid_with_auth():
    _validate_proxy("http://user:pass@proxy.com:8080")  # should not raise


def test_validate_bad_scheme():
    with pytest.raises(ValueError, match="Invalid proxy scheme 'ftp'"):
        _validate_proxy("ftp://host:80")


def test_validate_no_hostname():
    with pytest.raises(ValueError, match="missing hostname"):
        _validate_proxy("http://:8080")


def test_validate_no_port():
    with pytest.raises(ValueError, match="missing port"):
        _validate_proxy("http://host")


# ── _build_fingerprint_args ──────────────────────────────────────────────────

# Use the BrowserManager instance to call the method
_mgr = BrowserManager()


def test_build_args_always_includes_base():
    args = _mgr._build_fingerprint_args({})
    assert "--disable-infobars" in args
    assert "--test-type" in args
    assert "--use-angle=swiftshader" in args


def test_build_args_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": 42})
    assert "--fingerprint=42" in args


def test_build_args_no_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": None})
    assert not any(a.startswith("--fingerprint=") for a in args)


def test_build_args_platform():
    args = _mgr._build_fingerprint_args({"platform": "macos"})
    assert "--fingerprint-platform=macos" in args


def test_build_args_gpu():
    args = _mgr._build_fingerprint_args({
        "gpu_vendor": "NVIDIA Corporation",
        "gpu_renderer": "NVIDIA GeForce RTX 3070",
    })
    assert "--fingerprint-gpu-vendor=NVIDIA Corporation" in args
    assert "--fingerprint-gpu-renderer=NVIDIA GeForce RTX 3070" in args


def test_build_args_hardware_concurrency():
    args = _mgr._build_fingerprint_args({"hardware_concurrency": 8})
    assert "--fingerprint-hardware-concurrency=8" in args


def test_build_args_screen():
    args = _mgr._build_fingerprint_args({"screen_width": 2560, "screen_height": 1440})
    assert "--fingerprint-screen-width=2560" in args
    assert "--fingerprint-screen-height=1440" in args


def test_build_args_empty_profile():
    args = _mgr._build_fingerprint_args({})
    # Only the 3 base args
    assert len(args) == 3


# ── launch_args appended to extra_args ────────────────────────────────────────


def test_launch_args_appended_to_fingerprint_args():
    """launch_args from profile should appear in the args list after fingerprint args."""
    profile = {
        "fingerprint_seed": 42,
        "platform": "windows",
        "launch_args": ["--load-extension=/tmp/ext", "--disable-features=Foo"],
    }
    args = _mgr._build_fingerprint_args(profile)
    args += profile.get("launch_args") or []
    assert "--load-extension=/tmp/ext" in args
    assert "--disable-features=Foo" in args
    # Fingerprint args still present
    assert "--fingerprint=42" in args


def test_launch_args_empty_no_effect():
    profile = {"launch_args": []}
    args = _mgr._build_fingerprint_args(profile)
    base_count = len(args)
    args += profile.get("launch_args") or []
    assert len(args) == base_count


def test_launch_args_none_no_effect():
    profile = {"launch_args": None}
    args = _mgr._build_fingerprint_args(profile)
    base_count = len(args)
    args += profile.get("launch_args") or []
    assert len(args) == base_count


# ── KeePassXC launch argument detection ─────────────────────────────────────


def test_keepassxc_extension_arg_is_detected():
    assert _launch_args_include_keepassxc([KEEPASSXC_EXTENSION_ARG]) is True


def test_keepassxc_extension_in_comma_separated_list_is_detected():
    arg = f"--load-extension=/tmp/other,{KEEPASSXC_EXTENSION_DIR}"
    assert _launch_args_include_keepassxc([arg]) is True


def test_keepassxc_extension_separate_value_is_detected():
    assert _launch_args_include_keepassxc([
        "--load-extension",
        str(KEEPASSXC_EXTENSION_DIR),
    ]) is True


def test_similar_keepassxc_extension_path_is_not_detected():
    assert _launch_args_include_keepassxc([
        f"--load-extension={KEEPASSXC_EXTENSION_DIR}-copy",
    ]) is False


def test_missing_keepassxc_extension_arg_is_disabled():
    assert _launch_args_include_keepassxc(["--disable-features=Foo"]) is False


def test_allow_arg_without_load_arg_does_not_enable_keepassxc():
    assert _launch_args_include_keepassxc([
        f"--disable-extensions-except={KEEPASSXC_EXTENSION_DIR}",
    ]) is False


# ── KeePassXC configuration and environment ─────────────────────────────────


def _test_keepassxc_paths(tmp_path: Path) -> KeePassXCPaths:
    data_dir = tmp_path / "KeePassXC"
    runtime_dir = tmp_path / "runtime"
    return KeePassXCPaths(
        data_dir=data_dir,
        database=data_dir / "passwords.kdbx",
        config=data_dir / "keepassxc.ini",
        local_config=data_dir / "keepassxc-local.ini",
        runtime_dir=runtime_dir,
        socket=runtime_dir / "app/server",
        log=tmp_path / "keepassxc.log",
    )


def test_keepassxc_runtime_path_is_unique_and_fits_unix_socket_limit(tmp_path: Path):
    first = _keepassxc_paths(
        "1ddf0705-3816-4258-b749-1f8b1d1a432f",
        tmp_path / "first",
    )
    second = _keepassxc_paths(
        "917e1609-1b54-4761-90b4-f8bfdb04a691",
        tmp_path / "second",
    )

    assert first.runtime_dir != second.runtime_dir
    assert first.runtime_dir.parent == Path("/tmp/cbm")
    assert len(first.runtime_dir.name) == 22
    assert len(os.fsencode(first.socket)) < 108


def test_set_ini_root_value_updates_only_root_key():
    lines = ["SingleInstance=true", "", "[General]", "SingleInstance=true"]
    _set_ini_root_value(lines, "SingleInstance", "false")
    assert lines == ["SingleInstance=false", "", "[General]", "SingleInstance=true"]


def test_ensure_keepassxc_config_enables_browser_and_preserves_settings(tmp_path: Path):
    paths = _test_keepassxc_paths(tmp_path)
    paths.data_dir.mkdir()
    paths.config.write_text(
        "Theme=dark\n\n"
        "[Browser]\nShowNotification=false\n\n"
        "[Security]\nLockDatabaseIdle=true\nLockDatabaseMinimize=true\n"
    )

    _ensure_keepassxc_config(paths)

    config = paths.config.read_text()
    assert "Theme=dark" in config
    assert "SingleInstance=false" in config
    assert "[Browser]" in config
    assert "Enabled=true" in config
    assert "UpdateBinaryPath=false" in config
    assert "ShowNotification=false" in config
    assert "UpdateCheckMessageShown=true" in config
    assert "CheckForUpdates=false" in config
    assert "[Security]" in config
    assert "LockDatabaseIdle=false" in config
    assert "LockDatabaseMinimize=false" in config
    assert "LockDatabaseScreenLock=false" in config
    assert "LockDatabaseOnUserSwitch=false" in config
    assert "LockDatabaseIdle=true" not in config
    assert "LockDatabaseMinimize=true" not in config
    assert paths.local_config.exists()
    assert paths.config.stat().st_mode & 0o777 == 0o600


def test_profile_process_env_removes_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(KEEPASSXC_PASSWORD_ENV, "top-secret")
    paths = _test_keepassxc_paths(tmp_path)

    env = _build_profile_process_env(123, paths.runtime_dir, keepassxc_paths=paths)

    assert KEEPASSXC_PASSWORD_ENV not in env
    assert env["DISPLAY"] == ":123"
    assert env["XDG_RUNTIME_DIR"] == str(paths.runtime_dir)
    assert env["TMPDIR"] == str(paths.runtime_dir)
    assert env["KPXC_CONFIG"] == str(paths.config)


def test_manager_loads_password_once_and_removes_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(KEEPASSXC_PASSWORD_ENV, "top-secret")
    manager = BrowserManager()

    manager.load_keepassxc_password()

    assert manager._keepassxc_password == "top-secret"
    assert KEEPASSXC_PASSWORD_ENV not in os.environ


def test_keepassxc_password_rejects_newlines(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(KEEPASSXC_PASSWORD_ENV, "first-line\nsecond-line")

    with pytest.raises(RuntimeError, match="newline"):
        require_keepassxc_password()


@pytest.mark.asyncio
async def test_create_database_sends_password_twice(tmp_path: Path):
    manager = BrowserManager()
    manager._run_keepassxc_cli = AsyncMock(return_value=(0, ""))
    database = tmp_path / "passwords.kdbx"

    await manager._create_keepassxc_database(database, "secret", {})

    manager._run_keepassxc_cli.assert_awaited_once_with(
        ["db-create", "--quiet", "--set-password", str(database)],
        "secret\nsecret\n",
        {},
    )


@pytest.mark.asyncio
async def test_verify_database_failure_is_user_actionable(tmp_path: Path):
    manager = BrowserManager()
    manager._run_keepassxc_cli = AsyncMock(return_value=(1, "Invalid credentials"))

    with pytest.raises(BrowserLaunchError, match=KEEPASSXC_PASSWORD_ENV):
        await manager._verify_keepassxc_database(
            tmp_path / "passwords.kdbx",
            "wrong-password",
            {},
        )


@pytest.mark.asyncio
async def test_cleanup_keepassxc_terminates_process_and_runtime(tmp_path: Path):
    manager = BrowserManager()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "socket").touch()
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=0)

    await manager._cleanup_keepassxc(process, runtime_dir)

    process.terminate.assert_called_once()
    process.wait.assert_awaited_once()
    assert not runtime_dir.exists()


@pytest.mark.asyncio
async def test_keepassxc_readiness_waits_for_unlocked_database_window(tmp_path: Path):
    manager = BrowserManager()
    process = MagicMock(pid=4242, returncode=None)
    paths = _test_keepassxc_paths(tmp_path)
    manager._run_xdotool = AsyncMock(side_effect=[
        (1, ""), (0, "passwords.kdbx [Locked] - KeePassXC"),
        (0, "passwords.kdbx - KeePassXC"),
    ])
    await manager._wait_for_keepassxc_window_ready(process, paths, 123)
    assert manager._run_xdotool.await_count == 3


def _running_profile_with_keepassxc(tmp_path: Path) -> tuple[RunningProfile, MagicMock]:
    process = MagicMock()
    process.pid = 4242
    process.returncode = None
    running = RunningProfile(
        profile_id="profile-with-keepassxc",
        context=MagicMock(),
        display=123,
        ws_port=6123,
        cdp_port=5123,
        keepassxc_process=process,
        keepassxc_runtime_dir=tmp_path / "runtime",
    )
    return running, process


@pytest.mark.asyncio
async def test_toggle_keepassxc_window_minimizes_when_focused(tmp_path: Path):
    manager = BrowserManager()
    running, _ = _running_profile_with_keepassxc(tmp_path)
    running.user_data_dir = tmp_path / "profile"
    manager.running[running.profile_id] = running
    manager._find_keepassxc_window = AsyncMock(
        return_value=("12345", ["12345", "12346"])
    )
    manager._find_chromium_window = AsyncMock(return_value="54321")
    manager._maximize_chromium_window = AsyncMock()
    manager._run_xdotool = AsyncMock(side_effect=[
        (0, "98765"),
        (0, "4242"),
        (0, ""),
        (0, ""),
    ])

    state = await manager.toggle_keepassxc_window(running.profile_id)

    assert state == "minimized"
    assert manager._run_xdotool.await_args_list == [
        ((123, running.keepassxc_runtime_dir, ["getwindowfocus"]),),
        ((123, running.keepassxc_runtime_dir, ["getwindowpid", "98765"]),),
        ((123, running.keepassxc_runtime_dir, ["windowunmap", "--sync", "12346"]),),
        ((123, running.keepassxc_runtime_dir, ["windowunmap", "--sync", "12345"]),),
    ]
    manager._find_chromium_window.assert_awaited_once_with(
        running,
        running.keepassxc_runtime_dir,
    )
    manager._maximize_chromium_window.assert_awaited_once_with(
        running,
        running.keepassxc_runtime_dir,
        window_id="54321",
    )


@pytest.mark.asyncio
async def test_maximize_chromium_window_fills_display(tmp_path: Path):
    manager = BrowserManager()
    running, _ = _running_profile_with_keepassxc(tmp_path)
    manager._get_display_geometry = AsyncMock(return_value=(1280, 720))
    manager._find_chromium_window = AsyncMock(return_value="54321")
    manager._run_xdotool = AsyncMock(side_effect=[
        (0, ""),
        (0, ""),
        (0, ""),
        (0, ""),
        (0, ""),
        (0, "X=0\nY=0\nWIDTH=1280\nHEIGHT=720"),
    ])

    await manager._maximize_chromium_window(
        running,
        running.keepassxc_runtime_dir,
    )

    commands = [call.args[2] for call in manager._run_xdotool.await_args_list]
    assert commands == [
        ["windowmap", "--sync", "54321"],
        ["windowmove", "--sync", "54321", "0", "0"],
        ["windowsize", "--sync", "54321", "1280", "720"],
        ["windowraise", "54321"],
        ["windowfocus", "--sync", "54321"],
        ["getwindowgeometry", "--shell", "54321"],
    ]


@pytest.mark.asyncio
async def test_toggle_keepassxc_window_restores_and_maximizes(tmp_path: Path):
    manager = BrowserManager()
    running, _ = _running_profile_with_keepassxc(tmp_path)
    manager.running[running.profile_id] = running
    manager._find_keepassxc_window = AsyncMock(
        return_value=("12345", ["12345", "12346"])
    )
    manager._run_xdotool = AsyncMock(side_effect=[
        (0, "98765"),
        (0, "7777"),
        (0, "1280 720"),
        (0, ""),
        (0, ""),
        (0, ""),
        (0, ""),
        (0, ""),
        (0, ""),
        (0, ""),
    ])

    state = await manager.toggle_keepassxc_window(running.profile_id)

    assert state == "shown"
    commands = [call.args[2] for call in manager._run_xdotool.await_args_list]
    assert commands == [
        ["getwindowfocus"],
        ["getwindowpid", "98765"],
        ["getdisplaygeometry"],
        ["windowmap", "--sync", "12345"],
        ["windowmap", "--sync", "12346"],
        ["windowmove", "12345", "0", "0"],
        ["windowsize", "12345", "1280", "720"],
        ["windowraise", "12345"],
        ["windowraise", "12346"],
        ["windowfocus", "--sync", "12346"],
    ]


@pytest.mark.asyncio
async def test_toggle_keepassxc_window_rejects_unpaired_profile(tmp_path: Path):
    manager = BrowserManager()
    manager.running["without-keepassxc"] = RunningProfile(
        profile_id="without-keepassxc",
        context=MagicMock(),
        display=123,
        ws_port=6123,
        cdp_port=5123,
    )

    with pytest.raises(KeePassXCUnavailableError, match="not enabled"):
        await manager.toggle_keepassxc_window("without-keepassxc")


# ── _allocate_cdp_port ───────────────────────────────────────────────────────


def test_allocate_cdp_port_returns_free_port():
    mgr = BrowserManager()
    port = mgr._allocate_cdp_port()
    assert BASE_CDP_PORT <= port < BASE_CDP_PORT + CDP_PORT_RANGE


def test_allocate_cdp_port_skips_occupied():
    mgr = BrowserManager()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", BASE_CDP_PORT))
        blocker.listen(1)
        port = mgr._allocate_cdp_port()
        assert port == BASE_CDP_PORT + 1


def test_allocate_cdp_port_advances_counter():
    mgr = BrowserManager()
    p1 = mgr._allocate_cdp_port()
    p2 = mgr._allocate_cdp_port()
    assert p2 == p1 + 1


def test_allocate_cdp_port_wraps_around():
    mgr = BrowserManager()
    mgr._next_cdp_port = BASE_CDP_PORT + CDP_PORT_RANGE - 1
    p1 = mgr._allocate_cdp_port()
    assert p1 == BASE_CDP_PORT + CDP_PORT_RANGE - 1
    p2 = mgr._allocate_cdp_port()
    assert p2 == BASE_CDP_PORT


def test_allocate_cdp_port_all_occupied_raises():
    mgr = BrowserManager()
    blockers = []
    try:
        for i in range(CDP_PORT_RANGE):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", BASE_CDP_PORT + i))
            s.listen(1)
            blockers.append(s)
        with pytest.raises(ValueError, match="No free CDP ports"):
            mgr._allocate_cdp_port()
    finally:
        for s in blockers:
            s.close()


# ── _init_profile_defaults ───────────────────────────────────────────────────


def test_init_creates_bookmarks(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    assert bookmarks_path.exists()
    data = json.loads(bookmarks_path.read_text())
    children = data["roots"]["bookmark_bar"]["children"]
    assert len(children) == 4  # 4 folders
    folder_names = {f["name"] for f in children}
    assert folder_names == {"Detection Tests", "Fingerprint", "Headers & TLS", "reCAPTCHA"}


def test_init_creates_preferences(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    prefs_path = tmp_path / "Default" / "Preferences"
    assert prefs_path.exists()
    data = json.loads(prefs_path.read_text())
    assert "default_search_provider_data" in data
    assert "DuckDuckGo" in data["default_search_provider_data"]["template_url_data"]["short_name"]


def test_init_idempotent(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    original = bookmarks_path.read_text()

    # Write a sentinel to the file
    bookmarks_path.write_text("SENTINEL")

    # Second call should NOT overwrite (file already exists)
    _init_profile_defaults(tmp_path)
    assert bookmarks_path.read_text() == "SENTINEL"
