"""WebRTC mode defaults, lossless migration and reversible engine settings."""

import asyncio
import json
from urllib.parse import urlsplit

import pytest

from backend import browser_backends as engines, database as db
from backend.models import ProfileCreate
from backend.webrtc import chromium_policy, firefox_policy


@pytest.mark.parametrize("browser_type", ["cloakbrowser", "chromium", "firefox", "camoufox"])
def test_new_profile_defaults_and_mode_roundtrip(tmp_db, browser_type):
    expected = "enabled" if browser_type == "cloakbrowser" else "disabled"
    assert ProfileCreate(name="new", browser_type=browser_type).webrtc_mode == expected
    profile = db.create_profile("new", browser_type=browser_type)
    assert profile["webrtc_mode"] == expected
    for mode in ("protect_ip", "enabled", "disabled"):
        assert db.update_profile(profile["id"], webrtc_mode=mode)["webrtc_mode"] == mode
        assert db.list_profiles()[0]["webrtc_mode"] == mode


@pytest.mark.parametrize("mode", ["disabled", "protect_ip", "enabled"])
def test_api_mode_contract(app_client, mode):
    response = app_client.post("/api/profiles", json={"name": "new", "browser_type": "chromium", "webrtc_mode": mode})
    assert response.status_code == 201
    profile = response.json()
    assert profile["webrtc_mode"] == mode
    assert app_client.put('/api/profiles/'+profile['id'], json={"notes": "preserve mode"}).json()["webrtc_mode"] == mode
    assert app_client.put('/api/profiles/'+profile['id'], json={"webrtc_mode": "enabled"}).json()["webrtc_mode"] == "enabled"
    for invalid in (None, "unknown"):
        assert app_client.put('/api/profiles/'+profile['id'], json={"webrtc_mode": invalid}).status_code == 422
        assert app_client.post("/api/profiles", json={"name": "invalid", "webrtc_mode": invalid}).status_code == 422


def test_upgrade_preserves_all_existing_profiles(tmp_db):
    originals = [db.create_profile(t, browser_type=t, launch_args=["--existing"], notes="keep") for t in ("cloakbrowser", "chromium", "firefox", "camoufox")]
    with db.get_db() as c:
        c.execute("ALTER TABLE profiles DROP COLUMN webrtc_mode")
        c.commit()
    for _ in range(3):
        db.init_db()
    for original in originals:
        upgraded = db.get_profile(original["id"])
        assert upgraded["webrtc_mode"] == "enabled"
        assert {k:v for k,v in upgraded.items() if k != "webrtc_mode"} == {k:v for k,v in original.items() if k != "webrtc_mode"}


@pytest.mark.parametrize("browser_type", ["firefox", "camoufox"])
def test_firefox_modes_restore_original_and_allow_advanced_override(tmp_path, browser_type):
    (tmp_path / "prefs.js").write_text('user_pref("media.peerconnection.ice.no_host", true);\nuser_pref("unrelated", 7);\n')
    profile = {"browser_type": browser_type, "user_data_dir": str(tmp_path), "webrtc_mode": "disabled"}
    assert firefox_policy(profile)["media.peerconnection.enabled"] is False
    for mode in ("protect_ip", "disabled", "enabled", "protect_ip", "enabled"):
        profile["webrtc_mode"] = mode
        prefs = engines.firefox_preferences(profile, None)
        if mode == "protect_ip":
            assert prefs["media.peerconnection.enabled"] is True
            assert prefs["media.peerconnection.ice.proxy_only"] is True
        elif mode == "disabled":
            assert prefs["media.peerconnection.enabled"] is False
        else:
            assert prefs.get("media.peerconnection.enabled", True) is True
            assert prefs.get("media.peerconnection.ice.proxy_only", False) is False
            assert prefs["media.peerconnection.ice.no_host"] is True
        if browser_type == "firefox":
            engines._write_firefox_preferences(tmp_path, prefs)
    profile.update(webrtc_mode="disabled", firefox_preferences={"media.peerconnection.enabled": True})
    assert engines.firefox_preferences(profile, None)["media.peerconnection.enabled"] is True
    assert 'user_pref("unrelated", 7);' in (tmp_path / "prefs.js").read_text()


def test_enabled_legacy_firefox_is_untouched(tmp_path):
    assert firefox_policy({"user_data_dir": str(tmp_path), "browser_type": "firefox"}) == {}
    assert not (tmp_path / "webrtc-state.json").exists()


@pytest.mark.parametrize("original", [None, "default_public_interface_only"])
def test_chromium_policy_restores_preference_and_extension_becomes_noop(tmp_path, original):
    profile = {"user_data_dir": str(tmp_path), "webrtc_mode": "enabled"}
    data = {"other": "keep", "webrtc": {"unrelated": 123}}
    if original is not None:
        data["webrtc"]["ip_handling_policy"] = original
    expected = json.loads(json.dumps(data))
    assert chromium_policy(profile, data) is None
    for mode in ("disabled", "protect_ip", "enabled", "disabled", "enabled"):
        profile["webrtc_mode"] = mode
        extension = chromium_policy(profile, data)
        script = (extension / "block.js").read_text()
        assert f"if (!{str(mode == 'disabled').lower()}) return;" in script
        if mode == "enabled":
            assert data == expected
        else:
            assert data["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
        assert data["webrtc"]["unrelated"] == 123


def test_chromium_keeps_extensions_and_custom_arguments(tmp_path):
    profile = {"user_data_dir": str(tmp_path), "runtime_dir": str(tmp_path / "runtime"), "browser_type": "chromium", "webrtc_mode": "disabled", "launch_args": ["--load-extension=/custom", "--disable-extensions"]}
    command = engines.native_command(profile, None, True)
    extension_arg = next(a for a in command if a.startswith("--load-extension="))
    assert "webrtc-control" in extension_arg and "/custom" in extension_arg
    assert str(engines.CHROMIUM_EXTENSION) in extension_arg
    assert command[-1] == "--disable-extensions"
    assert not any("debugging" in arg or "automation" in arg for arg in command)


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_type,mode,args,guarded", [
    ("chromium", "disabled", [], True),
    ("chromium", "protect_ip", [], False),
    ("chromium", "enabled", [], False),
    ("chromium", "disabled", ["--disable-extensions"], False),
    ("firefox", "disabled", [], False),
])
async def test_native_extension_readiness_is_private_and_optional(tmp_path, monkeypatch, browser_type, mode, args, guarded):
    sentinel = object()
    async def spawn(profile, proxy, env, keepassxc, ready):
        assert (ready is not None) == guarded
        if ready is not None:
            url = urlsplit(profile["webrtc_ready_url"])
            assert url.hostname == "127.0.0.1"
            for path,accepted in (("/incorrect-token", False),(url.path, True)):
                reader,writer = await asyncio.open_connection(url.hostname,url.port)
                writer.write(f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                await reader.read()
                writer.close()
                await writer.wait_closed()
                assert ready.is_set() == accepted
        return sentinel
    monkeypatch.setattr(engines, "_spawn_native", spawn)
    profile = {"browser_type":browser_type,"webrtc_mode":mode,"user_data_dir":str(tmp_path),"launch_args":args}
    assert await engines.launch_native(profile,None,{},False) is sentinel
