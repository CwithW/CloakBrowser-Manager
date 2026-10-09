"""Real protocol E2E inside a disposable Docker instance with AUTH_TOKEN set."""

import asyncio
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import websockets
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8080"
HEADERS = {"Authorization": "Bearer " + os.environ["AUTH_TOKEN"]}
WS = "ws://127.0.0.1:8080"
FIXTURE = "http://127.0.0.1:18194"


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>Protocol E2E</title>Protocol E2E")


def api(method, path, data=None):
    response = httpx.request(method, BASE + "/api" + path, json=data, headers=HEADERS, timeout=120, trust_env=False)
    response.raise_for_status()
    return response.json()


async def bidi(endpoint):
    async with websockets.connect(endpoint, additional_headers=HEADERS) as ws:
        for identifier, method, params in (
            (1, "session.new", {"capabilities": {}}),
            (2, "browsingContext.getTree", {}),
        ):
            await ws.send(json.dumps({"id": identifier, "method": method, "params": params}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if reply.get("id") == identifier:
                    break
            assert reply["type"] == "success", reply
        context = reply["result"]["contexts"][0]["context"]
        await ws.send(json.dumps({"id": 5, "method": "browsingContext.navigate", "params": {"context": context, "url": FIXTURE, "wait": "complete"}}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if reply.get("id") == 5:
                break
        assert reply["type"] == "success", reply
        await ws.send(json.dumps({"id": 3, "method": "script.evaluate", "params": {"expression": "40 + 2", "target": {"context": context}, "awaitPromise": False}}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if reply.get("id") == 3:
                break
        assert reply["type"] == "success", reply
        assert reply["result"]["result"]["value"] == 42, reply
        await ws.send(json.dumps({"id": 4, "method": "session.end", "params": {}}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if reply.get("id") == 4:
                break
        assert reply["type"] == "success", reply


async def reject_auth(endpoint):
    try:
        async with websockets.connect(endpoint):
            raise AssertionError("Unauthenticated protocol connection succeeded")
    except websockets.exceptions.InvalidStatus as error:
        assert error.response.status_code == 403


async def verify_ui():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=True) as browser:
        context = await browser.new_context()
        response = await context.request.post(BASE + "/api/auth/login", data={"token": os.environ["AUTH_TOKEN"]})
        assert response.ok
        page = await context.new_page()
        await page.goto(BASE)
        for kind in ("cloakbrowser", "chromium", "firefox", "camoufox"):
            await page.get_by_role("button", name="New Profile").click()
            await page.get_by_label("Browser Type").select_option(kind)
            protocol = "cdp" if kind in {"cloakbrowser", "chromium"} else "bidi"
            endpoint_label = protocol.upper() + " endpoint"
            if kind != "cloakbrowser":
                assert not await page.get_by_label(endpoint_label).count()
                await page.get_by_label("Expose CDP" if kind == "chromium" else "Expose WebDriver BiDi").check()
            assert await page.get_by_label(endpoint_label).input_value() == WS + "/api/profiles/<profile-id>/" + protocol
            if kind == "camoufox":
                await page.get_by_label("Expose Camoufox Playwright").check()
                assert not await page.get_by_label("Expose WebDriver BiDi").is_checked()
                assert not await page.get_by_label(endpoint_label).count()
                endpoint_label = "Playwright endpoint"
            name = kind + " remote UI"
            await page.get_by_placeholder("e.g. Amazon Seller #1").fill(name)
            await page.get_by_role("button", name="Create", exact=True).click()
            await page.get_by_role("heading", name="Edit Profile", exact=True).wait_for()
            profile = next(p for p in api("GET", "/profiles") if p["name"] == name)
            suffix = "playwright" if kind == "camoufox" else protocol
            assert await page.get_by_label(endpoint_label).input_value() == WS + f"/api/profiles/{profile['id']}/{suffix}"
            assert await page.locator("code").filter(has_text="Authorization").count() >= 1
            if kind == "camoufox":
                await page.get_by_label("Expose WebDriver BiDi").check()
                assert not await page.get_by_label("Expose Camoufox Playwright").is_checked()
                await page.get_by_role("button", name="Save", exact=True).click()
                await page.get_by_role("button", name="Save", exact=True).wait_for()
                current = api("GET", f"/profiles/{profile['id']}")
                assert current["remote_debugging_enabled"] and not current["playwright_enabled"]
            await page.get_by_role("button", name="Cancel", exact=True).click()
        print("PASS: real React protocol defaults/endpoints/examples/authenticated save/Camoufox mutual exclusion", flush=True)


async def main():
    for _ in range(60):
        try:
            api("GET", "/status")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.25)
    else:
        raise RuntimeError("Test container API did not become ready")
    server = ThreadingHTTPServer(("127.0.0.1", 18194), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as playwright:
            for kind in os.environ.get("E2E_BROWSER_TYPES", "cloakbrowser,chromium,firefox,camoufox").split(","):
                profile = api("POST", "/profiles", {"name": kind + " protocols", "browser_type": kind, "keepassxc_enabled": False, "platform": "linux", "screen_width": 1280, "screen_height": 720})
                pid = profile["id"]
                assert not profile["remote_debugging_enabled"] and not profile["playwright_enabled"]
                launch = api("POST", f"/profiles/{pid}/launch")
                assert bool(launch["cdp_url"]) == (kind == "cloakbrowser")
                assert launch["bidi_url"] is None and launch["playwright_url"] is None
                api("POST", f"/profiles/{pid}/stop")
                cases = [(True, False)] if kind != "camoufox" else [(False, True), (True, False), (False, True)]
                fingerprint = None
                fingerprint_data = None
                for debug, remote_playwright in cases:
                    api("PUT", f"/profiles/{pid}", {"remote_debugging_enabled": debug, "playwright_enabled": remote_playwright})
                    launch = api("POST", f"/profiles/{pid}/launch")
                    try:
                        protocol = "cdp" if kind in {"cloakbrowser", "chromium"} else "bidi"
                        if debug:
                            endpoint = WS + f"/api/profiles/{pid}/{protocol}"
                            await reject_auth(endpoint)
                            if protocol == "cdp":
                                browser = await playwright.chromium.connect_over_cdp(endpoint, headers=HEADERS)
                                page = await browser.contexts[0].new_page()
                                assert await page.evaluate("40 + 2") == 42
                                await page.close()
                                await browser.close()
                            else:
                                await bidi(endpoint)
                        if remote_playwright:
                            endpoint = WS + f"/api/profiles/{pid}/playwright"
                            await reject_auth(endpoint)
                            browser = await playwright.firefox.connect(endpoint, headers=HEADERS)
                            assert len(browser.contexts) == 1
                            context = browser.contexts[0]
                            page = context.pages[0]
                            await page.goto(FIXTURE)
                            if fingerprint:
                                assert await page.evaluate("localStorage.getItem('protocol-e2e')") == "retained"
                                assert "protocol-e2e=retained" in await page.evaluate("document.cookie")
                            await page.evaluate("localStorage.setItem('protocol-e2e', 'retained'); document.cookie = 'protocol-e2e=retained; Max-Age=86400; Path=/'")
                            await browser.close()
                            assert api("GET", f"/profiles/{pid}")["status"] == "running"
                            browser = await playwright.firefox.connect(endpoint, headers=HEADERS)
                            assert await browser.contexts[0].pages[0].evaluate("localStorage.getItem('protocol-e2e')") == "retained"
                            await browser.close()
                        assert api("GET", f"/profiles/{pid}")["status"] == "running"
                    finally:
                        api("POST", f"/profiles/{pid}/stop")
                    if kind == "camoufox":
                        saved = (Path(profile["user_data_dir"]) / "camoufox-fingerprint.json").read_bytes()
                        current = hashlib.sha256(saved).hexdigest()
                        if fingerprint:
                            current_data = json.loads(saved)
                            changed = [(key, fingerprint_data["config"].get(key), current_data["config"].get(key)) for key in fingerprint_data["config"].keys() | current_data["config"].keys() if fingerprint_data["config"].get(key) != current_data["config"].get(key)]
                            assert current == fingerprint, changed
                        fingerprint = current
                        fingerprint_data = json.loads(saved)
                    print(f"PASS: {kind} debugging={debug} playwright={remote_playwright} real protocol/auth/disconnect/persistence", flush=True)
                api("PUT", f"/profiles/{pid}", {"remote_debugging_enabled": False, "playwright_enabled": False})
                launch = api("POST", f"/profiles/{pid}/launch")
                assert bool(launch["cdp_url"]) == (kind == "cloakbrowser")
                assert launch["bidi_url"] is None and launch["playwright_url"] is None
                api("POST", f"/profiles/{pid}/stop")
                print(f"PASS: {kind} controls disabled again", flush=True)
        if "E2E_BROWSER_TYPES" not in os.environ:
            await verify_ui()
    finally:
        for profile in api("GET", "/profiles"):
            if profile["status"] == "running":
                api("POST", f"/profiles/{profile['id']}/stop")
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
