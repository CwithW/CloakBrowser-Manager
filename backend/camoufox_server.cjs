// Playwright 1.62's persistent shared server keeps VNC and remote clients on
// the same browser context. Its private launch options are version-pinned.
const { firefox } = require(process.argv[2]);

let server;
let closing = false;
async function close() {
  if (closing) return;
  closing = true;
  try { await server?.close(); } finally { process.exitCode = 0; }
}
process.on("SIGTERM", close);
process.on("SIGINT", close);

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", async () => {
  try {
    server = await firefox.launchServer(JSON.parse(input));
    // Let launchServer's graceful close finish flushing the persistent browser
    // before exiting; a close event alone can precede its cleanup promise.
    process.stdout.write(server.wsEndpoint() + "\n");
  } catch (error) {
    process.stderr.write(String(error) + "\n");
    process.exit(1);
  }
});
