const api = globalThis.browser ?? globalThis.chrome;
const defaults = { server: "http://127.0.0.1:8765", token: "", profile: "default", browserFamily: "" };
const CONNECT_TIMEOUT_MS = 6_000;

async function getSettings() {
  const value = api.storage.local.get(defaults);
  if (value && typeof value.then === "function") return value;
  return new Promise((resolve) => api.storage.local.get(defaults, resolve));
}
async function saveSettings(values) {
  const value = api.storage.local.set(values);
  if (value && typeof value.then === "function") return value;
  return new Promise((resolve) => api.storage.local.set(values, resolve));
}

async function detectedProfile() {
  const userAgent = String(globalThis.navigator?.userAgent || "").toLowerCase();
  if (userAgent.includes("zen")) return "zen";
  try {
    if (typeof api.runtime.getBrowserInfo === "function") {
      const info = await api.runtime.getBrowserInfo();
      const name = String(info?.name || "").toLowerCase();
      if (name.includes("firefox")) return "firefox";
      if (name.includes("chrome")) return "chrome";
    }
  } catch (_) {}
  return globalThis.browser ? "firefox" : "chrome";
}

async function detectedBrowserFamily() {
  const existing = await getSettings();
  if (existing.browserFamily) return existing.browserFamily;
  const family = await detectedProfile();
  await saveSettings({ browserFamily: family });
  return family;
}

async function discoverPairing(serverAddress) {
  const health = await fetch(new URL("/api/health", serverAddress), { cache: "no-store" });
  if (!health.ok) throw new Error(`The local dashboard responded with HTTP ${health.status}.`);
  const data = await health.json();
  const pairingToken = String(data?.pairing_token || "").trim();
  if (!pairingToken) throw new Error("The local dashboard did not provide a pairing token.");
  return pairingToken;
}

function websocketUrl(server, token) {
  const url = new URL(server);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/extension";
  url.search = new URLSearchParams({ token }).toString();
  return url.toString();
}

async function checkLocalServer(serverAddress, pairingToken) {
  const serverUrl = new URL(serverAddress);
  if (serverUrl.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(serverUrl.hostname)) {
    throw new Error("Use the exact loopback address http://127.0.0.1:8765. This local server does not use HTTPS.");
  }
  const health = await fetch(new URL("/api/health", serverUrl), { cache: "no-store" });
  if (!health.ok) throw new Error(`The local dashboard responded with HTTP ${health.status}.`);

  await new Promise((resolve, reject) => {
    let settled = false;
    const socket = new WebSocket(websocketUrl(serverUrl.origin, pairingToken));
    const timer = window.setTimeout(() => finish(new Error("The local server did not complete the WebSocket connection.")), CONNECT_TIMEOUT_MS);
    const finish = (error) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      try { socket.close(); } catch (_) {}
      error ? reject(error) : resolve();
    };
    socket.addEventListener("open", () => {
      try {
        // Use the same first frame as the real background connection. This
        // also verifies that the token is accepted by the application rather
        // than only checking that a TCP/WebSocket port is reachable.
        socket.send(JSON.stringify({
          type: "hello",
          profile: "options-check",
          resume: false,
        }));
        finish();
      } catch (_) {
        finish(new Error("The local WebSocket opened but could not send its handshake."));
      }
    });
    socket.addEventListener("error", () => finish(new Error(
      "Could not open the local WebSocket. Reload the updated extension, then disable HTTPS-Only/HTTPS-upgrade for 127.0.0.1 and use http://127.0.0.1:8765."
    )));
  });
}

const server = document.querySelector("#server");
const token = document.querySelector("#token");
const profile = document.querySelector("#profile");
const browserFamily = document.querySelector("#browser-family");
const status = document.querySelector("#status");

getSettings().then(async (settings) => {
  server.value = settings.server;
  token.value = settings.token;
  const family = await detectedBrowserFamily();
  browserFamily.textContent = family === "zen" ? "Zen (Firefox engine)" : family[0].toUpperCase() + family.slice(1);
  profile.value = settings.profile === "default" ? "Personal" : settings.profile;
  if (token.value) return;
  try {
    token.value = await discoverPairing(new URL(server.value.trim()).origin);
    await saveSettings({ server: new URL(server.value.trim()).origin, token: token.value, profile: profile.value, browserFamily: family });
    status.textContent = "Automatically paired with the local app.";
  } catch (_) {
    status.textContent = "Start MusicLibrary.bat to auto-pair this extension.";
  }
});
document.querySelector("#settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  try {
    const url = new URL(server.value.trim());
    const serverAddress = url.origin;
    let pairingToken = token.value.trim();
    button.disabled = true;
    status.textContent = "Finding the local dashboard and checking the connection…";
    if (!pairingToken) pairingToken = await discoverPairing(serverAddress);
    token.value = pairingToken;
    await saveSettings({ server: serverAddress, token: pairingToken, profile: profile.value.trim() || "Personal", browserFamily: await detectedBrowserFamily() });
    try {
      await checkLocalServer(serverAddress, pairingToken);
      status.textContent = "Connected and saved. This browser profile is paired to the local app.";
    } catch (error) {
      status.textContent = `Settings saved, but live player connection failed: ${error?.message || error}`;
    }
  } catch (error) {
    status.textContent = error?.message || "Use http://127.0.0.1:8765 and make sure MusicLibrary.bat is running.";
  } finally {
    button.disabled = false;
  }
});
document.querySelector("#test-connection").addEventListener("click", async () => {
  const button = document.querySelector("#test-connection");
  try {
    button.disabled = true;
    status.textContent = "Testing the local HTTP and WebSocket connection…";
    const serverAddress = new URL(server.value.trim()).origin;
    const pairingToken = token.value.trim() || await discoverPairing(serverAddress);
    token.value = pairingToken;
    await checkLocalServer(serverAddress, pairingToken);
    status.textContent = "Connection works. Nothing was changed.";
  } catch (error) { status.textContent = error?.message || "Connection test failed."; }
  finally { button.disabled = false; }
});
document.querySelector("#forget-browser").addEventListener("click", async () => {
  if (!window.confirm("Forget this browser's pairing, player tab, and widget position?")) return;
  try {
    const response = await new Promise((resolve) => api.runtime.sendMessage({ type: "extension-forget" }, resolve));
    if (!response?.forgotten) throw new Error(response?.error || "Could not forget browser settings.");
    token.value = "";
    profile.value = "Personal";
    status.textContent = "This browser has been unpaired. Start the local app to pair again.";
  } catch (error) { status.textContent = error?.message || "Could not forget browser settings."; }
});
document.querySelector("#open-dashboard").addEventListener("click", () => {
  api.tabs.create({ url: server.value.trim() || defaults.server });
});
