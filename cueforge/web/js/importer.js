// CueForge shared import + dedup-modal helper.
//
// Both the Library "Import" button and the Playing-grid "drop an OS audio
// file onto a cell" flow go through here so there is exactly one place that
// talks to POST /api/import, POST /api/import/clone, and the dedup modal
// (see PROTOCOL.md REST table).

import { send, authHeaders } from "./ws.js";
import { esc, itemDuration, formatClock } from "./util.js";
import { alertDialog } from "./dialogs.js";

// ---------------------------------------------------------------- REST
// Remote clients must present the PIN on REST too (loopback is trusted).

export async function postImport(file) {
  const fd = new FormData();
  fd.append("file", file, file.name);
  const res = await fetch("/api/import", {
    method: "POST", headers: authHeaders(), body: fd,
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(detail || ("import failed: HTTP " + res.status));
  }
  return res.json();
}

export async function postClone(audioHash, name) {
  const res = await fetch("/api/import/clone", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ audioHash, name }),
  });
  if (!res.ok) throw new Error("clone failed: HTTP " + res.status);
  return res.json();
}

// ---------------------------------------------------------------- flow

/**
 * Import one File through /api/import. On a "new" result, places it (if a
 * grid `target` was given). On a "duplicate" result, opens the dedup modal
 * and lets the user choose an existing match or clone a new copy.
 *
 * `target` is `{ page, column, row }` to place the resulting item, or `null`
 * for a library-only import (no placement).
 *
 * Resolves when the whole flow (including any modal) is done. Never throws;
 * import failures are surfaced via `alertDialog()` (best-effort, matches
 * the themed-dialog UX in this codebase).
 */
export async function importFileWithDedup(file, target) {
  let result;
  try {
    result = await postImport(file);
  } catch (e) {
    await alertDialog("Import failed: " + file.name + "\n" + e.message);
    return;
  }

  if (result.status === "new") {
    if (target && result.item) {
      send("placeCue", {
        libraryItemId: result.item.id,
        page: target.page, column: target.column, row: target.row,
      });
    }
    return;
  }

  // "duplicate": ask the user.
  await new Promise((resolve) => {
    showDedupModal({
      audioHash: result.audioHash,
      matches: result.matches || [],
      fileName: file.name,
      onUse: (itemId) => {
        if (target) {
          send("placeCue", {
            libraryItemId: itemId,
            page: target.page, column: target.column, row: target.row,
          });
        }
        resolve();
      },
      onClone: async (name) => {
        try {
          const item = await postClone(result.audioHash, name);
          if (target) {
            send("placeCue", {
              libraryItemId: item.id,
              page: target.page, column: target.column, row: target.row,
            });
          }
        } catch (e) {
          await alertDialog("Add copy failed: " + e.message);
        }
        resolve();
      },
      onCancel: () => resolve(),
    });
  });
}

// ---------------------------------------------------------------- modal

function matchSummary(item) {
  const bits = [item.type];
  const d = itemDuration(item);
  if (d != null) bits.push(formatClock(d));
  if (item.background && item.loop) bits.push("loop");
  return bits.join(" · ");
}

/**
 * showDedupModal({ audioHash, matches, fileName, onUse(itemId), onClone(name), onCancel })
 * Renders a themed modal listing each existing match with "Use this", plus
 * an "Add as new copy" action. Exactly one of the callbacks fires once.
 */
export function showDedupModal({ audioHash, matches, fileName, onUse, onClone, onCancel }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal dedup-modal">
      <h2>Already in the library</h2>
      <p>This audio is already in the library${fileName ? ` (from <b>${esc(fileName)}</b>)` : ""}.
         Use an existing item or add a new independent copy.</p>
      <div class="dedup-list" data-dedup-list></div>
      <div class="modal-actions">
        <div class="clone-row">
          <input type="text" class="clone-name" data-clone-name value="${esc(fileName ? fileName.replace(/\.[^.]+$/, "") : "clone")}" />
          <button class="btn" type="button" data-clone>Add as new copy</button>
        </div>
        <button class="btn" type="button" data-cancel>Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  let done = false;
  function finish(fn, ...args) {
    if (done) return;
    done = true;
    overlay.remove();
    document.removeEventListener("keydown", onKey);
    fn(...args);
  }
  function onKey(e) {
    if (e.key === "Escape") finish(onCancel);
  }
  document.addEventListener("keydown", onKey);

  const list = overlay.querySelector("[data-dedup-list]");
  list.innerHTML = (matches || []).map((m) => `
    <div class="dedup-item">
      <div class="dedup-info">
        <div class="dedup-name">${esc(m.name)}</div>
        <div class="dedup-meta">${esc(matchSummary(m))}</div>
      </div>
      <button class="btn" type="button" data-use="${esc(m.id)}">Use this</button>
    </div>`).join("") || `<div class="dedup-empty">No details available.</div>`;

  list.querySelectorAll("[data-use]").forEach((btn) => {
    btn.addEventListener("click", () => finish(onUse, btn.dataset.use));
  });
  overlay.querySelector("[data-clone]").addEventListener("click", () => {
    const nameInput = overlay.querySelector("[data-clone-name]");
    const name = (nameInput.value || "clone").trim() || "clone";
    finish(onClone, name);
  });
  overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(onCancel));
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) finish(onCancel);
  });
}
