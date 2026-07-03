// CueForge YouTube import: a small modal that takes a URL, POSTs it to
// /api/import/youtube, and reads the streamed NDJSON progress to drive a
// progress bar. On completion the new library item arrives via the normal
// websocket state broadcast; duplicates reuse the shared dedup modal.

import { showDedupModal, postClone } from "./importer.js";
import { authHeaders } from "./ws.js";
import { alertDialog } from "./dialogs.js";

export function openYouTubeImport() {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal yt-modal">
      <h2>Import from YouTube</h2>
      <p>Paste a YouTube (or other supported) link. The audio is downloaded and
         added to the library as a new cue.</p>
      <input type="text" class="yt-url" data-yt-url placeholder="https://www.youtube.com/watch?v=..." />
      <div class="yt-status" data-yt-status hidden>
        <div class="yt-phase" data-yt-phase></div>
        <div class="yt-progress"><div class="yt-progress-fill" data-yt-fill></div></div>
      </div>
      <div class="yt-error" data-yt-error hidden></div>
      <div class="modal-actions">
        <button class="btn primary" type="button" data-yt-go>Import</button>
        <button class="btn" type="button" data-yt-cancel>Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const urlInput = overlay.querySelector("[data-yt-url]");
  const statusEl = overlay.querySelector("[data-yt-status]");
  const phaseEl = overlay.querySelector("[data-yt-phase]");
  const fillEl = overlay.querySelector("[data-yt-fill]");
  const errorEl = overlay.querySelector("[data-yt-error]");
  const goBtn = overlay.querySelector("[data-yt-go]");
  const cancelBtn = overlay.querySelector("[data-yt-cancel]");

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
    if (e.key === "Enter" && document.activeElement === urlInput) start();
  }
  document.addEventListener("keydown", onKey);

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
    statusEl.hidden = true;
  }

  async function start() {
    if (busy) return;
    const url = (urlInput.value || "").trim();
    if (!/^https?:\/\//i.test(url)) {
      showError("Enter a valid http(s) URL.");
      return;
    }
    busy = true;
    errorEl.hidden = true;
    urlInput.disabled = true;
    goBtn.disabled = true;
    cancelBtn.disabled = true;
    setPhase("Starting…");

    let res;
    try {
      res = await fetch("/api/import/youtube", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ url }),
      });
    } catch (e) {
      finishError("Network error: " + e.message);
      return;
    }
    if (!res.ok || !res.body) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
      finishError(detail || ("HTTP " + res.status));
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (line) handleEvent(JSON.parse(line));
        }
      }
    } catch (e) {
      finishError("Stream error: " + e.message);
    }
  }

  function handleEvent(ev) {
    switch (ev.phase) {
      case "updating":
        setPhase("Updating yt-dlp…");
        break;
      case "downloading":
        setPhase(`Downloading… ${Math.round(ev.percent || 0)}%`, ev.percent || 0);
        break;
      case "importing":
        setPhase("Converting & adding…");
        break;
      case "done":
        if (ev.status === "duplicate") {
          const title = (ev.item && ev.item.name) || "this video";
          close();
          showDedupModal({
            audioHash: ev.audioHash,
            matches: ev.matches || [],
            fileName: title,
            onUse: () => {},
            onClone: async (name) => {
              try { await postClone(ev.audioHash, name); }
              catch (e) { await alertDialog("Add copy failed: " + e.message); }
            },
            onCancel: () => {},
          });
        } else {
          // "new": the item arrives via the websocket state broadcast.
          close();
        }
        break;
      case "error":
        finishError(ev.detail || "Import failed.");
        break;
    }
  }

  function finishError(msg) {
    busy = false;
    urlInput.disabled = false;
    goBtn.disabled = false;
    cancelBtn.disabled = false;
    showError(msg);
  }

  goBtn.addEventListener("click", start);
  cancelBtn.addEventListener("click", () => { if (!busy) close(); });
  overlay.addEventListener("click", (e) => { if (e.target === overlay && !busy) close(); });
  urlInput.focus();
}
