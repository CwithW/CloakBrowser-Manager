"""Verify Firefox/Camoufox English UI and independent website languages.

Run seed with the previous image and verify with the new image on the same
disposable /data volume. Copy e2e_chromium_locale.py alongside this script.
Page/Worker reports and ordinary X11 input require no debugging connection.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import e2e_chromium_locale as fixture

SNAPSHOT = Path("/data/e2e-firefox-locale.json")


def page(profile, key):
    status = fixture.api("POST", f"/profiles/{profile['id']}/launch")
    assert status["cdp_url"] is None
    assert bool(status["bidi_url"]) == bool(profile.get("remote_debugging_enabled"))
    assert bool(status["playwright_url"]) == bool(profile.get("playwright_enabled"))
    time.sleep(0.5)
    fixture.navigate(status["display"], fixture.FIXTURE + "/?key=" + key)
    return status, fixture.report(key)


def ui_title(display):
    fixture.navigate(display, "about:preferences")
    for _ in range(50):
        title = subprocess.check_output(
            ["xdotool", "getwindowfocus", "getwindowname"],
            env={**os.environ, "DISPLAY": display}, text=True,
        ).strip()
        if title.startswith(("Settings", "設定", "Einstellungen", "设置")):
            return title
        time.sleep(0.2)
    raise AssertionError("Settings did not load")


def check(profile, key, state, locale, *, retained=True):
    kind = profile["browser_type"]
    for context in (state, state["worker"]):
        assert context["language"] == (locale or "en-US"), state
        if locale:
            assert context["languages"] == [locale], state
        else:
            assert context["languages"][0] == "en-US", state
        # Camoufox's engine-level Intl fingerprint is independent of its UI.
        expected = {"ja": "ja-JP", "de-DE": "de-DE"}.get(locale, "en-US") if kind == "camoufox" else "en-US"
        assert all(context[item] == expected for item in ("date", "number", "collator")), state
    # Native Firefox exposes webdriver when the user opts into BiDi.
    assert state["webdriver"] is (kind == "firefox" and bool(profile.get("remote_debugging_enabled"))), state
    header = fixture.headers[key]
    assert header.split(",")[0].split(";")[0] == (locale or "en-US"), header
    if locale and not locale.startswith("en"):
        assert "en" not in [item.split(";")[0].strip().split("-")[0] for item in header.split(",")], header
    if retained:
        assert state["storage"] == "retained", state
        assert "locale-e2e=retained" in state["cookie"], state


def seed():
    profiles = []
    for kind in ("firefox", "camoufox"):
        profile = fixture.api("POST", "/profiles", {
            "name": kind + " locale upgrade", "browser_type": kind, "platform": "linux",
            "locale": "ja", "keepassxc_enabled": False, "notes": "retain browser identity and data",
            "remote_debugging_enabled": kind == "camoufox",
        })
        key = kind + "-seed"
        try:
            status, state = page(profile, key)
            assert state["language"] in ({"ja", "ja-JP"} if kind == "camoufox" else {"ja"}), state
            print(f"PASS: seeded {kind}: languages={state['languages']}, "
                  f"Accept-Language={fixture.headers[key]}, UI={ui_title(status['display'])}", flush=True)
        finally:
            fixture.api("POST", f"/profiles/{profile['id']}/stop")
        if kind == "camoufox":
            profile["saved_fingerprint"] = json.loads((Path(profile["user_data_dir"]) / "camoufox-fingerprint.json").read_text())
            # The old native/BiDi path writes Japanese user.js preferences.
            # Upgrade back to normal mode must replace them before startup.
            fixture.api("PUT", f"/profiles/{profile['id']}", {"remote_debugging_enabled": False})
            profile["remote_debugging_enabled"] = False
        profiles.append(profile)
    SNAPSHOT.write_text(json.dumps(profiles))


def verify():
    for profile in json.loads(SNAPSHOT.read_text()):
        kind = profile["browser_type"]
        profile.update(remote_debugging_enabled=False, playwright_enabled=False)
        fixture.api("PUT", f"/profiles/{profile['id']}", {
            "remote_debugging_enabled": False, "playwright_enabled": False,
        })
        for index, locale in enumerate(("ja", "ja", "de-DE", None)):
            profile["locale"] = locale
            fixture.api("PUT", f"/profiles/{profile['id']}", {"locale": locale})
            key = f"{kind}-locale-{index}"
            try:
                status, state = page(profile, key)
                check(profile, key, state, locale)
                title = ui_title(status["display"])
                assert title.startswith("Settings"), title
                print(f"PASS: {kind} locale={locale!r}: page/Worker={state['languages']}, "
                      f"Accept-Language={fixture.headers[key]}, Intl={state['date']}, English UI, data retained", flush=True)
            finally:
                fixture.api("POST", f"/profiles/{profile['id']}/stop")
            if kind == "camoufox" and index < 2:
                saved = json.loads((Path(profile["user_data_dir"]) / "camoufox-fingerprint.json").read_text())
                old = profile["saved_fingerprint"]
                assert saved["identity"] == old["identity"]
                assert {key: value for key, value in saved["config"].items() if key != "locale:all"} == old["config"], "Non-language fingerprint changed"
        current = fixture.api("GET", f"/profiles/{profile['id']}")
        for key in ("fingerprint_seed", "user_data_dir", "notes", "proxy"):
            assert current[key] == profile[key], key

        # Opt-in protocol modes must use the same persistent language settings.
        cases = [(True, False)] if kind == "firefox" else [(True, False), (False, True), (False, False)]
        for debug, playwright in cases:
            profile.update(locale="ja", remote_debugging_enabled=debug, playwright_enabled=playwright)
            fixture.api("PUT", f"/profiles/{profile['id']}", {
                "locale": "ja", "remote_debugging_enabled": debug, "playwright_enabled": playwright,
            })
            key = f"{kind}-{debug}-{playwright}"
            try:
                status, state = page(profile, key)
                check(profile, key, state, "ja")
                assert ui_title(status["display"]).startswith("Settings")
                print(f"PASS: {kind} debugging={debug} playwright={playwright}: ['ja'], "
                      "English UI, cookies/storage retained", flush=True)
            finally:
                fixture.api("POST", f"/profiles/{profile['id']}/stop")

        fresh = fixture.api("POST", "/profiles", {
            "name": kind + " fresh Japanese", "browser_type": kind, "platform": "linux",
            "locale": "ja", "keepassxc_enabled": False,
        })
        key = kind + "-fresh"
        try:
            status, state = page(fresh, key)
            check(fresh, key, state, "ja", retained=False)
            assert ui_title(status["display"]).startswith("Settings")
            print(f"PASS: newly created {kind}: ['ja'], English UI", flush=True)
        finally:
            fixture.api("POST", f"/profiles/{fresh['id']}/stop")


if __name__ == "__main__":
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 18095), fixture.Fixture).serve_forever, daemon=True).start()
    {"seed": seed, "verify": verify}[sys.argv[1]]()
