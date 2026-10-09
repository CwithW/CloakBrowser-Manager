"""Run inside an isolated test container: seed on the old image, verify on new.

Usage: docker exec <test-container> python /tmp/e2e_browser_types.py seed|verify
The containers must share a disposable /data volume and the KeePassXC password.
Native browsers report page state to an HTTP fixture; they are never automated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.async_api import async_playwright

sys.path.insert(0, "/app")

BASE = "http://127.0.0.1:8080"
FIXTURE = "http://127.0.0.1:18090"
EXTENSION = "/opt/cloakbrowser/extensions/keepassxc-browser"
SNAPSHOT = Path("/data/e2e-legacy.json")
reports: dict[str, dict] = {}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        key = query.get("key", ["legacy"])[0]
        value = query.get("value", [key])[0]
        page = """<!doctype html><title>Browser type E2E</title><script>
        const key = KEY;
        const value = VALUE;
        const state = {
          webdriver: navigator.webdriver, language: navigator.language,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          width: screen.width, height: screen.height,
          cookie: document.cookie, storage: localStorage.getItem('e2e'),
          userAgent: navigator.userAgent,
        };
        document.cookie = 'e2e=' + value + '; Max-Age=86400; Path=/';
        localStorage.setItem('e2e', value);
        fetch('/report?key=' + key, {method:'POST', body:JSON.stringify(state)});
        </script><body>Browser type E2E</body>""".replace("KEY", json.dumps(key)).replace("VALUE", json.dumps(value))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(page.encode())

    def do_POST(self):
        key = parse_qs(urlsplit(self.path).query)["key"][0]
        reports[key] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(204)
        self.end_headers()


def api(method: str, path: str, data=None):
    response = httpx.request(method, BASE + "/api" + path, json=data, timeout=90, trust_env=False)
    response.raise_for_status()
    return response.json()


async def report(key: str):
    for _ in range(100):
        if key in reports:
            return reports.pop(key)
        await asyncio.sleep(0.2)
    raise AssertionError(f"No page report received for {key}")


async def cloak_page(profile, *, key: str, verify_storage=False):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(BASE + f"/api/profiles/{profile['id']}/cdp")
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto(FIXTURE + "/?key=" + key)
        state = await report(key)
        if verify_storage:
            assert state["storage"] == key, state
            assert f"e2e={key}" in state["cookie"], state
            await page.bring_to_front()
            display = api("GET", f"/profiles/{profile['id']}/status")["display"]
            api("POST", f"/profiles/{profile['id']}/clipboard", {"text": "incoming selection"})
            import subprocess
            subprocess.run(["xdotool", "mousemove", "300", "300", "click", "1", "key", "--clearmodifiers", "ctrl+a", "ctrl+c"], env={"DISPLAY": display}, check=True)
            await asyncio.sleep(0.2)
            assert api("GET", f"/profiles/{profile['id']}/clipboard")["text"].strip() == "Browser type E2E"
        await page.close()


async def seed():
    profile = api("POST", "/profiles", {
        "name": "Legacy upgrade E2E", "fingerprint_seed": 54321,
        "screen_width": 1280, "screen_height": 720, "notes": "legacy notes",
        "launch_args": [],
        "tags": [{"tag": "legacy", "color": "#123456"}],
    })
    api("POST", f"/profiles/{profile['id']}/launch")
    await cloak_page(profile, key="legacy")
    api("POST", f"/profiles/{profile['id']}/stop")
    database = Path(profile["user_data_dir"]) / "KeePassXC/passwords.kdbx"
    # Older images may have the historical system-Qt KeePassXC GUI failure;
    # create the real database via its CLI so upgrade data checks remain valid.
    database.parent.mkdir(exist_ok=True)
    from backend.browser_manager import BrowserManager
    await BrowserManager()._create_keepassxc_database(database, "e2e-disposable-password", {})
    assert database.is_file()
    SNAPSHOT.write_text(json.dumps({"profile": profile, "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest()}))
    print("PASS: old image created a real profile, cookie, localStorage and KeePassXC database", flush=True)


async def verify():
    snapshot = json.loads(SNAPSHOT.read_text())
    old = snapshot["profile"]
    profile = api("GET", f"/profiles/{old['id']}")
    assert profile["browser_type"] == "cloakbrowser"
    for key in ("name", "fingerprint_seed", "notes", "launch_args", "tags", "user_data_dir", "created_at"):
        assert profile[key] == old[key], (key, profile[key], old[key])
    database = Path(profile["user_data_dir"]) / "KeePassXC/passwords.kdbx"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == snapshot["database_sha256"]
    api("POST", f"/profiles/{profile['id']}/launch")
    await cloak_page(profile, key="legacy", verify_storage=True)
    api("POST", f"/profiles/{profile['id']}/stop")
    print("PASS: upgrade retained legacy config, cookies, localStorage and KeePassXC database", flush=True)

    for browser_type in ("chromium", "firefox", "camoufox"):
        profile = api("POST", "/profiles", {
            "name": f"{browser_type} E2E", "browser_type": browser_type,
            "locale": "en-GB", "timezone": "Europe/London", "screen_width": 1280,
            "screen_height": 720, "notes": "shared config", "platform": "linux",
            "launch_args": [FIXTURE + "/?key=" + browser_type] if browser_type != "camoufox" else [],
        })
        assert profile["keepassxc_enabled"] is True
        files_url = BASE + f"/api/profiles/{profile['id']}/files"
        payload = ("浏览器 Home\x00".encode() + bytes(range(256))) * 16
        upload = httpx.put(files_url + "/upload?path=Downloads/e2e.bin", content=payload, trust_env=False)
        assert upload.status_code == 200, upload.text
        assert httpx.get(files_url + "/download?path=Downloads/e2e.bin", trust_env=False).content == payload
        assert httpx.put(files_url + "/upload?path=Downloads/e2e.bin", content=b"overwrite", trust_env=False).status_code == 409
        assert httpx.get(files_url, params={"path": "../"}, trust_env=False).status_code == 400
        response = httpx.put(BASE + f"/api/profiles/{profile['id']}", json={"browser_type": "cloakbrowser"}, trust_env=False)
        assert response.status_code == 422
        for attempt in range(2):
            key = f"{browser_type}-{profile['id']}-{attempt}"
            url = FIXTURE + f"/?key={key}&value={browser_type}"
            if browser_type != "camoufox":
                api("PUT", f"/profiles/{profile['id']}", {"launch_args": [url]})
            launch = api("POST", f"/profiles/{profile['id']}/launch")
            assert launch["cdp_url"] is None
            assert api("GET", f"/profiles/{profile['id']}/status")["cdp_url"] is None
            assert httpx.get(BASE + f"/api/profiles/{profile['id']}/cdp", trust_env=False).status_code == 400
            if browser_type == "camoufox":
                # Drive VNC with X11 keyboard events; do not attach another driver
                # to the manager's already running persistent Camoufox context.
                import subprocess
                display = launch["display"]
                subprocess.run(["xdotool", "key", "ctrl+l"], env={"DISPLAY": display}, check=True)
                subprocess.run(["xdotool", "type", "--clearmodifiers", url], env={"DISPLAY": display}, check=True)
                subprocess.run(["xdotool", "key", "Return"], env={"DISPLAY": display}, check=True)
            state = await report(key)
            assert state["webdriver"] is False, state
            assert state["language"] == "en-GB", state
            assert state["timezone"] == "Europe/London", state
            assert (state["width"], state["height"]) == (1280, 720), state
            if attempt:
                assert state["storage"] == browser_type, state
                assert f"e2e={browser_type}" in state["cookie"], state
            paths = Path(profile["user_data_dir"])
            assert (paths / "KeePassXC/passwords.kdbx").is_file()
            # Copy actual page text with ordinary X11 input, then read it back
            # after replacing an earlier manager-owned selection.
            import subprocess
            display = launch["display"]
            api("POST", f"/profiles/{profile['id']}/clipboard", {"text": "incoming selection"})
            subprocess.run(["xdotool", "mousemove", "300", "300", "click", "1", "key", "--clearmodifiers", "ctrl+a", "ctrl+c"], env={"DISPLAY": display}, check=True)
            await asyncio.sleep(0.2)
            assert api("GET", f"/profiles/{profile['id']}/clipboard")["text"].strip() == "Browser type E2E"
            # Replace the X11 selection repeatedly and simulate a crashed owner.
            for index in range(12):
                text = f"{browser_type} clipboard {index} 剪贴板"
                api("POST", f"/profiles/{profile['id']}/clipboard", {"text": text})
                assert api("GET", f"/profiles/{profile['id']}/clipboard")["text"] == text
            import os
            import signal
            owners = []
            for process in Path("/proc").iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    command = (process / "cmdline").read_bytes().split(b"\0")
                except OSError:
                    continue
                if command and command[0] == b"xclip" and b"-quiet" in command:
                    owners.append(int(process.name))
            assert len(owners) == 1, owners
            os.kill(owners[0], signal.SIGKILL)
            api("POST", f"/profiles/{profile['id']}/clipboard", {"text": "recovered after owner crash"})
            assert api("GET", f"/profiles/{profile['id']}/clipboard")["text"] == "recovered after owner crash"
            if browser_type in {"firefox", "camoufox"}:
                # Firefox records actual installed extension state separately
                # from the manager's copied .xpi files.
                for _ in range(50):
                    extensions = paths / "extensions.json"
                    if extensions.exists() and any(a["id"] == "keepassxc-browser@keepassxc.org" and a.get("active") for a in json.loads(extensions.read_text())["addons"]):
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise AssertionError(f"{browser_type} KeePassXC extension is not active")
                if browser_type == "firefox":
                    assert '"browser.contentblocking.category", "strict"' in (paths / "user.js").read_text()
                    # Check Firefox's live policy using ordinary input.
                    subprocess.run(["xdotool", "key", "ctrl+l"], env={"DISPLAY": display}, check=True)
                    subprocess.run(["xdotool", "type", "--clearmodifiers", "about:policies"], env={"DISPLAY": display}, check=True)
                    subprocess.run(["xdotool", "key", "Return"], env={"DISPLAY": display}, check=True)
                    for _ in range(30):
                        await asyncio.sleep(0.2)
                        subprocess.run(["xdotool", "mousemove", "300", "300", "click", "1", "key", "--clearmodifiers", "ctrl+a", "ctrl+c"], env={"DISPLAY": display}, check=True)
                        if "DisableAppUpdate" in api("GET", f"/profiles/{profile['id']}/clipboard")["text"]:
                            break
                    else:
                        raise AssertionError("Firefox did not activate its DisableAppUpdate policy")
            window = api("POST", f"/profiles/{profile['id']}/keepassxc/toggle-window")
            assert window["state"] in {"shown", "minimized"}
            api("POST", f"/profiles/{profile['id']}/stop")
            if browser_type == "camoufox":
                # Camoufox does not expose about:policies. Check the policy
                # service's real applied state and the active binary's policy.
                from camoufox.pkgman import camoufox_path
                assert '"browser.policies.applied", true' in (paths / "prefs.js").read_text()
                policy = camoufox_path(download_if_missing=False) / "distribution/policies.json"
                assert json.loads(policy.read_text())["policies"]["DisableAppUpdate"] is True
            if browser_type == "chromium":
                extensions = json.loads((paths / "Default/Preferences").read_text())["extensions"]["settings"].values()
                assert any(addon.get("path") == EXTENSION and not addon.get("disable_reasons") for addon in extensions)
        print(f"PASS: {browser_type} native signals, shared settings, extension, KeePassXC UI and persistence", flush=True)

    await verify_ui()


async def verify_ui():
    # Exercise the actual React create/edit flows with the allowed Camoufox
    # test browser. Target native profiles remain subprocess-only throughout.
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        await page.goto(BASE)
        for browser_type, label in (("chromium", "Chromium"), ("firefox", "Firefox"), ("camoufox", "Camoufox")):
            await page.get_by_role("button", name="New Profile").click()
            await page.get_by_label("Browser Type").select_option(browser_type)
            await page.get_by_placeholder("e.g. Amazon Seller #1").fill(label + " UI E2E")
            await page.get_by_role("button", name="Create", exact=True).click()
            await page.get_by_role("heading", name="Edit Profile", exact=True).wait_for()
            assert await page.get_by_label("Browser Type").is_disabled()
            await page.get_by_role("button", name="Files", exact=True).click()
            await page.get_by_label("Upload files to browser Home").set_input_files({"name": "ui-upload.txt", "mimeType": "text/plain", "buffer": b"UI upload"})
            await page.get_by_label("Download ui-upload.txt").wait_for()
            async with page.expect_download() as download_info:
                await page.get_by_label("Download ui-upload.txt").click()
            download = await download_info.value
            assert Path(await download.path()).read_bytes() == b"UI upload"
            await page.get_by_label("Close file manager").click()
            if browser_type == "chromium":
                await page.get_by_role("button", name="Launch", exact=True).click()
                await page.get_by_text("Connected", exact=True).wait_for()
                await page.get_by_label("Browser files", exact=True).click()
                await page.get_by_label("Close file manager").click()
                await page.get_by_label("Back to profile settings").click()
                await page.get_by_role("heading", name="Edit Profile", exact=True).wait_for()
                assert await page.get_by_role("button", name="Stop", exact=True).is_visible()
                await page.get_by_role("button", name="Stop", exact=True).click()
                await page.get_by_role("button", name="Launch", exact=True).wait_for()
            await page.get_by_role("button", name="Cancel", exact=True).click()
        print("PASS: React create/edit types, VNC back button and file upload/download flows", flush=True)


async def main():
    for _ in range(100):
        try:
            api("GET", "/status")
            break
        except httpx.HTTPError:
            await asyncio.sleep(0.2)
    else:
        raise AssertionError("Manager API did not become ready")
    server = ThreadingHTTPServer(("127.0.0.1", 18090), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        await (seed() if sys.argv[1] == "seed" else verify())
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
