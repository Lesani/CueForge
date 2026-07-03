// CueForge ffmpeg update prompt: on first connect, ask the server whether a
// newer ffmpeg release is available and, if so, offer to update. "Update now"
// triggers a background download and polls /api/ffmpeg/status for progress
// (reusing the yt-progress bar). Shown at most once per page load; "Don't show
// again for this version" persists a dismissal server-side.

import { authHeaders } from "./ws.js";
import { esc } from "./util.js";

let prompted = false;

async function getStatus() {
  const res = await fetch("/api/ffmpeg/status", { headers: authHeaders() });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// ---- startup download toast (non-blocking, top-centre) -----------------
let watching = false;

function createToast() {
  const el = document.createElement("div");
  el.className = "cf-toast";
  el.innerHTML = `
    <div class="cf-toast-msg" data-msg></div>
    <div class="cf-toast-bar"><div class="cf-toast-fill" data-fill></div></div>`;
  document.body.appendChild(el);
  const msg = el.querySelector("[data-msg]");
  const fill = el.querySelector("[data-fill]");
  return {
    set(text, percent, isError) {
      msg.textContent = text;
      el.classList.toggle("error", !!isError);
      const determinate = typeof percent === "number";
      fill.classList.toggle("indeterminate", !determinate);
      fill.style.width = determinate ? `${Math.max(0, Math.min(100, percent))}%` : "100%";
    },
    remove() {
      el.remove();
    },
  };
}

/**
 * If ffmpeg is being downloaded at startup, show a non-blocking toast with
 * live progress and follow it to completion. No-op if nothing is downloading.
 */
export async function watchFfmpegDownload() {
  if (watching) return;
  let st;
  try {
    st = await getStatus();
  } catch {
    return; // offline / not authed yet -- retried on the next connect
  }
  if (st.phase !== "downloading") return;
  watching = true;
  const toast = createToast();

  function render(s) {
    if (s.phase === "downloading") {
      const mb = s.total
        ? ` (${(s.downloaded / 1048576).toFixed(0)}/${(s.total / 1048576).toFixed(0)} MB)`
        : "";
      toast.set(`Downloading ffmpeg… ${s.percent || 0}%${mb}`, s.percent || 0);
      setTimeout(poll, 1000);
    } else if (s.phase === "error") {
      toast.set("ffmpeg download failed — audio import unavailable", null, true);
      setTimeout(() => toast.remove(), 6000);
      watching = false;
    } else {
      toast.remove(); // ready/idle -- done, just disappear
      watching = false;
    }
  }
  async function poll() {
    let s;
    try {
      s = await getStatus();
    } catch {
      setTimeout(poll, 1500);
      return;
    }
    render(s);
  }
  render(st);
}

export async function maybePromptUpdate() {
  if (prompted) return;
  let st;
  try {
    st = await getStatus();
  } catch {
    return; // offline, or remote device not authed yet -- try again next connect
  }
  if (!st.updateAvailable) return;
  prompted = true;
  showUpdateModal(st);
}

function showUpdateModal(st) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <h2>ffmpeg update available</h2>
      <p>A newer ffmpeg is available: <b>${esc(st.version || "?")}</b> &rarr;
         <b>${esc(st.latest || "?")}</b>. Updating downloads a fresh build
         (~110&nbsp;MB) and replaces the copy CueForge uses.</p>
      <div class="yt-status" data-status hidden>
        <div class="yt-phase" data-phase></div>
        <div class="yt-progress"><div class="yt-progress-fill" data-fill></div></div>
      </div>
      <div class="yt-error" data-error hidden></div>
      <div class="modal-actions">
        <button class="btn primary" type="button" data-update>Update now</button>
        <button class="btn" type="button" data-dismiss>Dismiss</button>
        <button class="btn" type="button" data-forever>Don't show again for this version</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const statusEl = overlay.querySelector("[data-status]");
  const phaseEl = overlay.querySelector("[data-phase]");
  const fillEl = overlay.querySelector("[data-fill]");
  const errorEl = overlay.querySelector("[data-error]");
  const updateBtn = overlay.querySelector("[data-update]");
  const dismissBtn = overlay.querySelector("[data-dismiss]");
  const foreverBtn = overlay.querySelector("[data-forever]");

  let busy = false;
  let closed = false;

  function close() {
    if (closed) return;
    closed = true;
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) {
    if (e.key === "Escape" && !busy) close();
  }
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay && !busy) close();
  });

  function setPhase(text, percent) {
    statusEl.hidden = false;
    phaseEl.textContent = text;
    const determinate = typeof percent === "number";
    fillEl.classList.toggle("indeterminate", !determinate);
    fillEl.style.width = determinate ? `${Math.max(0, Math.min(100, percent))}%` : "100%";
  }
  function showError(msg) {
    errorEl.hidden = false;
    errorEl.textContent = msg;
  }

  dismissBtn.addEventListener("click", () => {
    if (!busy) close();
  });

  foreverBtn.addEventListener("click", async () => {
    if (busy) return;
    try {
      await fetch("/api/ffmpeg/dismiss", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ forever: true }),
      });
    } catch {
      /* best-effort; closing either way */
    }
    close();
  });

  updateBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    updateBtn.disabled = dismissBtn.disabled = foreverBtn.disabled = true;
    errorEl.hidden = true;
    setPhase("Starting…");
    try {
      await fetch("/api/ffmpeg/update", { method: "POST", headers: authHeaders() });
    } catch (e) {
      busy = false;
      dismissBtn.disabled = false;
      showError("Network error: " + e.message);
      return;
    }
    poll();
  });

  async function poll() {
    let st2;
    try {
      st2 = await getStatus();
    } catch {
      setTimeout(poll, 1500);
      return;
    }
    if (st2.phase === "downloading") {
      const mb = st2.total
        ? ` (${(st2.downloaded / 1048576).toFixed(0)}/${(st2.total / 1048576).toFixed(0)} MB)`
        : "";
      setPhase(`Downloading ${st2.percent || 0}%${mb}`, st2.percent || 0);
      setTimeout(poll, 1000);
    } else if (st2.phase === "ready") {
      setPhase("Update complete.", 100);
      setTimeout(close, 900);
    } else if (st2.phase === "error") {
      busy = false;
      dismissBtn.disabled = false;
      showError(st2.error || "Update failed.");
    } else {
      setTimeout(poll, 1000); // brief idle window right after starting
    }
  }
}
