// CueForge in-app dialog module: themed stand-ins for the native
// confirm()/prompt()/alert(), which silently no-op inside iOS standalone
// (home-screen) web apps and look jarring and unthemed mid-show.
//
// Same modal-building pattern as importer.js's showDedupModal(): an overlay
// div built via innerHTML, Escape / click-outside handling, and a
// single-resolution guard so exactly one outcome ever fires.

import { esc } from "./util.js";

// Escape text for innerHTML, then turn newlines into <br> so multi-line
// messages render as line breaks instead of being squashed flat.
function escLines(s) {
  return esc(s).replace(/\n/g, "<br>");
}

// Shared overlay scaffold: builds `.modal-overlay > .modal`, wires Escape
// and click-outside to `onDismiss`, and returns a `finish(fn, ...args)`
// helper that tears the modal down and calls `fn` exactly once.
function buildOverlay(bodyHtml, onDismiss) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `<div class="modal">${bodyHtml}</div>`;
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
    if (e.key === "Escape") finish(onDismiss);
  }
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) finish(onDismiss);
  });

  return { overlay, finish };
}

/**
 * confirmDialog(message, { title, okLabel, cancelLabel, danger }) -> Promise<boolean>
 * Escape / click-outside / Cancel resolve false; OK resolves true. Pass
 * `danger: true` for destructive actions (reset, delete) to style OK red.
 */
export function confirmDialog(message, opts = {}) {
  const { title = "Confirm", okLabel = "OK", cancelLabel = "Cancel", danger = false } = opts;
  return new Promise((resolve) => {
    const { overlay, finish } = buildOverlay(`
      <h2>${esc(title)}</h2>
      <p>${escLines(message)}</p>
      <div class="modal-actions">
        <button class="btn${danger ? " danger" : ""}" type="button" data-ok>${esc(okLabel)}</button>
        <button class="btn" type="button" data-cancel>${esc(cancelLabel)}</button>
      </div>`, () => resolve(false));

    overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(resolve, false));
    const okBtn = overlay.querySelector("[data-ok]");
    okBtn.addEventListener("click", () => finish(resolve, true));
    okBtn.focus();
  });
}

/**
 * promptDialog(message, { title, initial, placeholder, okLabel }) -> Promise<string|null>
 * Escape / click-outside / Cancel resolve null. Enter or OK resolve the
 * trimmed input value (which may be ""; the caller decides what to do
 * with an empty submit).
 */
export function promptDialog(message, opts = {}) {
  const { title = "Input", initial = "", placeholder = "", okLabel = "OK" } = opts;
  return new Promise((resolve) => {
    const { overlay, finish } = buildOverlay(`
      <h2>${esc(title)}</h2>
      <p>${escLines(message)}</p>
      <input type="text" class="dialog-input" data-input value="${esc(initial)}" placeholder="${esc(placeholder)}" />
      <div class="modal-actions">
        <button class="btn" type="button" data-ok>${esc(okLabel)}</button>
        <button class="btn" type="button" data-cancel>Cancel</button>
      </div>`, () => resolve(null));

    const input = overlay.querySelector("[data-input]");
    const submit = () => finish(resolve, input.value.trim());
    overlay.querySelector("[data-cancel]").addEventListener("click", () => finish(resolve, null));
    overlay.querySelector("[data-ok]").addEventListener("click", submit);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submit();
    });
    input.focus();
    input.select();
  });
}

/**
 * alertDialog(message, { title }) -> Promise<void>
 * Single OK button, focused on open. Escape / click-outside / OK all
 * resolve (there is only one outcome).
 */
export function alertDialog(message, opts = {}) {
  const { title = "Notice" } = opts;
  return new Promise((resolve) => {
    const { overlay, finish } = buildOverlay(`
      <h2>${esc(title)}</h2>
      <p>${escLines(message)}</p>
      <div class="modal-actions">
        <button class="btn" type="button" data-ok>OK</button>
      </div>`, () => resolve());

    const okBtn = overlay.querySelector("[data-ok]");
    okBtn.addEventListener("click", () => finish(resolve));
    okBtn.focus();
  });
}
