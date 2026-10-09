"""Opt-in protocol defaults, migrations, routing and launch ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import browser_backends as engines, browser_manager as manager_module, database as db, main
from backend.browser_manager import BrowserManager, RunningProfile
from backend.models import ProfileCreate


@pytest.mark.parametrize("kind", ["cloakbrowser", "chromium", "firefox", "camoufox"])
def test_remote_controls_default_off_and_roundtrip(app_client, kind):
    request = ProfileCreate(name=kind, browser_type=kind)
    assert not request.remote_debugging_enabled and not request.playwright_enabled
    profile = app_client.post("/api/profiles", json=request.model_dump()).json()
    assert profile["remote_debugging_enabled"] is False
    assert profile["playwright_enabled"] is False
    for enabled in (True, False):
        response = app_client.put(f"/api/profiles/{profile['id']}", json={"remote_debugging_enabled": enabled, "playwright_enabled": enabled and kind != "camoufox"})
        assert response.status_code == 200
        assert response.json()["remote_debugging_enabled"] == enabled
        assert response.json()["playwright_enabled"] == (enabled and kind != "camoufox")
    assert app_client.put(f"/api/profiles/{profile['id']}", json={"remote_debugging_enabled": None}).status_code == 422


def test_remote_control_migration_preserves_existing_rows(tmp_db):
    old = [db.create_profile(kind, browser_type=kind, notes="retained", launch_args=["--existing"]) for kind in ("cloakbrowser", "chromium", "firefox", "camoufox")]
    with db.get_db() as conn:
        conn.execute("ALTER TABLE profiles DROP COLUMN remote_debugging_enabled")
        conn.execute("ALTER TABLE profiles DROP COLUMN playwright_enabled")
        conn.commit()
    db.init_db()
    db.init_db()
    for profile in old:
        assert db.get_profile(profile["id"]) == profile


def test_camoufox_controls_are_mutually_exclusive_on_create_and_partial_update(app_client):
    assert app_client.post("/api/profiles", json={"name": "conflict", "browser_type": "camoufox", "remote_debugging_enabled": True, "playwright_enabled": True}).status_code == 422
    profile = app_client.post("/api/profiles", json={"name": "Camoufox", "browser_type": "camoufox", "playwright_enabled": True}).json()
    path = f"/api/profiles/{profile['id']}"
    assert app_client.put(path, json={"remote_debugging_enabled": True}).status_code == 422
    assert app_client.get(path).json()["playwright_enabled"] is True
    assert app_client.put(path, json={"remote_debugging_enabled": True, "playwright_enabled": False}).status_code == 200
    assert app_client.put(path, json={"playwright_enabled": True}).status_code == 422


@pytest.mark.parametrize("kind,debugging,playwright,ports", [
    ("cloakbrowser", False, False, (5101, None, None)),
    ("chromium", False, False, (None, None, None)),
    ("chromium", True, True, (5101, None, None)),
    ("firefox", True, True, (None, 5101, None)),
    ("camoufox", False, True, (None, None, 5101)),
    ("camoufox", True, False, (None, 5101, None)),
])
@pytest.mark.asyncio
async def test_manager_allocates_only_requested_protocols(tmp_path, monkeypatch, kind, debugging, playwright, ports):
    manager = BrowserManager()
    manager.vnc.allocate = AsyncMock(return_value=(123, 6123))
    manager.vnc.start_vnc = AsyncMock()
    manager.vnc.stop_vnc = AsyncMock()
    manager._allocate_cdp_port = MagicMock(side_effect=[5101, 5102])
    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    launch = AsyncMock(return_value=context)
    target = "launch_persistent_context_async" if kind == "cloakbrowser" else "launch_camoufox" if kind == "camoufox" else "launch_native"
    monkeypatch.setattr(manager_module, target, launch)
    profile = {"id": "protocol", "browser_type": kind, "user_data_dir": str(tmp_path), "keepassxc_enabled": False, "headless": True,
               "remote_debugging_enabled": debugging, "playwright_enabled": playwright}
    running = await manager.launch(profile)
    assert (running.cdp_port, running.bidi_port, running.playwright_port) == ports
    if kind != "cloakbrowser":
        options = launch.call_args.args[0]
        assert options["remote_debugging_port"] == (ports[0] or ports[1])
        assert options["playwright_port"] == ports[2]
    await manager.stop(profile["id"])


@pytest.mark.parametrize("kind", ["chromium", "firefox"])
def test_native_debugging_uses_only_assigned_port(tmp_path, kind):
    profile = {"browser_type": kind, "user_data_dir": str(tmp_path), "runtime_dir": str(tmp_path), "remote_debugging_port": 5177}
    command = engines.native_command(profile, None, False)
    assert "--remote-debugging-port=5177" in command
    assert not any("marionette" in arg or "enable-automation" in arg for arg in command)
    profile["launch_args"] = ["--remote-debugging-port=9999"]
    with pytest.raises(ValueError):
        engines.native_command(profile, None, False)


@pytest.mark.parametrize("protocol", ["bidi", "playwright"])
def test_protocol_proxy_rejects_disabled_and_cross_origin(app_client, monkeypatch, protocol):
    running = RunningProfile("protocol", MagicMock(), 100, 6100, None)
    monkeypatch.setitem(main.browser_mgr.running, "protocol", running)
    path = f"/api/profiles/protocol/{protocol}"
    assert app_client.get(path).status_code == 400
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as disabled:
        with app_client.websocket_connect(path):
            pass
    assert disabled.value.code == 4005
    with pytest.raises(WebSocketDisconnect) as cross_origin:
        with app_client.websocket_connect(path, headers={"origin": "https://other.example", "host": "testserver"}):
            pass
    assert cross_origin.value.code == 4403
    setattr(running, f"{protocol}_port", 5177)
    response = app_client.get(path, headers={"x-forwarded-proto": "https"})
    assert response.json()["endpoint"] == f"wss://testserver{path}"


@pytest.mark.asyncio
async def test_camoufox_server_shares_persistent_profile_and_cleans_up(tmp_path, monkeypatch):
    page = SimpleNamespace(close=AsyncMock())
    context = MagicMock(pages=[page])
    context.close = AsyncMock()
    browser = SimpleNamespace(contexts=[context])
    playwright = SimpleNamespace(firefox=SimpleNamespace(connect=AsyncMock(return_value=browser), launch_persistent_context=AsyncMock()), stop=AsyncMock())
    from playwright import async_api as playwright_api
    monkeypatch.setattr(playwright_api, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)))
    driver = playwright
    monkeypatch.setattr(engines, "camoufox_options", lambda *_: {"user_data_dir": str(tmp_path)})
    process = MagicMock()
    server = AsyncMock(return_value=(process, "ws://127.0.0.1:5178/profile"))
    stop = AsyncMock()
    monkeypatch.setattr(engines, "_launch_camoufox_server", server)
    monkeypatch.setattr(engines, "_stop_camoufox_server", stop)
    wrapper = await engines.launch_camoufox({"playwright_port": 5178}, None, {}, False)
    assert wrapper._context is context
    driver.firefox.connect.assert_awaited_once_with("ws://127.0.0.1:5178/profile")
    driver.firefox.launch_persistent_context.assert_not_awaited()
    await wrapper.close()
    context.close.assert_not_awaited()
    page.close.assert_awaited_once()
    stop.assert_awaited_once_with(process)
    driver.stop.assert_awaited_once()
