async function initialize() {
  const {url: readyUrl} = await (await fetch(chrome.runtime.getURL("ready.json"), {cache: "no-store"})).json();
  await chrome.scripting.unregisterContentScripts();
  if (!readyUrl) return;
  await chrome.scripting.registerContentScripts([{
    id: "block-webrtc", matches: ["<all_urls>"], js: ["block.js"],
    world: "MAIN", runAt: "document_start", allFrames: true,
    matchOriginAsFallback: true, persistAcrossSessions: false,
  }]);
  await fetch(readyUrl, {method: "POST"});
}
// Installed and startup can both fire on a fresh profile; serialize them.
let initialized;
const start = () => initialized ??= initialize();
chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
start();
