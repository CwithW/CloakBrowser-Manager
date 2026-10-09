<p align="center">
<img src="https://i.imgur.com/cqkp6fG.png" width="500" alt="CloakBrowser">
</p>

<h3 align="center">Browser Profile Manager for CloakBrowser</h3>

<p align="center">
Create, manage, and launch isolated browser profiles with unique fingerprints.<br>
Free, self-hosted alternative to Multilogin, GoLogin, and AdsPower.
</p>

<p align="center">
<a href="https://github.com/CloakHQ/CloakBrowser"><img src="https://img.shields.io/github/stars/cloakhq/cloakbrowser?label=CloakBrowser" alt="Stars"></a>
<a href="https://hub.docker.com/r/cloakhq/cloakbrowser-manager"><img src="https://img.shields.io/docker/pulls/cloakhq/cloakbrowser-manager?label=docker&logo=docker&logoColor=white" alt="Docker Pulls"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

<p align="center">
<img src="https://i.imgur.com/twdX81Q.png" width="800" alt="CloakBrowser Manager — Browser View">
<br>
<img src="https://i.imgur.com/XFYn1qY.png" width="800" alt="CloakBrowser Manager — Profile Settings">
</p>

Each profile is an isolated CloakBrowser, native Chromium, native Firefox ESR, or Camoufox instance with its own proxy, cookies, and session data. Profiles persist across restarts. All four browsers are bundled in one Docker image.

## Browser types

Select the browser type when creating a profile. It cannot be changed after creation. Upgrading an existing installation adds the type `cloakbrowser` to old database rows without moving or replacing their browser directories, cookies, bookmarks, extension state, or KeePassXC databases. Keep the existing `/data` volume and KeePassXC password when upgrading.

| Type | Launch method | Fingerprint settings | Defaults |
| --- | --- | --- | --- |
| CloakBrowser | Existing CloakBrowser API | Seed, platform, GPU, CPU, screen | Existing profile settings are preserved |
| Chromium | Direct subprocess; no automation flags or CDP | Native Linux browser identity | KeePassXC-Browser enabled |
| Firefox | Direct subprocess; no WebDriver, Marionette, or CDP | Native Linux browser identity | Strict tracking protection and KeePassXC-Browser enabled |
| Camoufox | Official Camoufox/Playwright API | Platform, screen, CPU, supported WebGL pair, advanced config | Persistent generated fingerprint and KeePassXC-Browser enabled |

All types share `proxy`, `timezone`, `locale`, `screen_width`, `screen_height`, `launch_args`, `notes`, `user_agent`, `color_scheme`, tags, clipboard synchronization, and automatic launch. Native browsers apply timezone through `TZ`, language through native preferences/arguments, and use the X11 clipboard. CDP is always available for CloakBrowser and opt-in for native Chromium; Firefox and Camoufox can opt into WebDriver BiDi.

New Chromium/Firefox/Camoufox profiles use `keepassxc_enabled` to control the bundled extension. Old CloakBrowser profiles retain their existing extension launch arguments. Firefox preferences can be set with `firefox_preferences` (JSON values: boolean, integer, or string); the native Firefox default is strict tracking protection, and supplied preferences can override those defaults. Camoufox exposes `camoufox_config` for advanced fingerprint properties. Generated Camoufox identities are saved in the profile directory; changing the seed or fingerprint settings regenerates the identity. Use a WebGL vendor/renderer pair supported by Camoufox for the selected platform. Humanization and GeoIP are available for CloakBrowser/Camoufox.

Native proxies support HTTP, HTTPS, and unauthenticated SOCKS5. HTTP(S) proxy credentials are provided by a local per-profile extension without putting credentials on the process command line. Firefox ESR permits that local authentication extension by disabling its signature requirement for profiles using proxy credentials. Authenticated SOCKS5 is not supported by native Chromium/Firefox. Native profiles use normal windowed subprocess launches and reject headless/automation arguments. Debugging ports are controlled through explicit toggles, rather than custom launch arguments. Headless mode remains available for CloakBrowser and Camoufox.

GPU acceleration is optional for Chromium, Firefox, and Camoufox: enable **Use GPU when available** and expose accessible `/dev/dri` render devices. Without a render device the profile falls back to software rendering. CloakBrowser continues to use software rendering.

```bash
# Only on hosts that provide /dev/dri:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
# Or add --device /dev/dri:/dev/dri to docker run.
```

Each browser has a persistent, isolated Home directory at `<user_data_dir>/Home`; its default downloads directory is `Home/Downloads`. Open **Files** in Profile settings or the folder button in the VNC toolbar to browse, upload (up to 512 MiB per file), and download files. Uploading an existing filename is rejected to preserve its contents. The VNC back button returns to Profile settings while leaving the browser running.

Clipboard forwarding keeps its X11 selection owner in the foreground and cleans it up on profile stop. Reads/writes have timeouts, and automatic synchronization retries transient errors instead of stopping permanently. If the frontend browser denies clipboard access, the manual transfer buttons offer a text dialog.

Chromium, Firefox, and Camoufox offer three **WebRTC** modes through `webrtc_mode`: `disabled`, `protect_ip`, and `enabled`. New profiles default to `disabled`; existing profiles migrate to `enabled` to preserve their behavior. Changes take effect on the next launch. Disabled blocks peer connections and data channels; Chromium uses a bundled webpage extension plus native IP handling, while Firefox/Camoufox use native preferences. Chromium's block covers ordinary webpages rather than removing WebRTC from its engine. Protect IP suppresses local candidates and restricts direct traffic; calls may require a compatible proxy or TURN server. Enabled restores the preferences that existed before this feature took ownership. Custom Firefox preferences and browser-supported launch arguments can override these defaults. CloakBrowser keeps its existing behavior.

Remote access is configured through `remote_debugging_enabled` (Chromium CDP or Firefox/Camoufox BiDi) and `playwright_enabled` (Camoufox only). Both default to false for new and migrated profiles; CloakBrowser keeps its existing CDP endpoint. Enable the controls in the profile form to see the endpoint and Python connection example. Ports are assigned automatically and listen on loopback; the manager proxies WebSockets through `/api/profiles/{id}/cdp`, `/api/profiles/{id}/bidi`, and `/api/profiles/{id}/playwright`, with the existing authentication and Origin checks. Changes require a profile restart. Firefox BiDi can make remote control detectable. Camoufox BiDi and Playwright controls are mutually exclusive because its bundled Juggler conflicts with the native Remote Agent. BiDi mode launches Camoufox as a normal subprocess with the same fingerprint and profile. Camoufox Playwright clients use version 1.62.0 and share the persistent context shown in VNC; disconnecting a client leaves the profile running. The server uses version-pinned Playwright persistent/shared launch options, so changing that dependency requires repeating the protocol E2E. See [Firefox BiDi connection documentation](https://developer.mozilla.org/en-US/docs/Web/WebDriver/How_to/Create_BiDi_connection).

Docker E2E coverage is in `scripts/e2e_browser_types.py`, `scripts/e2e_webrtc.py`, and `scripts/e2e_remote_protocols.py`. Run their `seed` modes in an old image and `verify` in the new image using the same disposable `/data` volume. Native browser page state is reported to a local fixture without attaching an automation or debugging connection.

Browser versions are fixed in `docker/browser-versions.json`: Chromium 154.0.8037.92, Firefox ESR 153.4.0, Camoufox 156.0.1 beta.36, and the latest CloakBrowser available without a license key, 146.0.7680.177.5. The CloakBrowser Python SDK is pinned to 0.6.0, which supports exact binary downloads; newer Pro binaries require a license key and are not included. Firefox/Camoufox application updates, Chromium component updates, and CloakBrowser background binary updates are disabled. Native browser APT packages are held.

For a new release, run `python scripts/build_image.py --refresh-lock`, review and commit its version lock and SDK pins, then tag that commit. Build with `python scripts/build_image.py --tag IMAGE --release-tag GIT_TAG`. Rebuilds use the versions committed in the tag and fail if an exact package or checksum is unavailable. Use `--tag IMAGE` without `--release-tag` for development builds. The lock fixes the four browser versions; auxiliary OS packages and the base image do not provide byte-for-byte image reproducibility.

```bash
docker run --init -p 8080:8080 -v cloakprofiles:/data \
  -e KEEPASSXC_DATABASE_PASSWORD=your-database-secret \
  cloakhq/cloakbrowser-manager
```

Or build from source:

```bash
git clone https://github.com/CloakHQ/CloakBrowser-Manager.git
cd CloakBrowser-Manager
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080) in your browser. Create a profile. Click Launch. Done.

> **Early alpha** — this project is under active development. Expect bugs. If you find one, please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues).

## Why Not Just Use a VPN?

A VPN only changes your IP. Incognito only clears cookies. Chrome profiles share the same hardware fingerprint underneath. Platforms use 50+ signals to link your accounts — canvas, WebGL, audio, GPU, fonts, screen size, timezone.

Each CloakBrowser profile generates a completely different device identity. To the website, each profile looks like a different computer.

| Solution | What it changes | Accounts linked? |
|----------|----------------|-----------------|
| VPN | IP address only | Yes — same fingerprint |
| Incognito | Clears cookies | Yes — same fingerprint |
| Chrome profiles | Separate bookmarks/cookies | Yes — same hardware fingerprint |
| **CloakBrowser** | **Everything — full device identity per profile** | **No** |

## Features

- **Profile management** — create, edit, delete browser profiles with unique fingerprints
- **Per-profile settings** — fingerprint seed, proxy, timezone, locale, user agent, screen size, platform
- **One-click launch/stop** — each profile runs as an isolated CloakBrowser instance
- **Session persistence** — cookies, localStorage, and cache survive browser restarts
- **Passkey support** — each profile can run an isolated KeePassXC database through the bundled KeePassXC-Browser extension
- **In-browser viewing** — interact with launched browsers via noVNC, directly in the web GUI
- **Playwright/Puppeteer API** — connect to any running profile programmatically via CDP, while still watching it live in the browser
- **Optional authentication** — protect the web UI and API with a single token, or run wide open locally
- **Powered by CloakBrowser** — 32 source-level C++ patches, passes Cloudflare Turnstile, 0.9 reCAPTCHA v3 score

## Stack

- **Backend**: FastAPI (Python)
- **Frontend**: React + Tailwind CSS
- **Browser viewer**: noVNC (WebSocket-based VNC client)
- **Database**: SQLite
- **Browser engine**: [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) (stealth Chromium binary)

## Development

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Docker

```bash
docker compose up --build
```

The included Compose configuration uses `change-me-keepassxc` as a development
default for `KEEPASSXC_DATABASE_PASSWORD`. Set a private value before production
use:

```bash
export KEEPASSXC_DATABASE_PASSWORD='replace-with-a-strong-secret'
docker compose up --build
```

New profiles include both `--disable-extensions-except` and `--load-extension`
arguments for the bundled KeePassXC-Browser extension. The first overrides
CloakBrowser's extension-disable default; the second loads the unpacked
extension and is also the KeePassXC lifecycle switch. Removing the
`--load-extension=/opt/cloakbrowser/extensions/keepassxc-browser` argument
prevents KeePassXC from starting for that profile. Each enabled profile stores
its database and KeePassXC configuration under
`/data/profiles/<profile-id>/KeePassXC`.

The manager reads the database password once at startup, removes it from its
child-process environment, and sends it to KeePassXC only through standard
input. Changing the environment value does not re-encrypt existing databases;
profiles created with the old value will fail to start until the matching
password is restored or their database is re-encrypted manually.

KeePassXC automatic database locking is disabled for these managed instances,
including idle, minimize, screen-lock, and user-switch triggers. This keeps the
browser integration available for the lifetime of the profile; manually
locking the database from KeePassXC remains possible.

For profiles using KeePassXC, the browser viewer toolbar includes a key button.
It brings KeePassXC to the front and fills the VNC display; clicking it again
while KeePassXC has focus hides all of its windows, then restores, fills, and
focuses that profile's Chromium window. Profiles without the bundled extension
load argument do not show this button.

Non-headless Chromium windows are also restored, positioned at the display
origin, filled to the configured VNC resolution, and focused after launch.

The Compose service uses `init: true`, and direct `docker run` examples use
`--init`, so PID 1 reaps orphaned Chromium, Crashpad, KeePassXC Browser Proxy,
and clipboard helper processes. Recreate existing containers after upgrading;
restarting a container created without an init process does not add one.

The image uses KeePassXC 2.7.12 from its checksum-pinned official x86_64
AppImage. Its bundled Qt runtime avoids the reproducible system-Qt crash that
can occur when a distro build is unlocked with `--pw-stdin` as a background
process. KeePassXC does not currently publish a Linux arm64 AppImage, so this
Docker build targets amd64 rather than silently falling back to the affected
distro runtime.

## Requirements

- Docker (20.10+)
- ~2 GB disk (image + binary)
- ~512 MB RAM per running profile

## Updating

Pull the latest image and restart:

```bash
docker pull cloakhq/cloakbrowser-manager
docker stop <container-id>
docker run --init -p 8080:8080 -v cloakprofiles:/data \
  -e KEEPASSXC_DATABASE_PASSWORD=your-database-secret \
  cloakhq/cloakbrowser-manager
```

Your profiles and session data are stored in the `cloakprofiles` volume and persist across updates.

## Automation API

Every running profile exposes a CDP (Chrome DevTools Protocol) endpoint. Connect Playwright or Puppeteer to automate a profile while watching it live in the browser.

```python
from playwright.async_api import async_playwright

async with async_playwright() as pw:
    browser = await pw.chromium.connect_over_cdp(
        "http://localhost:8080/api/profiles/<profile-id>/cdp"
    )
    page = browser.contexts[0].pages[0]
    await page.goto("https://example.com")
```

```javascript
const { chromium } = require("playwright");

const browser = await chromium.connectOverCDP(
  "http://localhost:8080/api/profiles/<profile-id>/cdp"
);
const page = browser.contexts()[0].pages()[0];
await page.goto("https://example.com");
```

The CDP URL is available in the toolbar (code icon) when a profile is running. The same browser session is accessible both visually through VNC and programmatically through the API.

## Remote Access

The container binds to localhost only. To access from a remote server:

```bash
ssh -L 8080:localhost:8080 your-server
```

Then open `http://localhost:8080`.

## Authentication

By default, there is no authentication (ideal for local use). To protect the web UI and API when hosting on a network, set the `AUTH_TOKEN` environment variable:

```bash
docker run --init -p 8080:8080 -v cloakprofiles:/data \
  -e AUTH_TOKEN=your-secret-token \
  -e KEEPASSXC_DATABASE_PASSWORD=your-database-secret \
  cloakhq/cloakbrowser-manager
```

Or in `docker-compose.yml`:

```yaml
environment:
  - AUTH_TOKEN=your-secret-token
  - KEEPASSXC_DATABASE_PASSWORD=your-database-secret
```

When `AUTH_TOKEN` is set:

- The web UI shows a login page. Enter the token to unlock.
- API consumers pass the token via `Authorization: Bearer <token>` header.
- VNC WebSocket connections are authenticated via the login cookie.
- The `/api/status` endpoint remains unauthenticated (for Docker healthcheck).

> **Note**: The auth token is transmitted in cleartext over HTTP. If you expose the Manager to the internet, put it behind a reverse proxy with HTTPS (Caddy, nginx, Traefik).

## License

- **This application** (GUI source code) — MIT. See [LICENSE](LICENSE).
- **CloakBrowser binary** (compiled Chromium) — free to use, no redistribution. See [BINARY-LICENSE.md](BINARY-LICENSE.md).

The GUI application requires the CloakBrowser Chromium binary to function. The binary is automatically downloaded on first launch and is governed by its own license terms. If you fork or redistribute this application, your users must comply with the [CloakBrowser Binary License](BINARY-LICENSE.md).

## Contributing

Contributions are welcome. Please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues) first to discuss what you'd like to change.

## Links

- **CloakBrowser** — [github.com/CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
- **Website** — [cloakbrowser.dev](https://cloakbrowser.dev)
- **Bug reports** — [GitHub Issues](https://github.com/CloakHQ/CloakBrowser-Manager/issues)
- **Contact** — cloakhq@pm.me
