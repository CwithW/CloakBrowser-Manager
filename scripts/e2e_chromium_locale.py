"""Seed with the previous image, then verify Chromium locale after upgrading.

Run seed/verify in isolated containers sharing a disposable /data volume.
The ordinary webpage reports its own state; no debugging driver is attached.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

BASE = "http://127.0.0.1:8080/api"
FIXTURE = "http://127.0.0.1:18095"
SNAPSHOT = Path("/data/e2e-chromium-locale.json")
reports: dict[str, dict] = {}
headers: dict[str, str] = {}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        key = parse_qs(urlsplit(self.path).query).get("key", [""])[0]
        headers[key] = self.headers.get("Accept-Language", "")
        page = """<!doctype html><title>Chromium locale E2E</title><script>
        function state() {return {
          language: navigator.language, languages: navigator.languages,
          date: Intl.DateTimeFormat().resolvedOptions().locale,
          number: Intl.NumberFormat().resolvedOptions().locale,
          collator: Intl.Collator().resolvedOptions().locale,
          webdriver: navigator.webdriver
        };}
        const result = state();
        result.cookie = document.cookie;
        result.storage = localStorage.getItem('locale-e2e');
        document.cookie = 'locale-e2e=retained; Max-Age=86400; Path=/';
        localStorage.setItem('locale-e2e', 'retained');
        const worker = new Worker(URL.createObjectURL(new Blob([
          state.toString() + ';postMessage(state());'
        ], {type: 'text/javascript'})));
        worker.onmessage = event => {
          result.worker = event.data;
          fetch('/report?key=' + KEY, {method: 'POST', body: JSON.stringify(result)});
        };
        </script>""".replace("KEY", json.dumps(key))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())

    def do_POST(self):
        key = parse_qs(urlsplit(self.path).query)["key"][0]
        reports[key] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(204)
        self.end_headers()


def api(method, path, data=None):
    response = httpx.request(method, BASE + path, json=data, timeout=90, trust_env=False)
    response.raise_for_status()
    return response.json()


def report(key):
    for _ in range(150):
        if key in reports:
            return reports.pop(key)
        time.sleep(0.2)
    raise AssertionError(f"No webpage report for {key}")


def navigate(display, url):
    env = {**os.environ, "DISPLAY": display}
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+l"], env=env, check=True)
    time.sleep(0.2)
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "25", url], env=env, check=True)
    subprocess.run(["xdotool", "key", "Return"], env=env, check=True)


def check_state(key, language, *, retained=True):
    state = report(key)
    for context in (state, state["worker"]):
        assert context["language"] == language, state
        assert context["languages"][0] == language, state
        assert all(context[item] == "en-US" for item in ("date", "number", "collator")), state
    assert state["webdriver"] is False, state
    assert headers[key].split(",")[0].split(";")[0] == language, headers[key]
    if retained:
        assert "locale-e2e=retained" in state["cookie"], state
        assert state["storage"] == "retained", state
    return state


def launch(profile, key):
    api("PUT", f"/profiles/{profile['id']}", {"launch_args": [FIXTURE + "/?key=" + key]})
    status = api("POST", f"/profiles/{profile['id']}/launch")
    assert status["cdp_url"] is None, status
    commands = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            commands.append((proc / "cmdline").read_bytes().split(b"\0"))
        except OSError:
            continue
    command = next(command for command in commands
                   if f"--user-data-dir={profile['user_data_dir']}".encode() in command)
    assert not any(arg.startswith((b"--remote-debugging", b"--enable-automation")) for arg in command), command
    return status


def seed():
    profile = api("POST", "/profiles", {
        "name": "Chromium locale upgrade E2E", "browser_type": "chromium",
        "locale": "en-US", "keepassxc_enabled": False, "notes": "retain this profile",
    })
    try:
        launch(profile, "seed")
        check_state("seed", "en-US", retained=False)
    finally:
        api("POST", f"/profiles/{profile['id']}/stop")
    prefs = json.loads((Path(profile["user_data_dir"]) / "Default/Preferences").read_text())
    assert prefs["intl"]["selected_languages"].startswith("en-US"), prefs["intl"]
    SNAPSHOT.write_text(json.dumps(profile))
    print("PASS: seeded existing Chromium English preferences, cookies and storage", flush=True)


def verify():
    profile = json.loads(SNAPSHOT.read_text())
    for index, locale in enumerate(("ja", "ja", "de-DE", "zh-TW", None)):
        key = f"locale-{index}"
        api("PUT", f"/profiles/{profile['id']}", {"locale": locale})
        try:
            status = launch(profile, key)
            state = check_state(key, locale or "en-US")
            if index == 0:
                navigate(status["display"], "chrome://settings/")
                for _ in range(50):
                    title = subprocess.check_output(
                        ["xdotool", "getwindowfocus", "getwindowname"],
                        env={**os.environ, "DISPLAY": status["display"]}, text=True,
                    )
                    if "Settings" in title:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError(f"Settings UI is not English: {title}")
            print(f"PASS: locale={locale!r}, webpage/Worker={state['language']}, "
                  f"Accept-Language={headers[key]}, Intl/UI=en-US, data retained", flush=True)
        finally:
            api("POST", f"/profiles/{profile['id']}/stop")
    assert api("GET", f"/profiles/{profile['id']}")["notes"] == profile["notes"]

    # Different profiles must not share language settings or process state.
    api("PUT", f"/profiles/{profile['id']}", {"locale": "ja"})
    other = api("POST", "/profiles", {
        "name": "English locale isolation E2E", "browser_type": "chromium",
        "locale": "ja", "keepassxc_enabled": False,
    })
    try:
        launch(other, "fresh-ja")
        check_state("fresh-ja", "ja", retained=False)
        print("PASS: newly created Japanese profile reports ja without debugging", flush=True)
    finally:
        api("POST", f"/profiles/{other['id']}/stop")
    api("PUT", f"/profiles/{other['id']}", {"locale": "en-US"})
    try:
        launch(profile, "isolation-ja")
        launch(other, "isolation-en")
        check_state("isolation-ja", "ja")
        check_state("isolation-en", "en-US", retained=False)
        print("PASS: simultaneous Japanese and English profiles remain isolated", flush=True)
    finally:
        for item in (profile, other):
            api("POST", f"/profiles/{item['id']}/stop")


if __name__ == "__main__":
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 18095), Fixture).serve_forever, daemon=True).start()
    {"seed": seed, "verify": verify}[sys.argv[1]]()
