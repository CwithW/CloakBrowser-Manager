import type { BrowserType } from "../lib/api";

interface BrowserConnectionsProps {
  profileId?: string;
  browserType: BrowserType;
  remoteDebugging: boolean;
  playwright: boolean;
  onRemoteDebugging: (enabled: boolean) => void;
  onPlaywright: (enabled: boolean) => void;
}

function Connection({ protocol, profileId }: { protocol: "cdp" | "bidi" | "playwright"; profileId?: string }) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const endpoint = `${scheme}://${window.location.host}/api/profiles/${profileId ?? "<profile-id>"}/${protocol}`;
  const auth = 'import os\ntoken = os.environ.get("AUTH_TOKEN", "")\nheaders = {"Authorization": f"Bearer {token}"} if token else {}';
  const code = protocol === "bidi"
    ? `import asyncio, json, websockets\n${auth}\n\nasync def main():\n    async with websockets.connect(${JSON.stringify(endpoint)}, additional_headers=headers) as ws:\n        await ws.send(json.dumps({"id": 1, "method": "session.new", "params": {"capabilities": {}}}))\n        print(await ws.recv())\n\nasyncio.run(main())`
    : `import asyncio\nfrom playwright.async_api import async_playwright\n${auth}\n\nasync def main():\n    async with async_playwright() as p:\n        browser = await p.${protocol === "cdp" ? "chromium.connect_over_cdp" : "firefox.connect"}(${JSON.stringify(endpoint)}, headers=headers)\n        context = browser.contexts[0]\n        print([page.url for page in context.pages])\n        await browser.close()  # Disconnect this client\n\nasyncio.run(main())`;
  return <div className="space-y-2">
    <label className="label" htmlFor={`${protocol}-endpoint`}>{protocol === "playwright" ? "Playwright" : protocol.toUpperCase()} endpoint</label>
    <input id={`${protocol}-endpoint`} className="input font-mono text-xs" readOnly value={endpoint} onFocus={(e) => e.target.select()} />
    {protocol === "playwright" && <p className="text-xs text-gray-500">Use Playwright 1.62.0. The default context is the profile shown in VNC.</p>}
    <pre className="bg-surface-2 rounded p-3 text-xs text-gray-300 overflow-x-auto"><code>{code}</code></pre>
  </div>;
}

export function BrowserConnections({ profileId, browserType, remoteDebugging, playwright, onRemoteDebugging, onPlaywright }: BrowserConnectionsProps) {
  const chromium = browserType === "cloakbrowser" || browserType === "chromium";
  return <section className="space-y-3">
    <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Remote connections</h3>
    {browserType !== "cloakbrowser" && <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
      <input type="checkbox" checked={remoteDebugging} onChange={(e) => onRemoteDebugging(e.target.checked)} />
      {chromium ? "Expose CDP" : "Expose WebDriver BiDi"}
    </label>}
    {browserType === "camoufox" && <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
      <input type="checkbox" checked={playwright} onChange={(e) => onPlaywright(e.target.checked)} />
      Expose Camoufox Playwright
    </label>}
    {browserType === "camoufox" && <p className="text-xs text-gray-500">BiDi and Playwright are mutually exclusive. Enabling one turns the other off.</p>}
    <p className="text-xs text-gray-500">Endpoints work while the profile is running. Changes take effect on the next launch. Set AUTH_TOKEN in the client environment if manager authentication is enabled.</p>
    {!chromium && remoteDebugging && <p className="text-xs text-amber-400">Enabling BiDi can make remote control detectable by websites.</p>}
    {(browserType === "cloakbrowser" || remoteDebugging) && <Connection protocol={chromium ? "cdp" : "bidi"} profileId={profileId} />}
    {browserType === "camoufox" && playwright && <Connection protocol="playwright" profileId={profileId} />}
  </section>;
}
