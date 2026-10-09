"""Check image fonts and actual webpage detection in a disposable container.

Run with: docker exec <test-container> python /tmp/e2e_fonts.py
Native browsers use normal subprocess launches and X11 input, with no CDP/BiDi.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8080/api"
FIXTURE = "http://127.0.0.1:18195"
HEADERS = {"Authorization": "Bearer " + os.environ["AUTH_TOKEN"]} if os.environ.get("AUTH_TOKEN") else {}
FAMILIES = [
    "Arimo", "Tinos", "Cousine", "Roboto", "Noto Sans", "Noto Serif",
    "Noto Sans Mono", "Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans CJK JP",
    "Noto Sans CJK KR", "Noto Serif CJK SC", "Noto Color Emoji",
    "Noto Sans Symbols", "Noto Sans Symbols2", "Noto Sans Math",
    "Noto Naskh Arabic", "Noto Sans Thai",
]
CORE = FAMILIES[:6]
reports = {}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        page = """<!doctype html><title>Image fonts E2E</title><script>
        (async () => {
          await document.fonts.ready;
          const canvas = document.createElement('canvas');
          const ctx = canvas.getContext('2d');
          const text = 'abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789 mwilMWIL';
          function width(font) {
            ctx.font = '128px ' + font;
            return ctx.measureText(text).width;
          }
          function detected(font) {
            return ['monospace', 'sans-serif', 'serif'].some(base =>
              Math.abs(width(JSON.stringify(font) + ', ' + base) - width(base)) > 0.01);
          }
          const fonts = FAMILIES;
          const result = {fonts:Object.fromEntries(fonts.map(f => [f, detected(f)])),
            missing:detected('CBM nonexistent family 038ef712'), webdriver:navigator.webdriver};
          fetch('/report' + location.pathname, {method:'POST', body:JSON.stringify(result)});
        })();
        </script><body>Image fonts E2E: 中文 العربية ภาษาไทย ∑ 😀</body>""".replace("FAMILIES", json.dumps(FAMILIES))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())

    def do_POST(self):
        reports[self.path.removeprefix("/report/")] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(204)
        self.end_headers()


def api(method, path, data=None):
    response = httpx.request(method, BASE + path, json=data, headers=HEADERS, timeout=120, trust_env=False)
    response.raise_for_status()
    return response.json()


def main():
    installed = {family.strip() for line in subprocess.check_output(["fc-list", "--format", "%{family}\n"], text=True).splitlines() for family in line.split(",")}
    assert set(FAMILIES) <= installed, sorted(set(FAMILIES) - installed)
    assert {"Arial", "Times New Roman", "Verdana"} <= installed, "Existing Windows fonts are missing"
    print(f"PASS: {len(installed)} installed font families; all 18 requested families and existing Windows fonts present", flush=True)
    for _ in range(60):
        try:
            api("GET", "/status")
            break
        except httpx.ConnectError:
            time.sleep(0.25)
    else:
        raise RuntimeError("Test container API did not become ready")
    server = ThreadingHTTPServer(("127.0.0.1", 18195), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for kind in ("chromium", "firefox", "camoufox", "cloakbrowser"):
            profile = api("POST", "/profiles", {
                "name": kind + " real fonts", "browser_type": kind, "platform": "linux",
                "keepassxc_enabled": False, "screen_width": 1280, "screen_height": 720,
                **({"camoufox_config": {"fonts": FAMILIES}} if kind == "camoufox" else {}),
            })
            pid = profile["id"]
            assert not profile["remote_debugging_enabled"] and not profile["playwright_enabled"]
            try:
                for attempt in range(2 if kind == "camoufox" else 1):
                    key = kind + str(attempt)
                    launched = api("POST", f"/profiles/{pid}/launch")
                    assert not launched["bidi_url"] and not launched["playwright_url"]
                    assert bool(launched["cdp_url"]) == (kind == "cloakbrowser")
                    env = {**os.environ, "DISPLAY": launched["display"]}
                    time.sleep(1)
                    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+l"], env=env, check=True)
                    subprocess.run(["xdotool", "type", "--clearmodifiers", FIXTURE + "/" + key], env=env, check=True)
                    subprocess.run(["xdotool", "key", "Return"], env=env, check=True)
                    for _ in range(150):
                        if key in reports:
                            break
                        time.sleep(0.2)
                    assert key in reports, f"No font report from {kind}"
                    state = reports.pop(key)
                    assert state["missing"] is False, state
                    assert state["webdriver"] is False, state
                    assert all(state["fonts"][font] for font in CORE), state
                    if kind == "camoufox":
                        config = json.loads((Path(profile["user_data_dir"]) / "camoufox-fingerprint.json").read_text())
                        assert config["config"]["fonts"] == FAMILIES
                    print(f"PASS: {kind} launch {attempt + 1}: real Croscore/Roboto/Noto rendering, negative control, webdriver=false; {json.dumps(state['fonts'])}", flush=True)
                    api("POST", f"/profiles/{pid}/stop")
            finally:
                if api("GET", f"/profiles/{pid}")["status"] == "running":
                    api("POST", f"/profiles/{pid}/stop")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
