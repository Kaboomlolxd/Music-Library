/* Observe real provider HTML5 video playback.  This never injects a player,
 * captures audio, bypasses ads/privacy tools, or opens a browser tab. */
(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  let route = location.href;
  let observedVideo = null;
  let lastEndedRoute = "";
  let sentUnknownDuration = false;
  let pageScanTimer = null;
  let pageScanInFlight = false;
  let pageScanQueued = false;
  let widgetPositionLoaded = false;
  let widgetDrag = null;
  const requestedPageUrls = new Set();
  const pageStatuses = new Map();

  const provider = () => location.hostname.includes("bilibili") || location.hostname === "b23.tv"
    ? "bilibili" : "youtube";
  const send = (type, extra = {}) => {
    try {
      const result = api.runtime.sendMessage({
        type: "library-player-event",
        provider: provider(),
        url: location.href,
        ...extra,
      });
      if (result && typeof result.catch === "function") result.catch(() => {});
    } catch (_) {
      // The extension can be reloaded while a provider tab remains open.
    }
  };

  function message(type, extra = {}) {
    try {
      const result = api.runtime.sendMessage({ type, ...extra });
      return result && typeof result.then === "function" ? result : Promise.resolve(null);
    } catch (_) {
      return Promise.reject(new Error("Extension messaging is unavailable"));
    }
  }

  function canonicalProviderUrl(rawUrl) {
    try {
      const url = new URL(rawUrl, location.href);
      const host = url.hostname.toLowerCase();
      if (host === "youtu.be") {
        const id = url.pathname.split("/").filter(Boolean)[0];
        return id ? `https://www.youtube.com/watch?v=${encodeURIComponent(id)}` : null;
      }
      if (host.endsWith("youtube.com")) {
        const watchId = url.searchParams.get("v");
        const pathMatch = url.pathname.match(/^\/(?:shorts|live|embed)\/([^/?#]+)/i);
        const id = watchId || pathMatch?.[1];
        return id ? `https://www.youtube.com/watch?v=${encodeURIComponent(id)}` : null;
      }
      if (host.endsWith("bilibili.com") || host === "b23.tv") {
        const match = url.pathname.match(/\/video\/((?:BV[0-9A-Za-z]+)|(?:av\d+))/i);
        return match ? `https://www.bilibili.com/video/${match[1]}` : null;
      }
    } catch (_) {
      // Ignore malformed or non-provider links during a page scan.
    }
    return null;
  }

  function pageVideoUrl() {
    return canonicalProviderUrl(location.href);
  }

  function visible(element) {
    if (!(element instanceof Element) || element.closest("#local-music-library-saved-widget")) return false;
    if (element.getAttribute("aria-hidden") === "true") return false;
    const rects = element.getClientRects();
    return rects.length > 0 && rects[0].width > 0 && rects[0].height > 0;
  }

  function pageTitle() {
    const meta = document.querySelector('meta[property="og:title"], meta[name="title"]');
    const heading = document.querySelector("h1.ytd-watch-metadata, h1.video-title, h1");
    const title = meta?.content || heading?.textContent || document.title;
    return String(title || "").replace(/\s+/g, " ").trim()
      .replace(/\s*-\s*YouTube\s*$/i, "")
      .replace(/\s*-\s*bilibili\s*$/i, "")
      .trim();
  }

  function pageCreator() {
    const creatorLink = document.querySelector(
      "#owner #channel-name a, ytd-video-owner-renderer #channel-name a, " +
      ".up-name, .up-detail .username, a[href*='/space/']",
    );
    const authorMeta = document.querySelector('meta[itemprop="author"], meta[name="author"]');
    const creator = creatorLink?.textContent || authorMeta?.content;
    return String(creator || "").replace(/\s+/g, " ").trim() || null;
  }

  function pageCreatorUrl() {
    const creatorLink = document.querySelector(
      "#owner #channel-name a, ytd-video-owner-renderer #channel-name a, " +
      "a.up-name, .up-detail a[href*='/space/']",
    );
    try {
      return creatorLink?.href ? new URL(creatorLink.href, location.href).href : null;
    } catch (_) {
      return null;
    }
  }

  function pageViewCount() {
    const value = document.querySelector(
      'meta[itemprop="interactionCount"], meta[itemprop="userInteractionCount"], ' +
      "#info #count, .view, .view-text, .video-data",
    )?.content || document.querySelector(
      "#info #count, .view, .view-text, .video-data",
    )?.textContent;
    const match = String(value || "").replace(/,/g, "").match(/(\d+(?:\.\d+)?)\s*([万亿mk])?/i);
    if (!match) return null;
    const multiplier = { 万: 10000, 亿: 100000000, k: 1000, m: 1000000 }[String(match[2] || "").toLowerCase()] || 1;
    return Math.round(Number(match[1]) * multiplier);
  }

  function pageDuration() {
    const video = bestVideo();
    return video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
  }

  function button(text, onClick) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = text;
    element.style.cssText = "display:block;margin-top:7px;padding:4px 7px;cursor:pointer;pointer-events:auto";
    element.addEventListener("click", onClick);
    return element;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
  }

  function applyWidgetPosition(widget, position) {
    if (!position || !Number.isFinite(Number(position.left)) || !Number.isFinite(Number(position.top))) return;
    const bounds = widget.getBoundingClientRect();
    const left = clamp(Number(position.left), 0, window.innerWidth - bounds.width);
    const top = clamp(Number(position.top), 0, window.innerHeight - bounds.height);
    widget.style.left = `${left}px`;
    widget.style.top = `${top}px`;
    widget.style.right = "auto";
    widget.style.bottom = "auto";
  }

  function makeWidgetDraggable(widget, handle) {
    handle.style.cssText = "display:block;cursor:move;pointer-events:auto;user-select:none";
    handle.title = "Drag to move this library widget";
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 && event.pointerType !== "touch") return;
      const bounds = widget.getBoundingClientRect();
      widgetDrag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: bounds.left,
        top: bounds.top,
      };
      try { handle.setPointerCapture(event.pointerId); } catch (_) {}
      event.preventDefault();
    });
    handle.addEventListener("pointermove", (event) => {
      if (!widgetDrag || event.pointerId !== widgetDrag.pointerId) return;
      const bounds = widget.getBoundingClientRect();
      applyWidgetPosition(widget, {
        left: widgetDrag.left + event.clientX - widgetDrag.startX,
        top: widgetDrag.top + event.clientY - widgetDrag.startY,
      });
      // Re-read dimensions after the first move so the clamp remains correct
      // if the provider changes zoom or the card content is reflowed.
      if (bounds.width !== widget.getBoundingClientRect().width) {
        applyWidgetPosition(widget, {
          left: parseFloat(widget.style.left),
          top: parseFloat(widget.style.top),
        });
      }
      event.preventDefault();
    });
    const finishDrag = (event) => {
      if (!widgetDrag || event.pointerId !== widgetDrag.pointerId) return;
      const left = parseFloat(widget.style.left);
      const top = parseFloat(widget.style.top);
      widgetDrag = null;
      if (Number.isFinite(left) && Number.isFinite(top)) {
        message("widget-position-set", { left, top }).catch(() => {});
      }
      try { handle.releasePointerCapture(event.pointerId); } catch (_) {}
    };
    handle.addEventListener("pointerup", finishDrag);
    handle.addEventListener("pointercancel", finishDrag);
  }

  function restoreWidgetPosition(widget) {
    if (widgetPositionLoaded) return;
    widgetPositionLoaded = true;
    message("widget-position-get")
      .then((position) => applyWidgetPosition(widget, position))
      .catch(() => {});
  }

  function renderSavedWidget(status) {
    let widget = document.querySelector("#local-music-library-saved-widget");
    if (!widget) {
      widget = document.createElement("aside");
      widget.id = "local-music-library-saved-widget";
      widget.style.cssText = [
        "position:fixed", "z-index:2147483647", "top:80px", "right:16px",
        "max-width:280px", "padding:10px 12px", "border:1px solid #536da8",
        "border-radius:10px", "background:rgba(13,20,39,.96)", "color:#edf2ff",
        "font:13px system-ui,sans-serif", "box-shadow:0 8px 24px rgba(0,0,0,.35)",
        "pointer-events:none",
      ].join(";");
      document.documentElement.appendChild(widget);
      restoreWidgetPosition(widget);
    }
    widget.replaceChildren();
    const heading = document.createElement("strong");
    makeWidgetDraggable(widget, heading);
    const detail = document.createElement("div");
    const openButton = button("Open library", () => {
      try { api.runtime.sendMessage({ type: "open-library-dashboard" }); } catch (_) {}
    });
    if (!status?.supported) {
      heading.textContent = status?.reason === "not_paired"
        ? "Pair the extension in Options"
        : "Open a video to check saved status";
      widget.append(heading);
    } else if (!status.saved) {
      heading.textContent = "Not saved in Local Music Library";
      const videoUrl = pageVideoUrl() || location.href;
      const saveButton = button("Save to library", async (event) => {
        const target = event.currentTarget;
        target.disabled = true;
        target.textContent = "Saving…";
        try {
          const saved = await message("provider-save-track", {
            url: videoUrl,
            title: pageTitle(),
            creator: pageCreator(),
            view_count: pageViewCount(),
            duration: pageDuration(),
            creator_url: pageCreatorUrl(),
          });
          if (!saved?.saved) throw new Error(saved?.error || "The local library rejected this video");
          requestedPageUrls.delete(videoUrl);
          renderSavedWidget(saved);
          schedulePageScan();
        } catch (error) {
          target.disabled = false;
          target.textContent = "Save to library";
          detail.textContent = `Save failed: ${error?.message || error}`;
          detail.style.cssText = "margin-top:4px;line-height:1.35;color:#ffb4ab";
          widget.append(detail);
        }
      });
      widget.append(heading, saveButton, openButton);
    } else {
      const rating = status.rating == null ? "not rated" : `${status.rating}/10`;
      heading.textContent = "Saved in Local Music Library";
      detail.textContent = `${status.title || ""}\n${status.creator || "Unknown uploader"} · ${rating}`;
      detail.style.cssText = "margin-top:4px;line-height:1.35;white-space:pre-line";
      const ratingRow = document.createElement("div");
      ratingRow.style.cssText = "display:flex;gap:5px;align-items:center;margin-top:7px";
      const ratingInput = document.createElement("input");
      ratingInput.type = "number";
      ratingInput.min = "0";
      ratingInput.max = "10";
      ratingInput.step = "0.1";
      ratingInput.inputMode = "decimal";
      ratingInput.placeholder = "0–10";
      ratingInput.title = "Rate from 0 to 10; leave blank to clear";
      ratingInput.value = status.rating ?? "";
      ratingInput.style.cssText = "width:66px;padding:4px;pointer-events:auto";
      const ratingButton = document.createElement("button");
      ratingButton.type = "button";
      ratingButton.textContent = "Save rating";
      ratingButton.style.cssText = "padding:4px 7px;cursor:pointer;pointer-events:auto";
      const saveRating = async () => {
        const raw = ratingInput.value.trim();
        const value = raw === "" ? null : Number(raw);
        if (value !== null && (!Number.isFinite(value) || value < 0 || value > 10)) {
          detail.textContent = "Rating must be between 0 and 10.";
          detail.style.cssText = "margin-top:4px;line-height:1.35;color:#ffb4ab";
          return;
        }
        ratingButton.disabled = true;
        try {
          const updated = await message("provider-rate-track", {
            track_id: status.id,
            value,
          });
          if (!updated?.id) throw new Error(updated?.error || "The local library rejected this rating");
          renderSavedWidget({ ...status, ...updated, saved: true });
        } catch (error) {
          ratingButton.disabled = false;
          detail.textContent = `Rating failed: ${error?.message || error}`;
          detail.style.cssText = "margin-top:4px;line-height:1.35;color:#ffb4ab";
        }
      };
      ratingButton.addEventListener("click", saveRating);
      ratingInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") saveRating();
      });
      ratingRow.append(ratingInput, ratingButton);
      widget.append(heading, detail, ratingRow, openButton);
    }
  }

  async function checkSavedStatus() {
    const url = pageVideoUrl();
    if (!url) {
      document.querySelector("#local-music-library-saved-widget")?.remove();
      schedulePageScan();
      return;
    }
    try {
      const status = await message("provider-page-seen", { url });
      if (pageVideoUrl() !== url) return;
      renderSavedWidget(status);
    } catch (_) {
      // The local app may be stopped; playback monitoring should continue.
    }
  }

  const cardSelectors = [
    "ytd-rich-item-renderer",
    "ytd-video-renderer",
    "ytd-grid-video-renderer",
    "ytd-compact-video-renderer",
    "ytd-playlist-video-renderer",
    "ytd-reel-item-renderer",
    "yt-lockup-view-model",
    ".bili-video-card",
    ".bili-video-card__wrap",
    ".video-page-card-small",
    ".video-card",
    ".small-item",
    ".rank-item",
    ".bili-dyn-card-video",
    ".feed-card",
    ".video-list-item",
  ].join(",");

  const thumbnailSelectors = [
    "ytd-thumbnail",
    "a#thumbnail",
    "#thumbnail",
    ".bili-video-card__image",
    ".bili-video-card__image--wrap",
    ".video-card__image",
    ".b-img",
    ".pic",
    ".cover",
  ].join(",");

  function savedTooltip(status) {
    const lines = ["Already saved in Local Music Library"];
    if (status?.playlists) lines.push(`Playlist: ${status.playlists}`);
    if (status?.rating != null) lines.push(`Rating: ${status.rating}/10`);
    return lines.join("\n");
  }

  function restoreInlineStyle(element, property, datasetKey) {
    if (!(element instanceof HTMLElement) || !(datasetKey in element.dataset)) return;
    const original = element.dataset[datasetKey];
    if (original === "__empty__") element.style.removeProperty(property);
    else element.style.setProperty(property, original);
    delete element.dataset[datasetKey];
  }

  function clearCardDecoration(card) {
    if (!(card instanceof HTMLElement)) return;
    card.querySelectorAll("[data-library-saved-card-badge]").forEach((item) => item.remove());
    card.querySelectorAll("[data-library-marker-for-url]").forEach((item) => item.remove());
    restoreInlineStyle(card, "outline", "libraryOriginalOutline");
    restoreInlineStyle(card, "outline-offset", "libraryOriginalOutlineOffset");
    for (const thumbnail of card.querySelectorAll("[data-library-saved-positioned]")) {
      restoreInlineStyle(thumbnail, "position", "libraryOriginalPosition");
      delete thumbnail.dataset.librarySavedPositioned;
    }
    delete card.dataset.librarySavedForUrl;
  }

  function cardFor(anchor) {
    return anchor.closest(cardSelectors);
  }

  function thumbnailFor(card) {
    if (!(card instanceof Element)) return null;
    const candidates = [...card.querySelectorAll(thumbnailSelectors)];
    return candidates.find((candidate) =>
      candidate.querySelector("img, picture") || candidate.matches("img, picture")
    ) || null;
  }

  function markerFor(anchor, url, status) {
    const existing = [...(anchor.parentElement?.querySelectorAll("[data-library-marker-for-url]") || [])]
      .find((item) => item.dataset.libraryMarkerForUrl === url);
    if (existing) {
      existing.title = savedTooltip(status);
      return;
    }
    const marker = document.createElement("span");
    marker.dataset.libraryMarkerForUrl = url;
    marker.textContent = "★ Saved";
    marker.title = savedTooltip(status);
    marker.setAttribute("aria-label", "Already saved in Local Music Library");
    marker.style.cssText = [
      "display:inline-flex", "align-items:center", "margin-left:5px", "padding:2px 6px",
      "border:1px solid rgba(255,255,255,.28)", "border-radius:999px",
      "background:#087448", "color:#fff", "font:700 11px/1.2 system-ui,sans-serif",
      "box-shadow:0 1px 4px rgba(0,0,0,.35)", "vertical-align:middle",
      "pointer-events:none", "white-space:nowrap",
    ].join(";");
    anchor.insertAdjacentElement("afterend", marker);
  }

  function decorateSavedCard(anchor, url, status) {
    const card = cardFor(anchor);
    if (!(card instanceof HTMLElement)) {
      markerFor(anchor, url, status);
      return;
    }
    if (card.dataset.librarySavedForUrl && card.dataset.librarySavedForUrl !== url) {
      clearCardDecoration(card);
    }
    card.dataset.librarySavedForUrl = url;
    if (!("libraryOriginalOutline" in card.dataset)) {
      card.dataset.libraryOriginalOutline = card.style.getPropertyValue("outline") || "__empty__";
      card.dataset.libraryOriginalOutlineOffset = card.style.getPropertyValue("outline-offset") || "__empty__";
    }
    card.style.setProperty("outline", "2px solid rgba(24, 184, 112, .72)");
    card.style.setProperty("outline-offset", "-2px");

    const thumbnail = thumbnailFor(card);
    if (!thumbnail) {
      markerFor(anchor, url, status);
      return;
    }
    let badge = [...thumbnail.querySelectorAll("[data-library-saved-card-badge]")]
      .find((item) => item.dataset.librarySavedCardBadge === url);
    if (!badge) {
      badge = document.createElement("span");
      badge.dataset.librarySavedCardBadge = url;
      badge.textContent = "★ Saved";
      badge.setAttribute("aria-label", "Already saved in Local Music Library");
      badge.style.cssText = [
        "position:absolute", "z-index:2147483646", "top:7px", "left:7px",
        "display:inline-flex", "align-items:center", "padding:4px 8px",
        "border:1px solid rgba(255,255,255,.35)", "border-radius:999px",
        "background:rgba(5,105,63,.96)", "color:#fff",
        "font:700 11px/1.15 system-ui,sans-serif", "letter-spacing:.02em",
        "box-shadow:0 2px 7px rgba(0,0,0,.48)", "pointer-events:none",
        "white-space:nowrap",
      ].join(";");
      if (getComputedStyle(thumbnail).position === "static") {
        thumbnail.dataset.libraryOriginalPosition =
          thumbnail.style.getPropertyValue("position") || "__empty__";
        thumbnail.dataset.librarySavedPositioned = "true";
        thumbnail.style.setProperty("position", "relative");
      }
      thumbnail.appendChild(badge);
    }
    badge.title = savedTooltip(status);
  }

  function removeSavedDecoration(anchor, url) {
    const card = cardFor(anchor);
    if (card instanceof HTMLElement && card.dataset.librarySavedForUrl === url) {
      clearCardDecoration(card);
    }
    for (const marker of anchor.parentElement?.querySelectorAll("[data-library-marker-for-url]") || []) {
      if (marker.dataset.libraryMarkerForUrl === url) marker.remove();
    }
  }

  function applyPageStatusMarkers(statuses) {
    for (const anchor of document.querySelectorAll("a[href]")) {
      if (!visible(anchor)) continue;
      const url = canonicalProviderUrl(anchor.href);
      if (!url || !statuses.has(url)) continue;
      const status = statuses.get(url);
      if (status?.saved) decorateSavedCard(anchor, url, status);
      else removeSavedDecoration(anchor, url);
    }
  }

  async function scanPageLinks() {
    applyPageStatusMarkers(pageStatuses);
    const urls = [];
    const seen = new Set();
    for (const anchor of document.querySelectorAll("a[href]")) {
      if (urls.length >= 250 || !visible(anchor)) continue;
      const url = canonicalProviderUrl(anchor.href);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      if (!requestedPageUrls.has(url)) urls.push(url);
    }
    if (!urls.length) return;
    urls.forEach((url) => requestedPageUrls.add(url));
    pageScanInFlight = true;
    try {
      for (let start = 0; start < urls.length; start += 50) {
        const batch = urls.slice(start, start + 50);
        const result = await message("provider-pages-seen", { urls: batch });
        if (result?.error) throw new Error(result.error);
        const statuses = new Map(Object.entries(result || {}));
        for (const [url, status] of statuses) pageStatuses.set(url, status);
        applyPageStatusMarkers(statuses);
      }
    } catch (_) {
      urls.forEach((url) => requestedPageUrls.delete(url));
    } finally {
      pageScanInFlight = false;
      if (pageScanQueued) {
        pageScanQueued = false;
        schedulePageScan();
      }
    }
  }

  function schedulePageScan() {
    if (pageScanInFlight) {
      pageScanQueued = true;
      return;
    }
    if (pageScanTimer) return;
    pageScanTimer = window.setTimeout(() => {
      pageScanTimer = null;
      scanPageLinks();
    }, 250);
  }

  function bestVideo() {
    const videos = [...document.querySelectorAll("video")];
    return videos.sort((left, right) =>
      ((right.videoWidth || right.clientWidth || 0) * (right.videoHeight || right.clientHeight || 0)) -
      ((left.videoWidth || left.clientWidth || 0) * (left.videoHeight || left.clientHeight || 0))
    )[0] || null;
  }

  function hasFiniteDuration(video) {
    return Number.isFinite(video.duration) && video.duration > 0;
  }

  function adState() {
    if (provider() !== "youtube") return "none";
    if (document.querySelector(".html5-video-player.ad-showing, .html5-video-player.ad-interrupting")) return "youtube_ad";
    if (document.querySelector(".video-ads:not(:empty), .ytp-ad-player-overlay, .ytp-ad-text")) return "youtube_ad";
    return "none";
  }

  function blockedMessage() {
    const text = (document.body?.innerText || "").slice(0, 12_000).toLowerCase();
    if (text.includes("ad blockers violate youtube") || text.includes("ad blocker") || text.includes("playback blocked")) {
      return "Provider reported blocked playback, possibly due to an ad/privacy extension.";
    }
    return null;
  }

  function reportRoute() {
    if (route === location.href) return;
    route = location.href;
    lastEndedRoute = "";
    sentUnknownDuration = false;
    observedVideo = null;
    send("player_state", { status: "route_changed" });
    checkSavedStatus();
    schedulePageScan();
    attach();
  }

  function verifyAndReportEnded(video) {
    // A short defer avoids reporting an ad/transition event that immediately
    // resumes the actual media.  Providers can still be advanced manually.
    const endingRoute = location.href;
    window.setTimeout(() => {
      if (endingRoute !== location.href || lastEndedRoute === endingRoute) return;
      const ad = adState();
      if (ad !== "none") {
        send("player_state", { status: "ad_transition", is_ad: true, ad_state: ad });
        return;
      }
      if (!hasFiniteDuration(video)) {
        send("unknown_duration", { detail: "Live or unknown-duration video; use Next manually." });
        return;
      }
      if (video.loop || video.currentTime < video.duration - 1.25) return;
      lastEndedRoute = endingRoute;
      send("ended", {
        duration: video.duration,
        current_time: video.currentTime,
        status: "verified_end",
        is_ad: false,
        ad_state: ad,
      });
    }, 180);
  }

  function observe(video) {
    if (!video || video === observedVideo) return;
    observedVideo = video;
    video.addEventListener("play", () => {
      const ad = adState();
      if (ad !== "none") {
        send("player_state", { status: "ad_playing", is_ad: true, ad_state: ad });
        return;
      }
      if (!hasFiniteDuration(video) && !sentUnknownDuration) {
        sentUnknownDuration = true;
        send("live", { detail: "Live or unknown-duration media cannot auto-advance safely." });
      } else {
        send("player_state", { status: "playing", duration: video.duration });
      }
    });
    video.addEventListener("pause", () => {
      if (!video.ended) send("player_state", { status: "paused", current_time: video.currentTime });
    });
    video.addEventListener("error", () => {
      const detail = video.error ? `HTML5 error ${video.error.code}` : "Provider video error";
      const blocked = blockedMessage();
      send(blocked ? "blocked" : "unavailable", { detail: blocked || detail });
    });
  }

  function attach() {
    reportRoute();
    const video = bestVideo();
    if (video) observe(video);
  }

  document.addEventListener("ended", (event) => {
    const video = event.target;
    if (!(video instanceof HTMLVideoElement)) return;
    // Only the current largest video is treated as the provider's main player.
    if (video !== bestVideo()) return;
    verifyAndReportEnded(video);
  }, true);

  const mutationObserver = new MutationObserver(() => {
    attach();
    schedulePageScan();
  });
  mutationObserver.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("popstate", reportRoute);
  window.addEventListener("hashchange", reportRoute);
  const originalPushState = history.pushState;
  const originalReplaceState = history.replaceState;
  history.pushState = function (...args) { const result = originalPushState.apply(this, args); queueMicrotask(reportRoute); return result; };
  history.replaceState = function (...args) { const result = originalReplaceState.apply(this, args); queueMicrotask(reportRoute); return result; };
  api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "library-player-control") return undefined;
    const video = bestVideo();
    if (!video) {
      sendResponse?.({ ok: false, error: "No provider video element was found" });
      return false;
    }
    if (message.command === "pause") video.pause();
    if (message.command === "play") video.play().catch(() => {});
    if (message.command === "play_pause") {
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    }
    if (message.command === "stop") {
      video.pause();
      try { video.currentTime = 0; } catch (_) { /* provider may block seeking */ }
    }
    sendResponse?.({ ok: true, paused: video.paused });
    return false;
  });
  window.setInterval(reportRoute, 1000);
  checkSavedStatus();
  schedulePageScan();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", attach, { once: true });
  else attach();
})();
