// CueForge bootstrap: connects the ws, feeds the store, wires local tab
// switching (per-device only), renders the top bar, mounts the Playing section,
// and installs the global booth keyboard handler.

import * as ws from "./ws.js";
import * as store from "./store.js";
import * as playing from "./playing.js";
import * as library from "./library.js";
import * as settings from "./settings.js";
import { maybePromptUpdate, watchFfmpegDownload } from "./ffmpegUpdate.js";
import { refreshUpdateStatus } from "./update.js";
import { isTypingTarget, esc } from "./util.js";

const SECTIONS = ["playing", "library", "settings"];
let activeTab = "playing";
let lastEsc = 0;

function qs(sel) { return document.querySelector(sel); }

// ---------------------------------------------------------------- tabs

export function setTab(tab) {
  if (!SECTIONS.includes(tab)) return;
  activeTab = tab;
  for (const name of SECTIONS) {
    const sec = qs(`#section-${name}`);
    if (sec) sec.hidden = name !== tab;
    const btn = qs(`.tab[data-tab="${name}"]`);
    if (btn) btn.classList.toggle("active", name === tab);
  }
  const label = qs("[data-active-tab]");
  const active = qs(`.tab[data-tab="${tab}"]`);
  if (label && active) label.textContent = active.textContent;
  closeTabMenu();
  if (tab === "library") library.refresh();
  if (tab === "settings") settings.refresh();
}

function closeTabMenu() {
  const menu = qs("[data-tabs]");
  const toggle = qs("[data-menu-toggle]");
  if (menu) menu.classList.remove("open");
  if (toggle) toggle.setAttribute("aria-expanded", "false");
}

function wireTabs() {
  document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => setTab(btn.dataset.tab));
  });

  // Hamburger dropdown (< 600px; the toggle is display:none on desktop).
  const menu = qs("[data-tabs]");
  const toggle = qs("[data-menu-toggle]");
  if (menu && toggle) {
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => {
      if (menu.classList.contains("open") && !menu.contains(e.target)) {
        closeTabMenu();
      }
    });
  }
}

// ---------------------------------------------------------------- top bar

function renderTopbar(s) {
  const show = s.show, rt = s.runtime;
  const nameEl = qs("[data-project]");
  if (nameEl) {
    nameEl.innerHTML = show && show.name
      ? `${esc(show.name)} <span class="sub">&nbsp;&middot; live</span>`
      : `No show <span class="sub">&nbsp;&middot; --</span>`;
  }
  const pill = qs("[data-clients]");
  if (pill) {
    const n = rt && rt.clients != null ? rt.clients : 0;
    pill.textContent = `${n} ${n === 1 ? "client" : "clients"}`;
  }
}

// ---------------------------------------------------------------- keyboard

function pageNeighbor(delta) {
  const s = store.get();
  if (!s.show || !s.runtime || !Array.isArray(s.show.pages)) return;
  const pages = s.show.pages;
  const idx = pages.findIndex((p) => p.id === s.runtime.currentPage);
  if (idx < 0) return;
  const next = idx + delta;
  if (next < 0 || next >= pages.length) return;
  ws.send("setPage", { pageId: pages[next].id });
}

function onKeydown(e) {
  if (isTypingTarget(e.target)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  // A modal (dialog / picker / dedup) owns the keyboard while open: without
  // this guard, Space would fire GO and a double-Escape (dismissing two
  // dialogs quickly) would PANIC mid-edit. Native confirm() used to block JS
  // here; the in-app dialogs do not.
  if (document.querySelector(".modal-overlay, .overlay")) return;

  // Library tab: spacebar auditions the selected item (Audacity-style).
  if (activeTab === "library") {
    if (e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      library.toggleAudition();
    }
    return;
  }

  if (activeTab !== "playing") return;

  switch (e.key) {
    case " ":
    case "Spacebar":
      e.preventDefault();
      ws.send("go");
      break;
    case "ArrowUp":
      e.preventDefault();
      ws.send("cursorMove", { direction: "up" });
      break;
    case "ArrowDown":
      e.preventDefault();
      ws.send("cursorMove", { direction: "down" });
      break;
    case "ArrowLeft":
      e.preventDefault();
      ws.send("cursorMove", { direction: "left" });
      break;
    case "ArrowRight":
      e.preventDefault();
      ws.send("cursorMove", { direction: "right" });
      break;
    case "[":
      pageNeighbor(-1);
      break;
    case "]":
      pageNeighbor(1);
      break;
    case "p":
    case "P":
      e.preventDefault();
      ws.send(store.paused() ? "resume" : "pause");
      break;
    case "Escape": {
      const now = performance.now();
      if (now - lastEsc < 800) { ws.send("panic"); lastEsc = 0; }
      else lastEsc = now;
      break;
    }
    default:
      break;
  }
}

// ---------------------------------------------------------------- PIN prompt

function showPinPrompt() {
  if (qs("#pin-overlay")) return;
  const overlay = document.createElement("div");
  overlay.id = "pin-overlay";
  overlay.className = "overlay";
  overlay.innerHTML = `
    <div class="dialog">
      <h2>PIN required</h2>
      <p>This device is remote. Enter the CueForge PIN to connect.</p>
      <input type="password" inputmode="numeric" autocomplete="off"
             placeholder="PIN" data-pin-input />
      <div class="err" data-pin-err></div>
      <button class="btn" data-pin-submit type="button">Connect</button>
    </div>`;
  document.body.appendChild(overlay);
  const input = overlay.querySelector("[data-pin-input]");
  const submit = () => {
    const v = input.value.trim();
    ws.submitPin(v);
    overlay.remove();
  };
  overlay.querySelector("[data-pin-submit]").addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  input.focus();
}

// ---------------------------------------------------------------- disconnect overlay

function showConnectionLost() {
  if (qs("#disconnect-overlay")) return;
  const el = document.createElement("div");
  el.id = "disconnect-overlay";
  el.className = "disconnect-overlay";
  el.innerHTML = `
    <div class="disconnect-box">
      <div class="disconnect-icon">&#9888;</div>
      <h2>Server connection lost</h2>
      <p>Trying to reconnect&hellip;<br>Controls are disabled until the link is restored.</p>
    </div>`;
  document.body.appendChild(el);
}

function hideConnectionLost() {
  const el = qs("#disconnect-overlay");
  if (el) el.remove();
}

// ---------------------------------------------------------------- wake lock

// A control surface must not go dark mid-show. Best-effort screen wake lock:
// re-acquired whenever the tab returns to the foreground (the browser
// auto-releases it on backgrounding). Unsupported browsers and insecure
// contexts (plain-HTTP LAN serving can reject the request) fail silently --
// one console.info, no spam.
let wakeLockWarned = false;

async function acquireWakeLock() {
  if (!("wakeLock" in navigator)) return;
  try {
    await navigator.wakeLock.request("screen");
  } catch (e) {
    if (!wakeLockWarned) {
      wakeLockWarned = true;
      console.info("[wakelock] unavailable:", e && e.message ? e.message : e);
    }
  }
}

function installWakeLock() {
  acquireWakeLock();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") acquireWakeLock();
  });
}

// ---------------------------------------------------------------- boot

function boot() {
  installWakeLock();
  wireTabs();
  setTab("playing");
  playing.mount(qs("#section-playing"));
  library.mount(qs("#section-library"));
  settings.mount(qs("#section-settings"));

  // "Edit in Library..." from the placement popover (playing.js): switch tab
  // and focus the item. A CustomEvent keeps playing.js decoupled from main.js
  // (which already imports playing.js -- importing back would be circular).
  window.addEventListener("cueforge:editLibraryItem", (e) => {
    const id = e.detail && e.detail.libraryItemId;
    if (!id) return;
    setTab("library");
    library.focusItem(id);
  });

  store.subscribe(renderTopbar);
  renderTopbar(store.get());

  ws.onState((msg) => store.set(msg));
  ws.onError((message) => console.warn("[server error]", message));
  ws.onStatus((st) => {
    if (!st) return;
    const pill = qs("[data-clients]");
    if (st.needPin) {
      // A PIN is required (remote) -- that is not a "lost connection", show the
      // PIN prompt instead of the loud disconnect overlay.
      hideConnectionLost();
      showPinPrompt();
      return;
    }
    if (st.open === true) {
      hideConnectionLost();
      if (pill) pill.classList.remove("stale");
      // Now that we're connected (and authed): show a toast if ffmpeg is
      // downloading at startup, and offer an update if a newer release exists.
      watchFfmpegDownload();
      maybePromptUpdate();
      refreshUpdateStatus(); // app update badge on the Settings tab
    } else if (st.open === false) {
      if (pill) pill.classList.add("stale");
      if (st.code !== 1008) showConnectionLost();
    }
  });

  window.addEventListener("keydown", onKeydown);
  ws.start();

  // The server re-checks GitHub every 12 h; poll hourly so a tab left open
  // across days still lights the update badge without a reconnect.
  setInterval(refreshUpdateStatus, 60 * 60 * 1000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
