"""Browser-type contracts, lossless migration and native launch behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from backend import browser_backends as engines
from backend import browser_manager as manager_module
from backend import database as db
from backend.browser_manager import BrowserManager, RunningProfile
from backend.models import ProfileCreate, ProfileUpdate


@pytest.mark.parametrize("browser_type", ["cloakbrowser", "chromium", "firefox", "camoufox"])
def test_type_and_shared_settings_roundtrip(tmp_db, browser_type):
    request = ProfileCreate(name=browser_type, browser_type=browser_type, timezone="Asia/Taipei", locale="zh-TW", proxy="https://proxy:443", notes="keep", launch_args=["--custom"], firefox_preferences={"privacy.globalprivacycontrol.enabled": True}, camoufox_config={"navigator.hardwareConcurrency": 8})
    profile = db.create_profile(**request.model_dump())
    assert db.get_profile(profile["id"])["browser_type"] == browser_type
    assert db.list_profiles()[0]["firefox_preferences"] == request.firefox_preferences
    assert profile["timezone"] == "Asia/Taipei"
    assert profile["launch_args"] == ["--custom"]
    if browser_type != "cloakbrowser":
        assert profile["keepassxc_enabled"] == 1


def test_type_immutable_in_model_and_database(sample_profile):
    with pytest.raises(ValidationError, match="cannot be changed"):
        ProfileUpdate(browser_type="firefox")
    with pytest.raises(ValueError, match="cannot be changed"):
        db.update_profile(sample_profile["id"], browser_type="firefox", name="changed")
    assert db.get_profile(sample_profile["id"]) == sample_profile


def test_upgrade_preserves_legacy_data_and_files(tmp_db):
    legacy = db.create_profile("Legacy", fingerprint_seed=12345, launch_args=["--custom"], notes="unchanged", tags=[{"tag": "work", "color": "#fff"}])
    directory = Path(legacy["user_data_dir"])
    directory.mkdir(parents=True)
    for name in ("Cookies", "Bookmarks", "KeePassXC/passwords.kdbx"):
        path = directory / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"original data\x00\xff")
    new_fields = ("browser_type", "use_gpu", "keepassxc_enabled", "firefox_preferences", "camoufox_config")
    with db.get_db() as conn:
        for field in new_fields:
            conn.execute(f"ALTER TABLE profiles DROP COLUMN {field}")
        conn.commit()
    for _ in range(3):
        db.init_db()
    upgraded = db.get_profile(legacy["id"])
    assert upgraded["browser_type"] == "cloakbrowser"
    assert upgraded["keepassxc_enabled"] is None
    assert {k: v for k, v in upgraded.items() if k not in new_fields} == {k: v for k, v in legacy.items() if k not in new_fields}
    for name in ("Cookies", "Bookmarks", "KeePassXC/passwords.kdbx"):
        assert (directory / name).read_bytes() == b"original data\x00\xff"


@pytest.mark.parametrize("browser_type", ["chromium", "firefox"])
def test_native_commands_have_no_automation_and_share_settings(tmp_path, browser_type):
    profile = {"browser_type": browser_type, "user_data_dir": str(tmp_path), "runtime_dir": str(tmp_path), "timezone": "Asia/Taipei", "locale": "zh-TW", "screen_width": 1280, "screen_height": 720, "launch_args": ["--custom"]}
    command = engines.native_command(profile, "https://proxy:443", False)
    assert command[-1] == "--custom"
    assert not any("debugging" in arg or "automation" in arg or "juggler" in arg or "marionette" in arg or "fingerprint=" in arg for arg in command)
    env = engines.browser_environment(profile, {"https_proxy": "http://unexpected:80"})
    assert env["TZ"] == "Asia/Taipei"
    assert "https_proxy" not in env
    if browser_type == "firefox":
        prefs = (tmp_path / "user.js").read_text()
        assert '"browser.contentblocking.category", "strict"' in prefs
        assert '"network.proxy.ssl", "proxy"' in prefs
        assert '"intl.accept_languages", "zh-TW"' in prefs
    else:
        assert "--proxy-server=https://proxy:443" in command
        assert "--window-size=1280,720" in command


@pytest.mark.parametrize("arg", ["--enable-automation", "--remote-debugging-port=9222", "--remote-debugging-pipe", "--marionette", "--juggler=1234", "--headless=new"])
def test_native_debug_flags_rejected(tmp_path, arg):
    with pytest.raises(ValueError, match="automation"):
        engines.native_command({"browser_type": "chromium", "user_data_dir": str(tmp_path), "launch_args": [arg]}, None, False)


def test_firefox_managed_preferences_preserve_user_js(tmp_path):
    path = tmp_path / "user.js"
    path.write_text('// User settings\nuser_pref("custom.setting", true);\n')
    engines._write_firefox_preferences(tmp_path, {"intl.accept_languages": "zh-TW"})
    engines._write_firefox_preferences(tmp_path, {"intl.accept_languages": "en-US"})
    text = path.read_text()
    assert 'user_pref("custom.setting", true);' in text
    assert text.count("BEGIN CloakBrowser Manager") == 1
    assert "zh-TW" not in text


def test_gpu_only_for_non_cloakbrowser(monkeypatch):
    monkeypatch.setattr(Path, "glob", lambda *_: [Path("/dev/dri/renderD128")])
    monkeypatch.setattr(engines.os, "access", lambda *_: True)
    assert engines.gpu_available({"browser_type": "chromium"})
    assert not engines.gpu_available({"browser_type": "cloakbrowser"})
    assert not engines.gpu_available({"browser_type": "firefox", "use_gpu": False})
    monkeypatch.setattr(engines.os, "access", lambda *_: False)
    assert not engines.gpu_available({"browser_type": "camoufox"})


def test_native_chromium_merges_keepass_and_proxy_auth(tmp_path):
    command = engines.native_command({"browser_type": "chromium", "user_data_dir": str(tmp_path), "runtime_dir": str(tmp_path), "launch_args": ["--load-extension=/other"]}, "http://user:pass@proxy:8080", True)
    extension_arg = next(arg for arg in command if arg.startswith("--load-extension="))
    assert str(engines.CHROMIUM_EXTENSION) in extension_arg
    assert str(tmp_path / "proxy-auth") in extension_arg
    assert "/other" in extension_arg
    assert "user:pass" not in " ".join(command)
    script = (tmp_path / "proxy-auth/background.js").read_text()
    assert "details.isProxy" in script
    assert "details.challenger.host !== host" in script


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_type", ["chromium", "firefox", "camoufox"])
async def test_manager_routes_browser_without_cdp(tmp_path, monkeypatch, browser_type):
    manager = BrowserManager()
    manager.vnc.allocate = AsyncMock(return_value=(123, 6123))
    manager.vnc.start_vnc = AsyncMock()
    manager.vnc.stop_vnc = AsyncMock()
    manager._allocate_cdp_port = MagicMock(side_effect=AssertionError("No CDP"))
    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    launch = AsyncMock(return_value=context)
    monkeypatch.setattr(manager_module, "launch_camoufox" if browser_type == "camoufox" else "launch_native", launch)
    profile = {"id": "native", "browser_type": browser_type, "user_data_dir": str(tmp_path), "keepassxc_enabled": False, "headless": True}
    running = await manager.launch(profile)
    assert running.cdp_port is None
    assert manager.get_status("native")["cdp_url"] is None
    if browser_type != "camoufox":
        context.add_init_script.assert_not_awaited()
    await manager.stop("native")
    context.close.assert_awaited_once()
    manager.vnc.stop_vnc.assert_awaited_once_with(123)


def test_camoufox_generated_identity_persists_but_behavior_updates(tmp_path, monkeypatch):
    calls = []
    def launch_options(**kwargs):
        calls.append(kwargs)
        config = {**kwargs["config"]}
        config.setdefault("navigator.userAgent", f"random-{len(calls)}")
        if kwargs["humanize"]:
            config["humanize"] = True
        return {"env": {"CAMOU_CONFIG_1": json.dumps(config)}}
    monkeypatch.setitem(sys.modules, "camoufox.utils", SimpleNamespace(launch_options=launch_options))
    profile = {"browser_type": "camoufox", "user_data_dir": str(tmp_path), "fingerprint_seed": 42, "humanize": True}
    first = engines.camoufox_options(profile, None, {}, False)
    second = engines.camoufox_options({**profile, "humanize": False}, None, {}, False)
    config = json.loads(second["env"]["CAMOU_CONFIG_1"])
    assert {key: config[key] for key in ("screen.availLeft", "screen.availTop", "screen.availWidth", "screen.availHeight")} == {
        "screen.availLeft": 0, "screen.availTop": 0, "screen.availWidth": 1920, "screen.availHeight": 1080,
    }
    assert config["navigator.userAgent"] == json.loads(first["env"]["CAMOU_CONFIG_1"])["navigator.userAgent"]
    assert "humanize" not in config
    assert second["user_data_dir"] == str(tmp_path)
    engines.camoufox_options({**profile, "fingerprint_seed": 43}, None, {}, False)
    assert calls[-1]["config"].get("navigator.userAgent") is None


@pytest.mark.parametrize("fonts", [None, [], ["Arimo", "Tinos", "Cousine"]])
def test_camoufox_image_fonts_require_explicit_font_identity(tmp_path, monkeypatch, fonts):
    import xml.etree.ElementTree as ET

    bundled = str(tmp_path / "bundled & isolated.conf")
    def launch_options(**kwargs):
        return {"env": {"FONTCONFIG_FILE": bundled, "CAMOU_CONFIG_1": json.dumps(kwargs["config"])}}
    monkeypatch.setitem(sys.modules, "camoufox.utils", SimpleNamespace(launch_options=launch_options))
    profile = {"browser_type": "camoufox", "user_data_dir": str(tmp_path), "camoufox_config": {"fonts": fonts} if fonts is not None else {}}
    options = engines.camoufox_options(profile, None, {}, False)
    if fonts:
        config = ET.parse(options["env"]["FONTCONFIG_FILE"]).getroot()
        assert config.find("include").text == bundled
        assert config.find("dir").text == "/usr/share/fonts"
        assert json.loads(options["env"]["CAMOU_CONFIG_1"])["fonts"] == fonts
    else:
        assert options["env"]["FONTCONFIG_FILE"] == bundled
        assert not (tmp_path / "camoufox-fonts.conf").exists()


@pytest.mark.asyncio
async def test_camoufox_finishes_persistent_context_close_before_stopping_driver():
    callbacks = {}
    actions = []
    context = MagicMock()
    context.on.side_effect = lambda event, callback: callbacks.update({event: callback})

    async def close():
        callbacks["close"]()
        await asyncio.sleep(0)
        assert actions == []
        actions.append("storage flushed")

    context.close = close
    playwright = SimpleNamespace(stop=AsyncMock(side_effect=lambda: actions.append("driver stopped")))
    wrapper = engines.CamoufoxContext(context, playwright)
    await wrapper.close()
    assert actions == ["storage flushed", "driver stopped"]


@pytest.mark.asyncio
async def test_native_firefox_closes_navigator_windows_without_terminating_process(monkeypatch):
    exited = asyncio.Event()
    process = SimpleNamespace(pid=123, returncode=None)

    async def wait():
        await exited.wait()
        return process.returncode

    process.wait = wait
    search = SimpleNamespace(communicate=AsyncMock(return_value=(b"456\n", b"")))

    async def finish_close():
        process.returncode = 0
        exited.set()

    closer = SimpleNamespace(wait=finish_close)
    spawn = AsyncMock(side_effect=[search, closer])
    monkeypatch.setattr(engines.asyncio, "create_subprocess_exec", spawn)
    kill = MagicMock(side_effect=AssertionError("Should close normally"))
    monkeypatch.setattr(engines.os, "killpg", kill)
    context = engines.NativeBrowserContext(process, {"DISPLAY": ":123"}, "firefox")
    await context.close()
    assert "Navigator" in spawn.await_args_list[0].args
    assert spawn.await_args_list[1].args[-1] == "456"
    kill.assert_not_called()


@pytest.mark.parametrize("browser_type", ["chromium", "firefox", "camoufox"])
def test_api_create_type_immutable_and_no_cdp(app_client, monkeypatch, browser_type):
    response = app_client.post("/api/profiles", json={"name": browser_type, "browser_type": browser_type})
    assert response.status_code == 201
    profile = response.json()
    assert profile["browser_type"] == browser_type
    assert app_client.put(f"/api/profiles/{profile['id']}", json={"browser_type": "cloakbrowser"}).status_code == 422
    from backend import main
    monkeypatch.setitem(main.browser_mgr.running, profile["id"], RunningProfile(profile_id=profile["id"], context=MagicMock(), display=100, ws_port=6100, cdp_port=None, browser_type=browser_type))
    for suffix in ("cdp", "cdp/json/version", "cdp/json/list"):
        assert app_client.get(f"/api/profiles/{profile['id']}/{suffix}").status_code == 400
