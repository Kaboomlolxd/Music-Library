const state = {
  playlists: [],
  pools: [],
  creators: [],
  subscriptions: [],
  activePlaylistId: null,
  tracks: [],
  total: 0,
  offset: 0,
  selectedTrackIds: new Set(),
  queue: null,
  health: null,
  mergeIds: [],
  lastCurrent: null,
  toastTrack: null,
  toastTimer: null,
  toastRemaining: 5000,
  toastStarted: 0,
  bulkOperations: [],
  bulkPreviewRequest: null,
  lastBulkOperationId: null,
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));

// A browser may restore form/control state after a reload, including the
// modal's DOM state. Always start with the modal closed; only openDialog()
// may expose it after real content has been rendered.
const initialDialog = document.querySelector("#dialog");
if (initialDialog) {
  initialDialog.hidden = true;
  initialDialog.setAttribute("aria-hidden", "true");
}

const api = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* no JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
};
const post = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

function toast(message, isError = false) {
  const status = $("#import-status");
  status.textContent = message;
  status.style.color = isError ? "var(--danger)" : "";
}

function setImportProgress(message) {
  const wrap = $("#import-progress-wrap");
  const bar = $("#import-progress");
  const label = $("#import-progress-label");
  const completed = Number(message.completed || 0);
  const total = Number(message.discovered_count || 0);
  wrap.hidden = false;
  if (total > 0) {
    bar.max = total;
    bar.value = Math.min(completed, total);
    label.textContent = `${completed}/${total}${message.title ? ` · ${message.title}` : ""}`;
  } else {
    bar.removeAttribute("value");
    label.textContent = message.source
      ? `Discovering ${message.source}`
      : "Discovering sources…";
  }
}

function finishImportProgress(message, isError = false) {
  $("#import-progress-wrap").hidden = true;
  const result = message.result;
  if (isError) {
    toast(`Import failed: ${message.error}`, true);
    return;
  }
  if (Array.isArray(result?.suggestions)) {
    toast(`Relation scan complete: ${result.suggestions.length} suggestions found.`);
    loadReviewInbox().catch(() => {});
    return;
  }
  toast(`Import complete: ${result?.added_memberships ?? 0} added, ${result?.tracks_upserted ?? 0} metadata records saved.`);
}

function parseViewCount(value) {
  const text = value.trim().toLowerCase().replaceAll(",", "");
  if (!text) return null;
  const match = text.match(/^(\d+(?:\.\d+)?)\s*([km])?$/);
  if (!match) throw new Error("Minimum views must look like 100k, 1.5m, or 1200.");
  return Number(match[1]) * (match[2] === "m" ? 1_000_000 : match[2] === "k" ? 1_000 : 1);
}

function activePlaylist() {
  return state.playlists.find((playlist) => playlist.id === state.activePlaylistId) || null;
}

const LIBRARY_PREF_KEY = "local-music-library.view-preferences.v1";
const LIBRARY_PREF_CONTROLS = [
  "track-search", "provider-filter", "creator-library-filter", "artist-library-filter",
  "album-library-filter", "genre-library-filter", "availability-filter", "rating-filter",
  "min-duration-filter", "max-duration-filter", "min-views-filter", "max-views-filter",
  "override-filter", "track-sort", "track-order",
];
const LIBRARY_FILTER_CONTROLS = [
  "provider-filter", "creator-library-filter", "artist-library-filter",
  "album-library-filter", "genre-library-filter", "availability-filter",
  "rating-filter", "min-duration-filter", "max-duration-filter",
  "min-views-filter", "max-views-filter", "override-filter",
];
function updateFilterSummary() {
  const summary = document.querySelector(".filter-summary");
  if (!summary) return;
  const active = LIBRARY_FILTER_CONTROLS
    .map((id) => document.getElementById(id)?.value?.trim())
    .filter(Boolean).length;
  summary.textContent = active ? `${active} active filter${active === 1 ? "" : "s"}` : "Refine this view";
}
function saveLibraryPreferences() {
  try {
    const controls = {};
    for (const id of LIBRARY_PREF_CONTROLS) {
      const element = document.getElementById(id);
      if (element) controls[id] = element.value;
    }
    localStorage.setItem(LIBRARY_PREF_KEY, JSON.stringify({
      controls,
      activePlaylistId: state.activePlaylistId,
      view: document.querySelector(".view-link.active")?.dataset.view || "home",
      filterDrawerOpen: Boolean(document.querySelector(".filter-drawer")?.open),
    }));
  } catch (_) {
    // Private browsing or disabled storage should not block library use.
  }
}
function restoreLibraryPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(LIBRARY_PREF_KEY) || "{}");
    for (const [id, value] of Object.entries(saved.controls || {})) {
      const element = document.getElementById(id);
      if (element && value !== undefined) element.value = value;
    }
    if (saved.activePlaylistId !== null && saved.activePlaylistId !== undefined && saved.activePlaylistId !== "") {
      state.activePlaylistId = Number(saved.activePlaylistId);
    }
    const drawer = document.querySelector(".filter-drawer");
    if (drawer && saved.filterDrawerOpen) drawer.open = true;
    updateFilterSummary();
    return String(saved.view || "home");
  } catch (_) {
    return "home";
  }
}
async function applyView(view) {
  const selectedView = view || "home";
  document.querySelectorAll(".view-link").forEach((item) => {
    const active = item.dataset.view === selectedView;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.hidden = !String(panel.dataset.viewPanel || "").split(/\s+/).includes(selectedView);
  });
  saveLibraryPreferences();
  if (selectedView === "backups") await loadBackups();
  if (selectedView === "imports" || selectedView === "home") await loadJobs();
  if (selectedView === "settings") await loadSettings();
  if (selectedView === "maintenance") await runMaintenance();
  updateFilterSummary();
}

function currentSelection() {
  const selection = {
    include_repeats: $("#include-repeats").checked,
    provider: $("#provider-filter").value || undefined,
    creator: $("#creator-filter").value.trim() || undefined,
  };
  const rating = $("#rating-filter").value;
  if (rating !== "") selection.min_rating = Number(rating);
  const poolPlaylistIds = [...$("#pool-playlists").selectedOptions].map((option) => Number(option.value));
  if (poolPlaylistIds.length) selection.playlist_ids = poolPlaylistIds;
  if (state.selectedTrackIds.size) {
    selection.track_ids = [...state.selectedTrackIds];
  } else if (!poolPlaylistIds.length && state.activePlaylistId !== null) {
    selection.playlist_ids = [state.activePlaylistId];
  }
  return selection;
}

function renderPlaylists() {
  const list = $("#playlist-list");
  list.innerHTML = state.playlists.map((playlist) => `
    <button class="playlist-link ${playlist.id === state.activePlaylistId ? "active" : ""}" data-playlist-id="${playlist.id}">
      ${escapeHtml(playlist.name)} <span class="muted">(${playlist.active_count})</span>
    </button>`).join("") || '<span class="muted">Create a local playlist before importing.</span>';
  const target = $("#import-playlist");
  const previous = target.value;
  target.innerHTML = '<option value="">Choose a local playlist…</option>' + state.playlists
    .map((playlist) => `<option value="${playlist.id}">${escapeHtml(playlist.name)}</option>`).join("");
  if (previous) target.value = previous;
  const selectionTarget = $("#selection-playlist");
  if (selectionTarget) {
    const previousSelection = selectionTarget.value;
    selectionTarget.innerHTML = '<option value="">Add selected to playlist…</option>' + state.playlists
      .map((playlist) => `<option value="${playlist.id}">${escapeHtml(playlist.name)}</option>`).join("");
    if (previousSelection) selectionTarget.value = previousSelection;
  }
  const mergeSource = $("#merge-source");
  mergeSource.innerHTML = state.playlists.map((playlist) =>
    `<option value="${playlist.id}">${escapeHtml(playlist.name)}</option>`).join("");
  const poolSelect = $("#pool-playlists");
  const selectedPoolIds = new Set([...poolSelect.selectedOptions].map((option) => option.value));
  poolSelect.innerHTML = state.playlists.map((playlist) =>
    `<option value="${playlist.id}" ${selectedPoolIds.has(String(playlist.id)) ? "selected" : ""}>${escapeHtml(playlist.name)}</option>`
  ).join("");
}
async function loadSavedSearches() {
  const searches = await api("/api/searches");
  const select = $("#saved-search-select");
  if (!select) return;
  select.innerHTML = '<option value="">Saved searches…</option>' + searches.map((search) => `<option value="${search.id}" data-query='${escapeHtml(JSON.stringify(search.query))}'>${escapeHtml(search.name)}</option>`).join("");
}
function currentLibraryQuery() {
  const query = {};
  for (const [id, key] of [
    ["track-search", "q"], ["provider-filter", "provider"], ["creator-library-filter", "uploader"],
    ["artist-library-filter", "artist"], ["album-library-filter", "album"], ["genre-library-filter", "genre"],
    ["availability-filter", "availability"], ["rating-filter", "min_rating"], ["min-duration-filter", "min_duration"],
    ["max-duration-filter", "max_duration"], ["min-views-filter", "min_views"], ["max-views-filter", "max_views"],
    ["override-filter", "has_override"], ["track-sort", "sort"], ["track-order", "order"],
  ]) {
    const value = document.getElementById(id)?.value;
    if (value !== undefined && value !== "") query[key] = value;
  }
  return query;
}

function renderPools() {
  const list = $("#pool-list");
  list.classList.toggle("muted", state.pools.length === 0);
  list.innerHTML = state.pools.length ? state.pools.map((pool) => `
    <div class="pool-row"><button class="pool-link" data-pool-id="${pool.id}">${escapeHtml(pool.name)}</button>
    <button class="icon" data-delete-pool="${pool.id}" title="Delete saved pool">×</button></div>`).join("") :
    "No saved pools yet.";
}

function renderCreators() {
  const list = $("#creator-list");
  list.classList.toggle("muted", state.creators.length === 0);
  list.innerHTML = state.creators.length
    ? state.creators.map((creator) => `
      <div class="creator-row">
        <button class="creator-link ${$("#creator-library-filter").value === creator.name && state.activePlaylistId === null ? "active" : ""}"
          data-creator-name="${escapeHtml(creator.name)}" title="Show this creator's saved music">
          ${escapeHtml(creator.name)} <span class="muted">(${creator.track_count})</span>
        </button>
        ${creator.url ? `<button class="icon creator-subscribe" data-subscribe-creator-url="${escapeHtml(creator.url)}" data-subscribe-creator-name="${escapeHtml(creator.name)}" title="Subscribe to this creator">+</button>` : ""}
      </div>`).join("")
    : "No creators detected yet.";
}

function renderSubscriptions() {
  const list = $("#subscription-list");
  list.classList.toggle("muted", state.subscriptions.length === 0);
  list.innerHTML = state.subscriptions.length
    ? state.subscriptions.map((subscription) => `
      <div class="subscription-row">
        <button class="subscription-name ${subscription.enabled ? "" : "paused"}"
          data-subscription-name="${subscription.id}" title="${escapeHtml(subscription.url)}">
          ${escapeHtml(subscription.name)}
        </button>
        <span class="muted">${subscription.enabled ? "on" : "off"}</span>
      </div>`).join("")
    : "No subscriptions yet.";
}

function renderQueue(queue) {
  state.queue = queue;
  const now = queue?.current;
  $("#now-title").textContent = now ? now.title : "Nothing queued";
  $("#now-creator").textContent = now ? `${now.uploader || now.creator || "Unknown uploader"} · ${now.provider}` :
    "Build a queue from all library, a playlist, a pool, or selected tracks.";
  $("#now-rating").value = now?.rating ?? "";
  $("#queue-preview").innerHTML = (queue?.items || []).slice(0, 30).map((item) => `
    <li class="${item.position === queue.current_position ? "current" : ""}">
      ${escapeHtml(item.uploader || item.creator || "Unknown uploader")} — ${escapeHtml(item.title)}
    </li>`).join("") || "<li>No tracks queued.</li>";
  if (state.lastCurrent && now && state.lastCurrent.id !== now.id) showRatingToast(state.lastCurrent);
  state.lastCurrent = now || null;
}

function renderTracks() {
  const playlist = activePlaylist();
  $("#library-heading").textContent = playlist ? playlist.name : "All Library";
  $("#result-count").textContent = `${state.total.toLocaleString()} track${state.total === 1 ? "" : "s"}${state.selectedTrackIds.size ? ` · ${state.selectedTrackIds.size} selected` : ""}`;
  $("#track-list").innerHTML = state.tracks.map((track) => {
    const memberships = track.membership_id ? `data-membership-id="${track.membership_id}"` : "";
    const selected = state.selectedTrackIds.has(track.id) ? "checked" : "";
    const lastPlayed = track.last_played_at
      ? new Date(track.last_played_at).toLocaleString()
      : "Never played";
    return `<tr>
      <td><input type="checkbox" class="track-select" data-track-id="${track.id}" ${selected}></td>
      <td class="creator-cell">${escapeHtml(track.uploader || track.creator || "Unknown uploader")}</td>
      <td class="title-cell"><a href="${escapeHtml(track.url)}" target="_blank" rel="noopener">${escapeHtml(track.title)}</a><small class="muted">Last played: ${escapeHtml(lastPlayed)}</small></td>
      <td>${escapeHtml(track.provider)}</td>
      <td><input class="table-rating" data-rating-id="${track.id}" inputmode="decimal" value="${track.rating ?? ""}" placeholder="—"></td>
      <td>${track.view_count == null ? "—" : Number(track.view_count).toLocaleString()}</td>
      <td class="actions">
        <button data-action="play-track" data-track-id="${track.id}">Play</button>
        <button data-action="open-track" data-track-id="${track.id}">Default</button>
        <button data-action="copy-link" data-track-id="${track.id}">Copy link</button>
        <button data-action="relations" data-track-id="${track.id}">Explore relations</button>
        ${track.membership_id ? `<button data-action="remove-membership" ${memberships}>Remove</button>
          <button data-action="copy-membership" data-track-id="${track.id}">Copy</button>` : ""}
        <button data-action="hide-track" data-track-id="${track.id}">Hide</button>
      </td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="muted">No tracks match this view.</td></tr>';
  $("#load-more-button").hidden = state.offset + state.tracks.length >= state.total;
  $("#select-page").checked = state.tracks.length > 0 && state.tracks.every((track) => state.selectedTrackIds.has(track.id));
  const removeSelected = $("#remove-selected-button");
  if (removeSelected) {
    removeSelected.disabled = state.activePlaylistId === null || state.selectedTrackIds.size === 0;
    removeSelected.title = state.activePlaylistId === null
      ? "Open a playlist to remove selected memberships"
      : "Remove selected tracks from this playlist";
  }
  const addSelected = $("#add-selected-button");
  if (addSelected) addSelected.disabled = state.selectedTrackIds.size === 0 || !$("#selection-playlist")?.value;
}

function renderMergeOrder() {
  const element = $("#merge-order");
  if (!state.mergeIds.length) {
    element.className = "merge-order muted";
    element.textContent = "Add playlists above. The first one has priority.";
    return;
  }
  element.className = "merge-order";
  element.innerHTML = state.mergeIds.map((id, index) => {
    const playlist = state.playlists.find((entry) => entry.id === id);
    if (!playlist) return "";
    return `<span class="merge-chip" draggable="true" data-merge-index="${index}">${index + 1}. ${escapeHtml(playlist.name)}
      <button data-merge-move="${index}" data-direction="-1" title="Move up">↑</button>
      <button data-merge-move="${index}" data-direction="1" title="Move down">↓</button>
      <button data-merge-remove="${index}" title="Remove">×</button>
    </span>`;
  }).join("");
}

async function loadTracks({ append = false } = {}) {
  const playlist = activePlaylist();
  const offset = append ? state.offset + state.tracks.length : 0;
  const params = new URLSearchParams({
    q: $("#track-search").value.trim(),
    limit: "250",
    offset: String(offset),
  });
  if (playlist) params.set("playlist_id", String(playlist.id));
  if ($("#provider-filter").value) params.set("provider", $("#provider-filter").value);
  if ($("#rating-filter").value) params.set("min_rating", $("#rating-filter").value);
  if ($("#creator-library-filter").value.trim()) params.set("creator", $("#creator-library-filter").value.trim());
  for (const [id, key] of [
    ["artist-library-filter", "artist"], ["album-library-filter", "album"],
    ["genre-library-filter", "genre"], ["availability-filter", "availability"],
    ["min-duration-filter", "min_duration"], ["max-duration-filter", "max_duration"],
    ["min-views-filter", "min_views"], ["max-views-filter", "max_views"],
  ]) {
    const value = $(`#${id}`)?.value;
    if (value) params.set(key, value);
  }
  if ($("#override-filter").value) params.set("has_override", $("#override-filter").value);
  params.set("sort", $("#track-sort").value);
  params.set("order", $("#track-order").value);
  const response = playlist
    ? await api(`/api/playlists/${playlist.id}/tracks?${params}`)
    : await api(`/api/tracks?${params}`);
  state.total = response.total;
  state.offset = offset;
  state.tracks = append ? state.tracks.concat(response.items) : response.items;
  renderTracks();
  updateFilterSummary();
}

async function reload({ tracks = true } = {}) {
  const [health, playlists, pools, creators, subscriptions] = await Promise.all([
    api("/api/health"), api("/api/playlists"), api("/api/pools"),
    api("/api/creators"), api("/api/subscriptions"),
  ]);
  state.health = health;
  state.playlists = playlists;
  state.pools = pools;
  state.creators = creators;
  state.subscriptions = subscriptions;
  $("#stats").textContent = `${health.stats.tracks.toLocaleString()} tracks · ${health.stats.playlists} playlists`;
  $("#pairing-token").value = health.pairing_token;
  renderPlaylists();
  await loadSavedSearches();
  renderPools();
  renderCreators();
  renderSubscriptions();
  renderQueue(health.queue);
  renderMergeOrder();
  renderExtensionStatus(health.extension_sessions);
  if (tracks) await loadTracks();
}

function renderExtensionStatus(sessions) {
  const connected = (sessions || []).filter((session) => session.status !== "disconnected");
  $("#extension-status").textContent = connected.length
    ? `Connected: ${connected.map((session) => `${session.profile} (${session.status})`).join(", ")}`
    : "No browser extension connected. Manual Next is still available.";
}

async function saveRating(trackId, rawValue) {
  const value = rawValue.trim();
  const parsed = value ? Number(value) : null;
  if (parsed !== null && (!Number.isFinite(parsed) || parsed < 0 || parsed > 10)) throw new Error("Rating must be a number from 0 to 10.");
  await post(`/api/tracks/${trackId}/rating`, { value: parsed });
  for (const track of state.tracks) if (track.id === trackId) track.rating = parsed;
  if (state.queue?.current?.id === trackId) state.queue.current.rating = parsed;
  if (state.lastCurrent?.id === trackId) state.lastCurrent.rating = parsed;
  toast(parsed === null ? "Rating cleared." : `Saved ${parsed}/10.`);
  renderTracks();
  renderQueue(state.queue);
}

function clearToast() {
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = null;
  $("#toast").hidden = true;
  state.toastTrack = null;
}

function startToastTimer(milliseconds) {
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastRemaining = milliseconds;
  state.toastStarted = Date.now();
  state.toastTimer = window.setTimeout(clearToast, milliseconds);
}

function showRatingToast(track) {
  clearToast();
  state.toastTrack = track;
  $("#toast-label").textContent = `Rate finished: ${track.creator || "Unknown uploader"} — ${track.title}`;
  $("#toast-rating").value = track.rating ?? "";
  $("#toast").hidden = false;
  startToastTimer(5000);
}

function closeDialog() {
  const dialog = $("#dialog");
  $("#dialog-content").innerHTML = '<h2 id="dialog-title">Dialog</h2>';
  dialog.hidden = true;
  dialog.setAttribute("aria-hidden", "true");
}

function openDialog(title, html) {
  const dialog = $("#dialog");
  const content = $("#dialog-content");
  // Populate the card before exposing the backdrop. This prevents a visible
  // empty modal if a render is interrupted or the page is restoring state.
  content.innerHTML = `<h2 id="dialog-title">${escapeHtml(title)}</h2>${html || "<p class='muted'>Nothing to show.</p>"}`;
  dialog.hidden = false;
  dialog.setAttribute("aria-hidden", "false");
  $("#dialog-close").focus();
}

function showDialogError(error) {
  openDialog("Could not load this view", `<p class="muted">${escapeHtml(error?.message || error)}</p>`);
}

async function showTrash() {
  const data = await api("/api/trash");
  const memberships = data.memberships.map((item) => `<div class="dialog-item">
    <strong>${escapeHtml(item.creator || "Unknown uploader")} — ${escapeHtml(item.title)}</strong>
    <p class="muted">Removed from ${escapeHtml(item.playlist_name)}</p>
    <button data-restore-membership="${item.membership_id}">Restore membership</button>
  </div>`).join("") || "<p class='muted'>No removed playlist entries.</p>";
  const hidden = data.hidden_tracks.map((item) => `<div class="dialog-item">
    <strong>${escapeHtml(item.creator || "Unknown uploader")} — ${escapeHtml(item.title)}</strong>
    <button data-unhide-track="${item.id}">Unhide track</button>
  </div>`).join("") || "<p class='muted'>No globally hidden tracks.</p>";
  openDialog("Trash & undo", `<h3>Removed memberships</h3><div class="dialog-list">${memberships}</div><h3>Hidden tracks</h3><div class="dialog-list">${hidden}</div>`);
}

async function showDuplicates() {
  const groups = await api("/api/possible-duplicates");
  const html = groups.map((group) => `<div class="dialog-item">
    <strong>${escapeHtml(group.key)}</strong>
    <ul>${group.tracks.map((track) => `<li>${escapeHtml(track.provider)} — <a href="${escapeHtml(track.url)}" target="_blank">${escapeHtml(track.creator || "Unknown uploader")} — ${escapeHtml(track.title)}</a></li>`).join("")}</ul>
  </div>`).join("") || "<p class='muted'>No cross-provider title/uploader matches found.</p>";
  openDialog("Possible cross-provider duplicates", `<p class="muted">These are only suggestions. Exact provider IDs are the only automatic duplicates.</p><div class="dialog-list">${html}</div>`);
}

async function showRelations(trackId, depth = 2) {
  const data = await api(`/api/tracks/${trackId}/relations?depth=${depth}&include_proposed=true`);
  const nodes = new Map((data.nodes || []).map((node) => [Number(node.id), node]));
  const root = nodes.get(Number(trackId));
  const edgeRows = (data.edges || []).map((edge) => {
    const from = nodes.get(Number(edge.from_track_id));
    const to = nodes.get(Number(edge.to_track_id));
    const evidence = edge.evidence || {};
    return `<div class="dialog-item relation-item">
      <strong>${escapeHtml(edge.relation_type)} · ${Math.round(Number(edge.confidence || 0) * 100)}%</strong>
      <p><a href="${escapeHtml(to?.url || from?.url || "#")}" target="_blank" rel="noopener">${escapeHtml(to?.title || from?.title || "Unknown track")}</a></p>
      <p class="muted">${escapeHtml(evidence.explanation || "Metadata evidence available")}; ${escapeHtml(edge.status || "proposed")}</p>
      <button data-relation-review="${edge.id}" data-relation-status="accepted">Accept</button>
      <button data-relation-review="${edge.id}" data-relation-status="rejected" class="secondary">Reject</button>
    </div>`;
  }).join("") || "<p class='muted'>No saved relations meet this threshold. Run Find related songs from Maintenance first.</p>";
  openDialog("Explore relations", `
    <p class="muted">${escapeHtml(root?.title || "Track")} · ${nodes.size - 1} related tracks · depth ${depth}</p>
    <div class="form-row"><label>Depth <select id="relation-depth"><option value="1" ${depth === 1 ? "selected" : ""}>1</option><option value="2" ${depth === 2 ? "selected" : ""}>2</option><option value="3" ${depth === 3 ? "selected" : ""}>3</option></select></label>
      <button id="relation-refresh" data-relation-root="${trackId}">Refresh</button>
      <button id="relation-playlist" data-relation-root="${trackId}">Create relation playlist</button></div>
    <div class="dialog-list relation-list">${edgeRows}</div>`);
}

async function showSources() {
  const data = await api("/api/sources");
  const rows = data.sources.map((source) => `<div class="dialog-item">
    <strong>${escapeHtml(source.target_playlist_name)} · ${escapeHtml(source.provider)}</strong>
    <p class="muted">${escapeHtml(source.url)}<br>Last import: ${escapeHtml(source.last_imported_at || "never")} · ${source.imported_count} memberships added total</p>
    <button data-refresh-source="${source.id}">Refresh this source</button>
    <button data-subscribe-source-url="${escapeHtml(source.url)}">Auto-sync this source</button>
  </div>`).join("") || "<p class='muted'>No saved imports yet.</p>";
  const runs = data.runs.slice(0, 20).map((run) => `<li>${escapeHtml(run.source_url)} — ${run.discovered_count} found, ${run.added_count} added${run.error ? ` · ${escapeHtml(run.error)}` : ""}</li>`).join("");
  openDialog("Sources & refresh", `<div class="dialog-list">${rows}</div><h3>Recent runs</h3><ul>${runs || "<li class='muted'>No runs yet.</li>"}</ul>`);
}

async function showSubscriptions(prefillUrl = "", prefillName = "") {
  const playlistOptions = state.playlists.map((playlist) =>
    `<option value="${playlist.id}">${escapeHtml(playlist.name)}</option>`).join("");
  const rows = state.subscriptions.map((subscription) => `
    <div class="dialog-item">
      <strong>${escapeHtml(subscription.name)}</strong>
      <p class="muted">${escapeHtml(subscription.url)}<br>
        ${subscription.enabled ? "Enabled" : "Paused"} · destination:
        ${escapeHtml(subscription.target_playlist_name)} · last sync:
        ${escapeHtml(subscription.last_sync_at || "never")}
        ${subscription.last_error ? `<br>Last error: ${escapeHtml(subscription.last_error)}` : ""}
      </p>
      <button data-sync-subscription="${subscription.id}">Sync now</button>
      <button data-toggle-subscription="${subscription.id}" data-enabled="${subscription.enabled ? "false" : "true"}">
        ${subscription.enabled ? "Pause" : "Resume"}
      </button>
      <button class="danger" data-delete-subscription="${subscription.id}">Remove subscription</button>
    </div>`).join("") || "<p class='muted'>No subscriptions yet.</p>";
  openDialog("Subscriptions", `
    <form id="subscription-form" class="dialog-form">
      <h3>Add channel or playlist subscription</h3>
      <label>Name (optional)<input name="name" value="${escapeHtml(prefillName)}" placeholder="Producer releases"></label>
      <label>Source URL<input name="url" required value="${escapeHtml(prefillUrl)}" placeholder="YouTube channel/playlist or Bilibili feed/series"></label>
      <label>Destination local playlist<select name="target_playlist_id" required>${playlistOptions}</select></label>
      <label>Minimum views (optional)<input name="min_views" placeholder="100k"></label>
      <label>Cookies from browser (optional)<input name="cookies_from_browser" placeholder="zen or chrome"></label>
      <label>Synchronization policy<select name="sync_policy"><option value="append_only">Append only</option><option value="mirror">Mirror membership</option><option value="mirror_preserve_local_removals">Mirror, preserve local removals</option></select></label>
      <label><input type="checkbox" name="refresh_views"> Refresh missing metadata</label>
      <button type="submit">Subscribe and sync now</button>
      <p class="muted">Mirroring only removes entries owned by this source; manual and unrelated playlist entries are never removed.</p>
    </form>
    <h3>Current subscriptions</h3>
    <div class="dialog-list">${rows}</div>`);
}

async function showHistory() {
  const history = await api("/api/history");
  const rows = history.items.map((item) => `<div class="dialog-item"><strong>${escapeHtml(item.creator || "Unknown uploader")} — ${escapeHtml(item.title)}</strong><p class="muted">${escapeHtml(item.reason)} · ${escapeHtml(item.played_at)}</p></div>`).join("") || "<p class='muted'>Nothing has finished or been skipped yet.</p>";
  openDialog("Playback history", `<div class="dialog-list">${rows}</div>`);
}

async function queueSelection(selection = currentSelection()) {
  const result = await post("/api/queue/build", {
    selection,
    mode: $("#shuffle-mode").value,
    cooldown: Number($("#creator-cooldown").value || 4),
  });
  renderQueue(result.queue);
  toast(result.extension_command_sent
    ? `Queued ${result.track_count} tracks and sent the player tab there.`
    : `Queued ${result.track_count} tracks. Pair the extension or use the direct links.`);
}

function connectWebSocket() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/ui`);
  socket.onmessage = async (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "import_started" || message.type === "job_started") {
      setImportProgress({ stage: "discovering" });
      toast("Importing…");
    } else if (message.type === "import_progress") {
      setImportProgress(message);
      toast(message.title ? `Importing: ${message.title}` : `Import: ${message.stage}…`);
    } else if (message.type === "import_finished" || message.type === "job_finished") {
      finishImportProgress(message);
    } else if (message.type === "import_failed" || message.type === "job_failed") {
      finishImportProgress(message, true);
    } else if (message.type === "playback_status") {
      $("#player-status").textContent = `${message.status}: ${message.detail || "manual Next remains available"}`;
    } else if (message.type === "player_state") {
      $("#player-status").textContent = `Player: ${message.status}`;
    } else if (message.type === "queue_finished") {
      $("#player-status").textContent = "Queue finished. Choose another pool or rebuild it.";
    }
    if (["connected", "library_changed", "import_finished", "job_finished", "job_failed", "exports_updated"].includes(message.type)) {
      try { await reload(); } catch (error) { console.error(error); }
    }
  };
  socket.onclose = () => window.setTimeout(connectWebSocket, 1200);
}

$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    let targetPlaylistId = Number($("#import-playlist").value);
    const name = $("#new-import-playlist").value.trim();
    if (name) {
      targetPlaylistId = (await post("/api/playlists", { name })).id;
      $("#new-import-playlist").value = "";
    }
    if (!targetPlaylistId) throw new Error("Choose or create a local playlist for this import.");
    const urls = $("#import-urls").value.split(/\r?\n/).map((url) => url.trim()).filter(Boolean);
    const job = await post("/api/imports", {
      urls,
      target_playlist_id: targetPlaylistId,
      min_views: parseViewCount($("#min-views").value),
      max_items: $("#max-items").value || null,
      cookies_from_browser: $("#cookies-browser").value || null,
      include_unknown_views: $("#include-unknown-views").checked,
      refresh_views: $("#refresh-views").checked,
      youtube_popular: $("#youtube-popular").checked,
      sync_policy: $("#sync-policy").value,
    });
    toast(`Import job ${job.id.slice(0, 8)} queued.`);
  } catch (error) {
    toast(error.message, true);
  }
});

$("#new-playlist-button").addEventListener("click", async () => {
  const name = window.prompt("Name for the local playlist:");
  if (!name) return;
  try { await post("/api/playlists", { name }); await reload(); } catch (error) { toast(error.message, true); }
});
$("#new-rating-playlist-button").addEventListener("click", async () => {
  const name = window.prompt("Name for the rating playlist:");
  if (!name) return;
  const min = window.prompt("Minimum rating from 0 to 10:", "8");
  if (min === null) return;
  const value = Number(min);
  if (!Number.isFinite(value) || value < 0 || value > 10) return toast("Rating must be between 0 and 10.", true);
  try { await post("/api/playlists", { name, kind: "smart", query: { min_rating: value } }); await reload(); } catch (error) { toast(error.message, true); }
});

$("#playlist-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-playlist-id]");
  if (!button) return;
  state.activePlaylistId = Number(button.dataset.playlistId);
  saveLibraryPreferences();
  state.selectedTrackIds.clear();
  await reload();
});

$("#creator-list").addEventListener("click", async (event) => {
  const subscribe = event.target.closest("[data-subscribe-creator-url]");
  if (subscribe) {
    showSubscriptions(subscribe.dataset.subscribeCreatorUrl, subscribe.dataset.subscribeCreatorName).catch(showDialogError);
    return;
  }
  const button = event.target.closest("[data-creator-name]");
  if (!button) return;
  state.activePlaylistId = null;
  state.selectedTrackIds.clear();
  $("#creator-library-filter").value = button.dataset.creatorName;
  saveLibraryPreferences();
  await reload();
});

$("#subscription-list").addEventListener("click", (event) => {
  if (event.target.closest("[data-subscription-name]")) showSubscriptions().catch(showDialogError);
});

$("#new-subscription-button").addEventListener("click", () => showSubscriptions().catch(showDialogError));

document.querySelector(".sidebar > section").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-playlist-id]");
  if (button && button.dataset.playlistId === "") {
    state.activePlaylistId = null;
    state.selectedTrackIds.clear();
    saveLibraryPreferences();
    await reload();
  }
});

$("#pool-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-pool-id]");
  const remove = event.target.closest("[data-delete-pool]");
  if (remove) {
    if (window.confirm("Delete this saved pool?")) {
      try { await api(`/api/pools/${remove.dataset.deletePool}`, { method: "DELETE" }); await reload({ tracks: false }); } catch (error) { toast(error.message, true); }
    }
    return;
  }
  if (!button) return;
  const pool = state.pools.find((entry) => entry.id === Number(button.dataset.poolId));
  if (pool) await queueSelection(pool.selection);
});

$("#save-pool-button").addEventListener("click", async () => {
  const name = window.prompt("Name for this saved pool:");
  if (!name) return;
  try { await post("/api/pools", { name, selection: currentSelection() }); await reload({ tracks: false }); } catch (error) { toast(error.message, true); }
});

$("#track-search").addEventListener("input", (() => {
  let timer;
  return () => { window.clearTimeout(timer); timer = window.setTimeout(() => loadTracks(), 200); };
})());
$("#provider-filter").addEventListener("change", () => loadTracks());
$("#rating-filter").addEventListener("change", () => loadTracks());
$("#track-sort").addEventListener("change", () => loadTracks());
$("#track-order").addEventListener("change", () => loadTracks());
$("#creator-library-filter").addEventListener("input", (() => {
  let timer;
  return () => { window.clearTimeout(timer); timer = window.setTimeout(() => loadTracks(), 200); };
})());
$("#load-more-button").addEventListener("click", () => loadTracks({ append: true }));
$("#clear-selection-button").addEventListener("click", () => { state.selectedTrackIds.clear(); renderTracks(); });
$("#selection-playlist")?.addEventListener("change", () => renderTracks());
$("#remove-selected-button")?.addEventListener("click", async () => {
  if (state.activePlaylistId === null || !state.selectedTrackIds.size) return;
  const count = state.selectedTrackIds.size;
  if (!window.confirm(`Delete ${count} selected track${count === 1 ? "" : "s"} from this playlist?`)) return;
  try {
    const result = await post(`/api/playlists/${state.activePlaylistId}/tracks/remove-selected`, {
      track_ids: [...state.selectedTrackIds],
    });
    state.selectedTrackIds.clear();
    await reload();
    toast(`Removed ${result.removed} playlist entr${result.removed === 1 ? "y" : "ies"}.`);
  } catch (error) { toast(error.message, true); }
});
$("#add-selected-button")?.addEventListener("click", async () => {
  const playlistId = Number($("#selection-playlist")?.value);
  if (!playlistId || !state.selectedTrackIds.size) return;
  try {
    const result = await post(`/api/playlists/${playlistId}/tracks/bulk`, {
      track_ids: [...state.selectedTrackIds],
    });
    state.selectedTrackIds.clear();
    await reload();
    $("#selection-playlist").value = String(playlistId);
    toast(`Added ${result.added} selected track${result.added === 1 ? "" : "s"}${result.existing ? ` · ${result.existing} already there` : ""}.`);
  } catch (error) { toast(error.message, true); }
});
$("#save-search-button")?.addEventListener("click", async () => {
  const name = window.prompt("Name for this saved search:");
  if (!name) return;
  try { await post("/api/searches", { name, query: currentLibraryQuery() }); await loadSavedSearches(); toast("Search saved."); } catch (error) { toast(error.message, true); }
});
$("#saved-search-select")?.addEventListener("change", async (event) => {
  const option = event.target.selectedOptions[0];
  if (!option?.dataset.query) return;
  try {
    const query = JSON.parse(option.dataset.query);
    const mapping = {
      q: "track-search", provider: "provider-filter", uploader: "creator-library-filter",
      artist: "artist-library-filter", album: "album-library-filter", genre: "genre-library-filter",
      availability: "availability-filter", min_rating: "rating-filter", min_duration: "min-duration-filter",
      max_duration: "max-duration-filter", min_views: "min-views-filter", max_views: "max-views-filter",
      has_override: "override-filter", sort: "track-sort", order: "track-order",
    };
    for (const id of Object.values(mapping)) document.getElementById(id).value = "";
    for (const [key, value] of Object.entries(query)) if (mapping[key]) document.getElementById(mapping[key]).value = value;
    saveLibraryPreferences(); await loadTracks();
  } catch (error) { toast(error.message, true); }
});

$("#select-page").addEventListener("change", (event) => {
  for (const track of state.tracks) {
    if (event.target.checked) state.selectedTrackIds.add(track.id);
    else state.selectedTrackIds.delete(track.id);
  }
  renderTracks();
});
$("#track-list").addEventListener("change", async (event) => {
  if (event.target.matches(".track-select")) {
    const id = Number(event.target.dataset.trackId);
    if (event.target.checked) state.selectedTrackIds.add(id); else state.selectedTrackIds.delete(id);
    renderTracks();
  }
  if (event.target.matches("[data-rating-id]")) {
    try { await saveRating(Number(event.target.dataset.ratingId), event.target.value); } catch (error) { toast(error.message, true); }
  }
});
$("#track-list").addEventListener("keydown", async (event) => {
  if (event.key === "Enter" && event.target.matches("[data-rating-id]")) {
    event.preventDefault();
    try { await saveRating(Number(event.target.dataset.ratingId), event.target.value); } catch (error) { toast(error.message, true); }
  }
});
$("#track-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  try {
    const id = Number(button.dataset.trackId);
    if (button.dataset.action === "play-track") await queueSelection({ track_ids: [id], include_repeats: false });
    if (button.dataset.action === "open-track") await post(`/api/tracks/${id}/open`);
    if (button.dataset.action === "copy-link") {
      const track = state.tracks.find((entry) => entry.id === id);
      if (!track) throw new Error("Track was not found in the current view.");
      await navigator.clipboard.writeText(track.url);
      toast("Track link copied.");
    }
    if (button.dataset.action === "relations") await showRelations(id);
    if (button.dataset.action === "hide-track") { await post(`/api/tracks/${id}/hidden`, { hidden: true }); await reload(); }
    if (button.dataset.action === "remove-membership") { await post(`/api/memberships/${button.dataset.membershipId}/remove`); await reload(); }
    if (button.dataset.action === "copy-membership") {
      if (state.activePlaylistId === null) throw new Error("Choose the playlist that should receive the duplicate copy.");
      await post(`/api/playlists/${state.activePlaylistId}/tracks`, { track_id: id, allow_duplicate: true });
      await reload();
    }
  } catch (error) { toast(error.message, true); }
});

$("#queue-selection-button").addEventListener("click", async () => {
  try { await queueSelection(); } catch (error) { toast(error.message, true); }
});
$("#play-button").addEventListener("click", async () => {
  try {
    const result = await post("/api/queue/play");
    toast(result.extension_command_sent
      ? "Sent current track to the Library Player."
      : result.opened_in_browser
        ? "Opened the current track in your browser."
        : "No paired extension is active and the browser could not be opened.", !result.extension_command_sent && !result.opened_in_browser);
  } catch (error) { toast(error.message, true); }
});
$("#pause-button")?.addEventListener("click", async () => {
  try { const result = await post("/api/queue/pause"); toast(result.extension_command_sent ? "Paused the player tab." : "Pause requires the paired extension."); } catch (error) { toast(error.message, true); }
});
$("#stop-button")?.addEventListener("click", async () => {
  try { const result = await post("/api/queue/stop"); toast(result.extension_command_sent ? "Stopped the player tab." : "Stop requires the paired extension."); } catch (error) { toast(error.message, true); }
});
$("#previous-button").addEventListener("click", async () => {
  try { const result = await post("/api/queue/previous"); renderQueue(result.queue); } catch (error) { toast(error.message, true); }
});
$("#next-button").addEventListener("click", async () => {
  try { const result = await post("/api/queue/next"); renderQueue(result.queue); } catch (error) { toast(error.message, true); }
});
$("#clear-queue-button").addEventListener("click", async () => {
  try { const result = await post("/api/queue/clear"); renderQueue(result.queue); toast("Queue cleared."); } catch (error) { toast(error.message, true); }
});
$("#now-rating").addEventListener("change", async (event) => {
  if (!state.queue?.current) return;
  try { await saveRating(state.queue.current.id, event.target.value); } catch (error) { toast(error.message, true); }
});
$("#now-rating").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter" || !state.queue?.current) return;
  event.preventDefault();
  try { await saveRating(state.queue.current.id, event.target.value); } catch (error) { toast(error.message, true); }
});

$("#merge-add-button").addEventListener("click", () => {
  const id = Number($("#merge-source").value);
  if (id && !state.mergeIds.includes(id)) state.mergeIds.push(id);
  renderMergeOrder();
});
$("#merge-order").addEventListener("click", (event) => {
  const move = event.target.closest("[data-merge-move]");
  const remove = event.target.closest("[data-merge-remove]");
  if (remove) state.mergeIds.splice(Number(remove.dataset.mergeRemove), 1);
  if (move) {
    const from = Number(move.dataset.mergeMove);
    const to = from + Number(move.dataset.direction);
    if (to >= 0 && to < state.mergeIds.length) [state.mergeIds[from], state.mergeIds[to]] = [state.mergeIds[to], state.mergeIds[from]];
  }
  renderMergeOrder();
});
$("#merge-order").addEventListener("dragstart", (event) => {
  const chip = event.target.closest("[data-merge-index]");
  if (chip) event.dataTransfer.setData("text/plain", chip.dataset.mergeIndex);
});
$("#merge-order").addEventListener("dragover", (event) => event.preventDefault());
$("#merge-order").addEventListener("drop", (event) => {
  event.preventDefault();
  const chip = event.target.closest("[data-merge-index]");
  const from = Number(event.dataTransfer.getData("text/plain"));
  const to = Number(chip?.dataset.mergeIndex);
  if (Number.isInteger(from) && Number.isInteger(to) && from !== to) {
    const [moved] = state.mergeIds.splice(from, 1);
    state.mergeIds.splice(to, 0, moved);
    renderMergeOrder();
  }
});
$("#merge-button").addEventListener("click", async () => {
  try {
    if (!state.mergeIds.length) throw new Error("Add at least one source playlist.");
    const merged = await post("/api/merge", {
      playlist_ids: state.mergeIds, name: $("#merge-name").value, archive_sources: $("#merge-archive").checked,
    });
    toast(`Created ${merged.name}.`);
    state.mergeIds = [];
    $("#merge-name").value = "";
    await reload();
  } catch (error) { toast(error.message, true); }
});
$("#playlist-set-button")?.addEventListener("click", async () => {
  try {
    if (!state.mergeIds.length) throw new Error("Add at least one source playlist.");
    const result = await post("/api/playlists/set-operation", {
      playlist_ids: state.mergeIds,
      operation: $("#playlist-set-operation").value,
      name: $("#merge-name").value || `Combined · ${$("#playlist-set-operation").value}`,
    });
    toast(`Created ${result.name}.`); state.mergeIds = []; await reload();
  } catch (error) { toast(error.message, true); }
});

$("#export-button").addEventListener("click", async () => {
  try { const exports = await post("/api/export"); toast(`Wrote ${exports.markdown} and ${exports.text}`); } catch (error) { toast(error.message, true); }
});
$("#duplicates-button").addEventListener("click", () => showDuplicates().catch(showDialogError));
$("#sources-button").addEventListener("click", () => showSources().catch(showDialogError));
$("#history-button").addEventListener("click", () => showHistory().catch(showDialogError));
$("#trash-button").addEventListener("click", () => showTrash().catch(showDialogError));
$("#dialog-close").addEventListener("click", closeDialog);
$("#dialog").addEventListener("click", async (event) => {
  if (event.target === $("#dialog")) {
    closeDialog();
    return;
  }
  const restore = event.target.closest("[data-restore-membership]");
  const unhide = event.target.closest("[data-unhide-track]");
  const refresh = event.target.closest("[data-refresh-source]");
  const sync = event.target.closest("[data-sync-subscription]");
  const toggle = event.target.closest("[data-toggle-subscription]");
  const removeSubscription = event.target.closest("[data-delete-subscription]");
  const subscribeSource = event.target.closest("[data-subscribe-source-url]");
  const relationReview = event.target.closest("[data-relation-review]");
  const relationRefresh = event.target.closest("#relation-refresh");
  const relationPlaylist = event.target.closest("#relation-playlist");
  try {
    if (restore) { await post(`/api/memberships/${restore.dataset.restoreMembership}/restore`); await showTrash(); await reload(); }
    if (unhide) { await post(`/api/tracks/${unhide.dataset.unhideTrack}/hidden`, { hidden: false }); await showTrash(); await reload(); }
    if (refresh) { const job = await post(`/api/sources/${refresh.dataset.refreshSource}/refresh`); toast(`Refresh job ${job.id.slice(0, 8)} queued.`); closeDialog(); }
    if (subscribeSource) { closeDialog(); showSubscriptions(subscribeSource.dataset.subscribeSourceUrl).catch(showDialogError); }
    if (relationReview) {
      await post(`/api/relationships/${relationReview.dataset.relationReview}/review`, { status: relationReview.dataset.relationStatus });
      const root = document.querySelector("#relation-refresh")?.dataset.relationRoot;
      if (root) await showRelations(Number(root), Number(document.querySelector("#relation-depth")?.value || 2));
    }
    if (relationRefresh) {
      await showRelations(Number(relationRefresh.dataset.relationRoot), Number(document.querySelector("#relation-depth")?.value || 2));
    }
    if (relationPlaylist) {
      const root = Number(relationPlaylist.dataset.relationRoot);
      const playlist = await post(`/api/tracks/${root}/relation-playlist`, {
        max_depth: Number(document.querySelector("#relation-depth")?.value || 2),
        include_proposed: false,
      });
      toast(`Created relation playlist: ${playlist.name}`);
      await reload({ tracks: false });
    }
    if (sync) {
      const job = await post(`/api/subscriptions/${sync.dataset.syncSubscription}/sync`);
      toast(job.id ? `Subscription sync job ${job.id.slice(0, 8)} queued.` : "Subscription sync is already running.");
      closeDialog();
    }
    if (toggle) {
      await post(`/api/subscriptions/${toggle.dataset.toggleSubscription}`, {
        enabled: toggle.dataset.enabled === "true",
      });
      await reload({ tracks: false });
      await showSubscriptions();
    }
    if (removeSubscription && window.confirm("Remove this subscription? Existing local tracks and playlist entries will remain.")) {
      await api(`/api/subscriptions/${removeSubscription.dataset.deleteSubscription}`, { method: "DELETE" });
      await reload({ tracks: false });
      await showSubscriptions();
    }
  } catch (error) { toast(error.message, true); }
});

$("#dialog").addEventListener("submit", async (event) => {
  if (event.target.id !== "subscription-form") return;
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    const result = await post("/api/subscriptions", {
      name: form.get("name"),
      url: form.get("url"),
      target_playlist_id: form.get("target_playlist_id"),
      min_views: form.get("min_views"),
      cookies_from_browser: form.get("cookies_from_browser"),
      refresh_views: form.has("refresh_views"),
      sync_policy: form.get("sync_policy"),
    });
    toast(result.job?.id ? `Subscription created; sync job ${result.job.id.slice(0, 8)} queued.` : "Subscription created.");
    closeDialog();
    await reload();
  } catch (error) { toast(error.message, true); }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#dialog").hidden) closeDialog();
});
$("#toast").addEventListener("mouseenter", () => {
  if (!state.toastTimer) return;
  window.clearTimeout(state.toastTimer);
  state.toastTimer = null;
  state.toastRemaining = Math.max(0, state.toastRemaining - (Date.now() - state.toastStarted));
});
$("#toast").addEventListener("mouseleave", () => { if (state.toastTrack) startToastTimer(state.toastRemaining); });
$("#toast-save-button").addEventListener("click", async () => {
  if (!state.toastTrack) return;
  try { await saveRating(state.toastTrack.id, $("#toast-rating").value); clearToast(); } catch (error) { toast(error.message, true); }
});
$("#toast-rating").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter" || !state.toastTrack) return;
  try { await saveRating(state.toastTrack.id, event.target.value); clearToast(); } catch (error) { toast(error.message, true); }
});

// Multi-view shell and operational panels.  The existing controls remain
// intact; views simply scope which panels are visible, so navigation never
// interrupts an active import or queue.
document.querySelectorAll(".view-link").forEach((link) => link.addEventListener("click", async () => {
  await applyView(link.dataset.view);
}));

for (const id of LIBRARY_PREF_CONTROLS) {
  const element = document.getElementById(id);
  element?.addEventListener("change", () => { saveLibraryPreferences(); updateFilterSummary(); });
  element?.addEventListener("input", () => { saveLibraryPreferences(); updateFilterSummary(); });
}
document.querySelector(".filter-drawer")?.addEventListener("toggle", saveLibraryPreferences);

async function loadBackups() {
  const list = await api("/api/backups");
  $("#backup-list").innerHTML = list.length
    ? list.map((backup) => `<div class="list-row"><strong>${escapeHtml(backup.id)}</strong><span class="muted">${escapeHtml(backup.manifest?.track_count ?? 0)} tracks · ${escapeHtml(backup.kind)}</span><button data-backup-action="validate" data-backup-id="${escapeHtml(backup.id)}">Validate</button><button data-backup-action="restore" data-backup-id="${escapeHtml(backup.id)}">Restore</button></div>`).join("")
    : "No backups yet.";
}
async function loadJobs() {
  const jobs = await api("/api/jobs?limit=100");
  const active = new Set(["queued", "running", "waiting_retry"]);
  const issues = new Set(["paused", "failed"]);
  const renderJob = (job, includePause = false) => `<div class="list-row"><strong>${escapeHtml(job.label || job.job_type)}</strong><span>${escapeHtml(job.status)} · ${Number(job.completed_count || 0)}/${Number(job.total_count || 0) || "?"}</span><span class="muted">${escapeHtml(job.error || "")}</span>${includePause ? `<button data-job-action="pause" data-job-id="${escapeHtml(job.id)}">Pause</button>` : ""}${issues.has(job.status) ? `<button data-job-action="resume" data-job-id="${escapeHtml(job.id)}">Resume</button>` : ""}</div>`;
  const currentJobs = jobs.filter((job) => active.has(job.status));
  const recentIssues = jobs.filter((job) => issues.has(job.status) && job.error);
  $("#jobs-list").innerHTML = currentJobs.length
    ? currentJobs.map((job) => renderJob(job, true)).join("")
    : "No jobs currently.";
  const issuesWrap = $("#jobs-issues-wrap");
  const issuesList = $("#jobs-issues");
  if (issuesWrap && issuesList) {
    issuesWrap.hidden = !recentIssues.length;
    issuesList.innerHTML = recentIssues.length
      ? recentIssues.map((job) => renderJob(job)).join("")
      : "";
  }
}
async function loadSettings() {
  const settings = await api("/api/settings");
  if (settings.update_schedule) $("#setting-schedule").value = settings.update_schedule;
  if (settings.max_requests_per_minute) $("#setting-rpm").value = settings.max_requests_per_minute;
  if (settings.request_delay) $("#setting-delay").value = settings.request_delay;
  if (settings.backup_retention_daily) $("#setting-backup-daily").value = settings.backup_retention_daily;
  if (settings.backup_retention_weekly) $("#setting-backup-weekly").value = settings.backup_retention_weekly;
  if (settings.backup_retention_monthly) $("#setting-backup-monthly").value = settings.backup_retention_monthly;
  if (settings.backup_retention_long_term) $("#setting-backup-long-term").value = settings.backup_retention_long_term;
}
async function runMaintenance() {
  const tracks = await api("/api/tracks?limit=2000");
  const missingUploader = tracks.items.filter((track) => !(track.uploader || track.creator)).length;
  const missingArtist = tracks.items.filter((track) => !track.artist).length;
  const duplicateGroups = await api("/api/possible-duplicates");
  $("#maintenance-report").innerHTML = `<div>Tracks checked: ${tracks.total}</div><div>Missing uploader: ${missingUploader}</div><div>Missing artist: ${missingArtist}</div><div>Possible cross-provider duplicates: ${Array.isArray(duplicateGroups) ? duplicateGroups.length : 0}</div>`;
}
async function loadReviewInbox() {
  const items = await api("/api/review?status_filter=open");
  $("#review-list").innerHTML = items.length
    ? items.map((item) => `<div class="list-row"><strong>${escapeHtml(item.kind)}</strong><span>${escapeHtml(item.payload?.relation_type || item.title || "Suggestion")}</span><button data-resolve-review="${item.id}">Dismiss</button></div>`).join("")
    : '<span class="muted">Review inbox is clear.</span>';
}

function renderBulkOperations() {
  const list = $("#bulk-operation-list");
  if (!list) return;
  list.innerHTML = state.bulkOperations.length
    ? state.bulkOperations.map((operation, index) => `
      <div class="list-row">
        <code>${escapeHtml(operation.type)}</code>
        <span>${escapeHtml(operation.field || (operation.fields || []).join(", "))}</span>
        <span class="muted">${escapeHtml(JSON.stringify(operation))}</span>
        <button type="button" data-remove-bulk-operation="${index}">Remove</button>
      </div>`).join("")
    : "No operations added.";
  $("#bulk-apply-button").disabled = true;
  state.bulkPreviewRequest = null;
}

function buildBulkOperation() {
  const type = $("#bulk-operation-type").value;
  const field = $("#bulk-field").value;
  const value = $("#bulk-value").value;
  const replacement = $("#bulk-replacement").value;
  if (type === "clear") return { type, field };
  if (type === "provider_refresh") {
    return { type, field, enabled: $("#bulk-refresh-enabled").checked };
  }
  if (type === "find_replace") return { type, field, find: value, replacement };
  if (type === "regex_replace") {
    if (!value) throw new Error("Enter a regular-expression pattern.");
    return { type, field, pattern: value, replacement, ignore_case: $("#bulk-ignore-case").checked };
  }
  if (type === "exact_normalize") return { type, field, match: value, replacement };
  if (["tags_add", "tags_remove", "tags_replace"].includes(type)) {
    if (!["tags", "aliases"].includes(field)) throw new Error("Tag operations require the Tags or Aliases field.");
    return { type, field, values: value.split(",").map((item) => item.trim()).filter(Boolean) };
  }
  if (type === "copy_from_track") {
    const sourceTrackId = Number($("#bulk-source-track").value);
    if (!sourceTrackId) throw new Error("Enter the source track ID.");
    return { type, source_track_id: sourceTrackId, fields: [field] };
  }
  return { type: "set", field, value };
}

$("#bulk-add-operation")?.addEventListener("click", () => {
  try {
    state.bulkOperations.push(buildBulkOperation());
    renderBulkOperations();
  } catch (error) { toast(error.message, true); }
});
$("#bulk-operation-list")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-bulk-operation]");
  if (!button) return;
  state.bulkOperations.splice(Number(button.dataset.removeBulkOperation), 1);
  renderBulkOperations();
});
$("#bulk-preview-button")?.addEventListener("click", async () => {
  try {
    if (!state.selectedTrackIds.size) throw new Error("Select at least one track.");
    if (!state.bulkOperations.length) throw new Error("Add at least one operation.");
    const request = {
      track_ids: [...state.selectedTrackIds],
      operations: state.bulkOperations.map((operation) => ({ ...operation })),
    };
    const preview = await post("/api/tracks/bulk-edit/preview", request);
    state.bulkPreviewRequest = request;
    $("#bulk-preview").innerHTML = preview.items.length
      ? preview.items.slice(0, 100).map((item) => `
        <div class="list-row">
          <strong>${escapeHtml(item.title || `Track ${item.track_id}`)}</strong>
          <span class="muted">${escapeHtml(JSON.stringify(item.before))} → ${escapeHtml(JSON.stringify(item.after))}</span>
        </div>`).join("") + (preview.items.length > 100 ? `<div>…and ${preview.items.length - 100} more</div>` : "")
      : "No values would change.";
    $("#bulk-apply-button").disabled = preview.count === 0;
  } catch (error) { toast(error.message, true); }
});
$("#bulk-apply-button")?.addEventListener("click", async () => {
  try {
    if (!state.bulkPreviewRequest) throw new Error("Preview the operation first.");
    if (!window.confirm("Apply this previewed bulk metadata edit?")) return;
    const result = await post("/api/tracks/bulk-edit", state.bulkPreviewRequest);
    state.lastBulkOperationId = result.operation_id;
    $("#bulk-undo-button").disabled = !result.operation_id;
    $("#bulk-apply-button").disabled = true;
    toast(`Updated ${result.count} track${result.count === 1 ? "" : "s"}.`);
    await reload();
  } catch (error) { toast(error.message, true); }
});
$("#bulk-undo-button")?.addEventListener("click", async () => {
  if (!state.lastBulkOperationId) return;
  try {
    const result = await post("/api/metadata/undo", { operation_id: state.lastBulkOperationId });
    state.lastBulkOperationId = null;
    $("#bulk-undo-button").disabled = true;
    toast(`Undid ${result.undone} metadata change${result.undone === 1 ? "" : "s"}.`);
    await reload();
  } catch (error) { toast(error.message, true); }
});
$("#create-backup-button")?.addEventListener("click", async () => {
  try { await post("/api/backups", { kind: "manual" }); await loadBackups(); toast("Backup created."); } catch (error) { toast(error.message, true); }
});
$("#prune-backups-button")?.addEventListener("click", async () => {
  try {
    const result = await post("/api/backups/prune", {});
    await loadBackups();
    toast(`Retention removed ${result.removed} backup${result.removed === 1 ? "" : "s"}.`);
  } catch (error) { toast(error.message, true); }
});
$("#rollback-restore-button")?.addEventListener("click", async () => {
  try {
    if (!window.confirm("Rollback the most recent restore? This consumes the one pre-restore rollback copy.")) return;
    await post("/api/backups/rollback-last-restore", {});
    toast("The most recent restore was rolled back.");
    await reload();
    await loadBackups();
  } catch (error) { toast(error.message, true); }
});
$("#jobs-refresh-button")?.addEventListener("click", () => loadJobs().catch((error) => toast(error.message, true)));
$("#jobs-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-job-action]");
  if (!button) return;
  try {
    const path = button.dataset.jobAction === "pause" ? "pause" : "resume";
    await post(`/api/jobs/${encodeURIComponent(button.dataset.jobId)}/${path}`);
    await loadJobs();
  } catch (error) { toast(error.message, true); }
});
$("#backup-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-backup-id]");
  if (!button) return;
  try {
    const id = encodeURIComponent(button.dataset.backupId);
    const validation = await post(`/api/backups/${id}/validate`);
    if (!validation.valid) throw new Error(validation.error || "Backup is invalid.");
    if (button.dataset.backupAction === "restore") {
      if (!window.confirm("Restore this validated backup? The current library will be kept as one pre-restore rollback copy.")) return;
      const restored = await post(`/api/backups/${id}/restore`, {});
      toast(`Backup restored. Rollback: ${restored.rollback_path || "not needed"}`);
      await reload();
      await loadBackups();
    } else {
      toast("Backup is valid.");
    }
  } catch (error) { toast(error.message, true); }
});
$("#save-settings-button")?.addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({
        update_schedule: $("#setting-schedule").value,
        max_requests_per_minute: $("#setting-rpm").value,
        request_delay: $("#setting-delay").value,
        backup_retention_daily: $("#setting-backup-daily").value,
        backup_retention_weekly: $("#setting-backup-weekly").value,
        backup_retention_monthly: $("#setting-backup-monthly").value,
        backup_retention_long_term: $("#setting-backup-long-term").value,
      }),
    });
    toast("Settings saved.");
  } catch (error) { toast(error.message, true); }
});
$("#rotate-pairing-token-button")?.addEventListener("click", async () => {
  if (!window.confirm("Rotate the pairing token? Existing browser extensions will need to pair again.")) return;
  try {
    const result = await post("/api/settings/rotate-pairing-token", {});
    $("#pairing-token").value = result.pairing_token;
    toast("Pairing token rotated. Pair the browser extension again.");
  } catch (error) { toast(error.message, true); }
});
$("#relationship-scan-button")?.addEventListener("click", async () => {
  try { const result = await post("/api/relationships/scan", { limit: 500 }); toast(`Relation scan job ${result.id.slice(0, 8)} queued.`); } catch (error) { toast(error.message, true); }
});
$("#fingerprint-local-button")?.addEventListener("click", async () => {
  try {
    const trackId = Number($("#fingerprint-track-id").value);
    const path = $("#fingerprint-local-path").value.trim();
    if (!trackId || !path) throw new Error("Enter a track ID and explicit local audio path.");
    await post(`/api/tracks/${trackId}/fingerprints/from-local-file`, { path });
    toast("Local fingerprint saved. Run related-song detection to compare it.");
  } catch (error) { toast(error.message, true); }
});
$("#review-refresh-button")?.addEventListener("click", () => loadReviewInbox().catch((error) => toast(error.message, true)));
$("#review-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-resolve-review]");
  if (!button) return;
  try { await post(`/api/review/${button.dataset.resolveReview}/resolve`, { status: "resolved" }); await loadReviewInbox(); } catch (error) { toast(error.message, true); }
});
let discoverSessionId = null;
$("#discover-start-button")?.addEventListener("click", async () => {
  try {
    const sources = $("#discover-sources").value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
    const session = await post("/api/discover", { sources, page_size: Number($("#discover-page-size").value || 100) });
    discoverSessionId = session.id; $("#discover-status").textContent = `Discover session ${session.name} · ${session.candidates.length} candidates`;
  } catch (error) { toast(error.message, true); }
});
$("#discover-refresh-button")?.addEventListener("click", async () => {
  if (!discoverSessionId) return;
  try { const session = await post(`/api/discover/${discoverSessionId}/refresh`, { candidates: [], seen: [] }); $("#discover-status").textContent = `Mix refreshed · ${session.candidates.length} candidates`; } catch (error) { toast(error.message, true); }
});

const restoredView = restoreLibraryPreferences();
applyView(restoredView).catch((error) => console.error(error));
reload().catch((error) => toast(`Could not load library: ${error.message}`, true));
connectWebSocket();
