/* Cross-browser MV3 service worker.  The only persisted extension data is
 * local connection configuration and its one designated browser tab ID. */
const browserApi = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = {
  server: "http://127.0.0.1:8765",
  token: "",
  profile: "default",
  libraryPlayerTabId: null,
  expectedTrack: null,
  widgetPosition: null,
};

let socket = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let connectInFlight = false;
let pairingInFlight = null;

async function storageGet(keys) {
  const result = browserApi.storage.local.get(keys);
  if (result && typeof result.then === "function") return result;
  return new Promise((resolve) => browserApi.storage.local.get(keys, resolve));
}

async function storageSet(values) {
  const result = browserApi.storage.local.set(values);
  if (result && typeof result.then === "function") return result;
  return new Promise((resolve) => browserApi.storage.local.set(values, resolve));
}

async function config() {
  return { ...DEFAULTS, ...(await storageGet(DEFAULTS)) };
}

async function detectedProfile() {
  const userAgent = String(globalThis.navigator?.userAgent || "").toLowerCase();
  if (userAgent.includes("zen")) return "zen";
  try {
    if (typeof browserApi.runtime.getBrowserInfo === "function") {
      const info = await browserApi.runtime.getBrowserInfo();
      const name = String(info?.name || "").toLowerCase();
      if (name.includes("firefox")) return "firefox";
      if (name.includes("chrome")) return "chrome";
    }
  } catch (_) {
    // Chrome does not expose getBrowserInfo; the namespace fallback below is
    // enough for normal Firefox/Chrome builds.
  }
  return globalThis.browser ? "firefox" : "chrome";
}

async function discoverPairing(settings) {
  if (!settings.server) return settings;
  const response = await fetch(new URL("/api/health", settings.server), { cache: "no-store" });
  if (!response.ok) throw new Error(`server_${response.status}`);
  const health = await response.json();
  const token = String(health?.pairing_token || "").trim();
  if (!token) return settings;
  const profile = settings.profile && settings.profile !== "default"
    ? settings.profile : await detectedProfile();
  await storageSet({ token, profile });
  return { ...settings, token, profile };
}

async function pairedConfig() {
  const settings = await config();
  if (settings.token) return settings;
  if (!pairingInFlight) {
    pairingInFlight = discoverPairing(settings).finally(() => {
      pairingInFlight = null;
    });
  }
  try {
    return await pairingInFlight;
  } catch (_) {
    return settings;
  }
}

function websocketUrl(server, token) {
  const url = new URL(server);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/extension";
  url.search = new URLSearchParams({ token }).toString();
  return url.toString();
}

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

async function currentPlayerTabId() {
  return (await config()).libraryPlayerTabId;
}

async function updatePlayerTabId(tabId) {
  await storageSet({ libraryPlayerTabId: tabId ?? null });
}

async function getTab(tabId) {
  if (tabId == null) return null;
  try {
    return await browserApi.tabs.get(tabId);
  } catch (_) {
    await updatePlayerTabId(null);
    return null;
  }
}

async function ensurePlayerTab(url) {
  const oldId = await currentPlayerTabId();
  const existing = await getTab(oldId);
  if (existing) {
    // Updating the existing player is deliberately the only navigation path:
    // a queue item never creates another provider tab.
    const updated = await browserApi.tabs.update(existing.id, { url, active: true });
    await updatePlayerTabId(updated.id);
    return updated;
  }
  const created = await browserApi.tabs.create({ url, active: true });
  await updatePlayerTabId(created.id);
  return created;
}

async function sendHello() {
  const settings = await config();
  const tab = await getTab(settings.libraryPlayerTabId);
  send({
    type: "hello",
    profile: settings.profile || "default",
    tab_id: tab?.id ?? null,
    url: tab?.url ?? "",
    resume: Boolean(tab),
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(15_000, reconnectDelay * 2);
}

async function connect() {
  if (connectInFlight) return;
  connectInFlight = true;
  try {
    const settings = await pairedConfig();
    if (!settings.token || !settings.server) {
      scheduleReconnect();
      return;
    }
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    socket = new WebSocket(websocketUrl(settings.server, settings.token));
    socket.addEventListener("open", async () => {
      reconnectDelay = 1000;
      await sendHello();
    });
    socket.addEventListener("message", async (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.type !== "command") return;
      if (message.command === "navigate" && message.url) {
        await storageSet({
          expectedTrack: {
            trackId: message.track_id ?? null,
            provider: message.provider ?? null,
            url: message.url,
            duration: message.expected_duration ?? null,
          },
        });
        try {
          await ensurePlayerTab(message.url);
        } catch (error) {
          send({ type: "blocked", status: "navigation_failed", detail: String(error?.message || error) });
        }
      }
      if (message.command === "activate") {
        const tab = await getTab(await currentPlayerTabId());
        if (tab) await browserApi.tabs.update(tab.id, { active: true });
      }
      if (["play", "pause", "play_pause", "stop"].includes(message.command)) {
        const tab = await getTab(await currentPlayerTabId());
        if (!tab) {
          send({ type: "blocked", status: "player_tab_missing", detail: "Repair or start the player tab first." });
          return;
        }
        try {
          await browserApi.tabs.sendMessage(tab.id, { type: "library-player-control", command: message.command });
        } catch (error) {
          send({ type: "blocked", status: "control_failed", detail: String(error?.message || error) });
        }
      }
    });
    socket.addEventListener("close", async (event) => {
      if (event.code === 1008) await storageSet({ token: "" });
      scheduleReconnect();
    });
    socket.addEventListener("error", () => socket?.close());
  } catch (_) {
    scheduleReconnect();
  } finally {
    connectInFlight = false;
  }
}

async function lookupProviderPage(url) {
  const settings = await pairedConfig();
  if (!settings.server || !settings.token) return { supported: false, reason: "not_paired" };
  const endpoint = new URL("/api/track-status", settings.server);
  endpoint.searchParams.set("url", url);
  const response = await fetch(endpoint, { cache: "no-store" });
  if (!response.ok) return { supported: false, reason: `server_${response.status}` };
  return response.json();
}

async function extensionApi(path, method, body) {
  const settings = await pairedConfig();
  if (!settings.server || !settings.token) throw new Error("not_paired");
  const response = await fetch(new URL(path, settings.server), {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`server_${response.status}`);
  return response.json();
}

browserApi.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "provider-page-seen") {
    lookupProviderPage(String(message.url || ""))
      .then(sendResponse)
      .catch((error) => sendResponse({ supported: false, reason: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "provider-pages-seen") {
    extensionApi("/api/track-status-batch", "POST", { urls: message.urls || [] })
      .then((result) => sendResponse(result.results || {}))
      .catch((error) => sendResponse({ error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "provider-save-track") {
    extensionApi("/api/extension/save", "POST", message)
      .then(sendResponse)
      .catch((error) => sendResponse({ error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "provider-rate-track") {
    extensionApi(`/api/tracks/${encodeURIComponent(message.track_id)}/rating`, "POST", { value: message.value })
      .then(sendResponse)
      .catch((error) => sendResponse({ error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "widget-position-get") {
    config().then((settings) => sendResponse(settings.widgetPosition || null));
    return true;
  }
  if (message?.type === "widget-position-set") {
    const left = Number(message.left);
    const top = Number(message.top);
    if (Number.isFinite(left) && Number.isFinite(top)) {
      storageSet({
        widgetPosition: {
          left: Math.max(0, Math.round(left)),
          top: Math.max(0, Math.round(top)),
        },
      }).then(() => sendResponse({ saved: true }));
    } else {
      sendResponse({ saved: false });
    }
    return true;
  }
  if (message?.type === "open-library-dashboard") {
    config().then((settings) => browserApi.tabs.create({ url: settings.server || DEFAULTS.server }));
  }
  if (message?.type === "extension-forget") {
    storageSet({ ...DEFAULTS, browserFamily: "" }).then(async () => {
      if (socket) {
        try { socket.close(); } catch (_) {}
      }
      sendResponse({ forgotten: true });
    }).catch((error) => sendResponse({ forgotten: false, error: String(error?.message || error) }));
    return true;
  }
  return false;
});

browserApi.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== "library-player-event") return;
  const tabId = sender.tab?.id;
  // Provider content scripts run in the designated tab only.  This prevents
  // a normal YouTube/Bilibili browsing tab from accidentally advancing a queue.
  currentPlayerTabId().then((playerTabId) => {
    if (tabId == null || playerTabId !== tabId) return;
    // The content script's provider URL/duration are authoritative. Expected
    // metadata is diagnostic only and must never overwrite observed evidence.
    config().then((settings) => send({
      expected_track_id: settings.expectedTrack?.trackId ?? null,
      expected_duration: settings.expectedTrack?.duration ?? null,
      ...message,
      tab_id: tabId,
    }));
  });
});

browserApi.tabs.onRemoved.addListener(async (tabId) => {
  if (tabId === await currentPlayerTabId()) {
    await updatePlayerTabId(null);
    send({ type: "player_state", status: "player_tab_closed", tab_id: null });
  }
});

browserApi.runtime.onInstalled.addListener(() => connect());
browserApi.runtime.onStartup.addListener(() => connect());
browserApi.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.server || changes.token || changes.profile) {
    if (socket) socket.close();
    connect();
  }
});
browserApi.action.onClicked.addListener(() => browserApi.runtime.openOptionsPage());

connect();
