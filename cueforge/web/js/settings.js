// CueForge Settings section. Talks to the REST-only
// config endpoints (PROTOCOL.md): /api/devices, /api/settings, /api/connection,
// /api/project/new|open|save, /api/projects. The live show/runtime pieces
// (project name, device-ok) still come from the WS-fed store; everything else
// is plain REST, refreshed each time this tab becomes active.

import * as store from "./store.js";
import { authHeaders, getPin } from "./ws.js";
import { esc } from "./util.js";
import { promptDialog, alertDialog, confirmDialog } from "./dialogs.js";
import {
  applyUpdate,
  forceUpdateCheck,
  refreshUpdateStatus,
} from "./update.js";

let sectionEl = null;
let els = null;
let unsub = null;

// Local (client-only) cache of the last REST reads, so re-renders driven by
// store updates (e.g. deviceOk ticking at 15 Hz) don't need to refetch.
let settingsCache = null;   // GET /api/settings
let devicesCache = null;    // GET /api/devices
let connectionCache = null; // GET /api/connection
let projectBusy = false;

// ---------------------------------------------------------------- REST

async function fetchJSON(url, opts) {
  // Remote clients must present the PIN on REST too (loopback is trusted).
  opts = Object.assign({}, opts);
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

function postJSON(url, body) {
  return fetchJSON(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// ---------------------------------------------------------------- mount

export function mount(container) {
  sectionEl = container;
  sectionEl.innerHTML = `
    <div class="settings-body">
      <div class="settings-grid">

        <section class="settings-card">
          <h3>Project</h3>
          <div class="settings-field">
            <label>Current project name (edit to rename)</label>
            <div class="settings-inline">
              <input type="text" data-proj-name placeholder="Project name" />
              <button class="btn" type="button" data-proj-rename>Rename</button>
            </div>
          </div>
          <div class="settings-actions">
            <button class="btn primary" type="button" data-proj-save>Save</button>
            <button class="btn" type="button" data-proj-new>New project</button>
          </div>
          <div class="settings-actions">
            <button class="btn" type="button" data-proj-download>Save to device</button>
            <button class="btn" type="button" data-proj-upload>Load from device</button>
            <input type="file" accept=".cueforge" data-proj-file hidden />
          </div>
          <div class="settings-field">
            <label>Saved projects &mdash; click to switch</label>
            <div class="settings-proj-list" data-proj-list>
              <div class="settings-note">Loading&hellip;</div>
            </div>
          </div>
          <div class="settings-note" data-proj-note></div>
        </section>

        <section class="settings-card">
          <h3>Output</h3>
          <div class="settings-field">
            <label>Output device</label>
            <select data-device-select></select>
          </div>
          <div class="settings-field">
            <label>Master trim (dB)</label>
            <input type="number" min="-60" max="12" step="0.5" data-master-db />
          </div>
          <div class="settings-status" data-device-status></div>
        </section>

        <section class="settings-card">
          <h3>Access</h3>
          <div class="settings-field">
            <label>PIN</label>
            <div class="settings-inline">
              <input type="text" inputmode="numeric" maxlength="8"
                     placeholder="(none — open access)" data-pin-input />
              <button class="btn" type="button" data-pin-save>Save</button>
            </div>
          </div>
          <div class="settings-field">
            <label>Port</label>
            <input type="text" data-port-readonly readonly />
          </div>
          <div class="settings-note" data-pin-note></div>
        </section>

        <section class="settings-card">
          <h3>Application</h3>
          <div class="settings-field">
            <label>Version</label>
            <div class="settings-inline">
              <input type="text" data-app-version readonly />
              <button class="btn" type="button" data-update-check>Check now</button>
            </div>
          </div>
          <label class="settings-toggle">
            <input type="checkbox" data-update-auto />
            <span>Check for updates automatically</span>
          </label>
          <div class="settings-field" data-update-row hidden>
            <label>Update available: <b data-update-latest></b>
              <a data-update-link href="#" target="_blank" rel="noopener">(release notes)</a>
            </label>
            <div class="settings-actions">
              <button class="btn primary" type="button" data-update-apply>Update &amp; restart</button>
            </div>
          </div>
          <div class="settings-note" data-update-note></div>
          <div class="settings-note settings-support">
            Enjoying CueForge?
            <a href="https://ko-fi.com/lesani" target="_blank" rel="noopener">Support development on Ko-fi &#9829;</a>
          </div>
        </section>

        <section class="settings-card settings-connect">
          <h3>Connect</h3>
          <div class="connect-body">
            <div class="connect-info">
              <div class="connect-row"><span class="label">LAN</span><span class="val" data-lan-url>&ndash;</span></div>
              <div class="connect-row"><span class="label">Local</span><span class="val" data-local-url>&ndash;</span></div>
              <div class="connect-row"><span class="label">PIN</span><span class="val" data-connect-pin>&ndash;</span></div>
            </div>
            <div class="connect-qr">
              <img data-qr-img alt="Scan to join QR code" hidden />
              <div class="connect-caption">Scan to join (PIN embedded)</div>
            </div>
          </div>
        </section>

      </div>
    </div>`;

  els = {
    projName: sectionEl.querySelector("[data-proj-name]"),
    projRename: sectionEl.querySelector("[data-proj-rename]"),
    projNew: sectionEl.querySelector("[data-proj-new]"),
    projSave: sectionEl.querySelector("[data-proj-save]"),
    projDownload: sectionEl.querySelector("[data-proj-download]"),
    projUpload: sectionEl.querySelector("[data-proj-upload]"),
    projFile: sectionEl.querySelector("[data-proj-file]"),
    projList: sectionEl.querySelector("[data-proj-list]"),
    projNote: sectionEl.querySelector("[data-proj-note]"),

    deviceSelect: sectionEl.querySelector("[data-device-select]"),
    masterDb: sectionEl.querySelector("[data-master-db]"),
    deviceStatus: sectionEl.querySelector("[data-device-status]"),

    pinInput: sectionEl.querySelector("[data-pin-input]"),
    pinSave: sectionEl.querySelector("[data-pin-save]"),
    pinNote: sectionEl.querySelector("[data-pin-note]"),
    portReadonly: sectionEl.querySelector("[data-port-readonly]"),

    appVersion: sectionEl.querySelector("[data-app-version]"),
    updCheck: sectionEl.querySelector("[data-update-check]"),
    updAuto: sectionEl.querySelector("[data-update-auto]"),
    updRow: sectionEl.querySelector("[data-update-row]"),
    updLatest: sectionEl.querySelector("[data-update-latest]"),
    updLink: sectionEl.querySelector("[data-update-link]"),
    updApply: sectionEl.querySelector("[data-update-apply]"),
    updNote: sectionEl.querySelector("[data-update-note]"),

    lanUrl: sectionEl.querySelector("[data-lan-url]"),
    localUrl: sectionEl.querySelector("[data-local-url]"),
    connectPin: sectionEl.querySelector("[data-connect-pin]"),
    qrImg: sectionEl.querySelector("[data-qr-img]"),
  };

  wireProject();
  wireOutput();
  wireAccess();
  wireUpdate();

  unsub = store.subscribe(renderFromStore);
  renderFromStore(store.get());
}

// Force a full REST + store re-render (e.g. when the Settings tab becomes
// active). Cheap enough to call unconditionally; the section is hidden the
// rest of the time so this only runs when the operator is looking at it.
export function refresh() {
  if (!sectionEl) return;
  renderFromStore(store.get());
  loadSettings();
  loadDevices();
  loadConnection();
  loadProjects();
  loadUpdateStatus();
}

// ---------------------------------------------------------------- store-driven bits

function renderFromStore(s) {
  if (!els || !sectionEl) return;
  if (sectionEl.hidden) return; // not the active tab -- skip work
  const show = s.show;
  const currentName = show && show.name ? show.name : "";
  if (els.projName && document.activeElement !== els.projName) {
    els.projName.value = currentName;
  }
  highlightCurrentProject(currentName);
  const rt = s.runtime;
  if (els.deviceStatus) {
    if (!rt) {
      els.deviceStatus.textContent = "";
    } else if (rt.deviceOk) {
      els.deviceStatus.textContent = "Audio device: OK";
      els.deviceStatus.classList.remove("bad");
      els.deviceStatus.classList.add("ok");
    } else {
      els.deviceStatus.textContent = "Audio device: unavailable";
      els.deviceStatus.classList.remove("ok");
      els.deviceStatus.classList.add("bad");
    }
  }
}

// ---------------------------------------------------------------- project

function currentProjectName() {
  const s = store.get();
  return s && s.show && s.show.name ? s.show.name : "";
}

function wireProject() {
  const commitRename = async () => {
    const name = (els.projName.value || "").trim();
    const cur = currentProjectName();
    if (!name || name === cur) {
      els.projName.value = cur;   // revert empty edits
      return;
    }
    setProjNote("");
    try {
      await postJSON("/api/project/rename", { name });
      setProjNote(`Renamed to "${name}".`);
      loadProjects();
    } catch (e) {
      setProjNote("Rename failed: " + e.message, true);
      els.projName.value = cur;
    }
  };
  els.projRename.addEventListener("click", commitRename);
  els.projName.addEventListener("keydown", (e) => {
    if (e.key === "Enter") els.projName.blur();
    if (e.key === "Escape") { els.projName.value = currentProjectName(); els.projName.blur(); }
  });
  els.projName.addEventListener("blur", commitRename);

  els.projNew.addEventListener("click", async () => {
    const name = ((await promptDialog("New project name:")) || "").trim();
    if (!name) return;
    setProjNote("");
    projectBusy = true;
    try {
      await postJSON("/api/project/new", { name });
      setProjNote(`Created "${name}".`);
      loadProjects();
    } catch (e) {
      setProjNote("New project failed: " + e.message, true);
    } finally {
      projectBusy = false;
    }
  });

  els.projSave.addEventListener("click", async () => {
    setProjNote("");
    try {
      const result = await postJSON("/api/project/save", {});
      setProjNote(`Saved "${result.name}".`);
      loadProjects();
    } catch (e) {
      setProjNote("Save failed: " + e.message, true);
    }
  });

  // Save to device: download the current project as a .cueforge file to the
  // browser's device (for a USB transfer). A <a download> cannot send headers,
  // so remote clients authenticate via a PIN query param (localhost -> omitted).
  els.projDownload.addEventListener("click", () => {
    setProjNote("");
    let href = "/api/project/download";
    const pin = getPin();
    if (pin) href += `?pin=${encodeURIComponent(pin)}`;
    const a = document.createElement("a");
    a.href = href;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setProjNote("Downloading current project to this device…");
  });

  // Load from device: pick a .cueforge from the browser device and upload it.
  els.projUpload.addEventListener("click", () => els.projFile.click());
  els.projFile.addEventListener("change", async () => {
    const file = els.projFile.files && els.projFile.files[0];
    els.projFile.value = "";           // allow re-selecting the same file later
    if (!file) return;
    setProjNote(`Uploading "${file.name}"…`);
    projectBusy = true;
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      // Note: no Content-Type header -- the browser sets the multipart boundary.
      const result = await fetchJSON("/api/project/upload", {
        method: "POST",
        body: form,
      });
      setProjNote(`Loaded "${result.name}".`);
      loadProjects();
    } catch (e) {
      setProjNote("Load failed: " + e.message, true);
    } finally {
      projectBusy = false;
    }
  });
}

async function loadProjects() {
  if (!els || !els.projList) return;
  try {
    const projects = await fetchJSON("/api/projects");
    renderProjectList(projects || []);
  } catch (e) {
    els.projList.innerHTML =
      `<div class="settings-note bad">Failed to list projects: ${esc(e.message)}</div>`;
  }
}

function renderProjectList(projects) {
  if (!projects.length) {
    els.projList.innerHTML =
      `<div class="settings-note">No saved projects yet. Use Save to store the current one.</div>`;
    return;
  }
  const cur = currentProjectName();
  els.projList.innerHTML = projects.map((p) => {
    const isCur = p.name === cur;
    return `
      <button class="settings-proj-item${isCur ? " current" : ""}" type="button"
              data-open="${esc(p.name)}"${isCur ? " disabled" : ""}>
        <span class="pname">${esc(p.name)}</span>
        ${isCur ? `<span class="pbadge">current</span>` : ""}
      </button>`;
  }).join("");
  els.projList.querySelectorAll("[data-open]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.open;
      setProjNote("");
      try {
        await postJSON("/api/project/open", { name });
        setProjNote(`Opened "${name}".`);
        loadProjects();
      } catch (e) {
        setProjNote("Open failed: " + e.message, true);
      }
    });
  });
}

// Re-highlight the "current" item without a full reload (store-driven).
function highlightCurrentProject(currentName) {
  if (!els || !els.projList) return;
  els.projList.querySelectorAll("[data-open]").forEach((btn) => {
    const isCur = btn.dataset.open === currentName;
    btn.classList.toggle("current", isCur);
    btn.disabled = isCur;
    const badge = btn.querySelector(".pbadge");
    if (isCur && !badge) {
      const span = document.createElement("span");
      span.className = "pbadge";
      span.textContent = "current";
      btn.appendChild(span);
    } else if (!isCur && badge) {
      badge.remove();
    }
  });
}

function setProjNote(text, isError) {
  if (!els.projNote) return;
  els.projNote.textContent = text || "";
  els.projNote.classList.toggle("bad", !!isError);
}

// ---------------------------------------------------------------- output

function wireOutput() {
  els.deviceSelect.addEventListener("change", async () => {
    const v = els.deviceSelect.value;
    const outputDevice = v === "" ? null : Number(v);
    try {
      settingsCache = await postJSON("/api/settings", { outputDevice });
    } catch (e) {
      await alertDialog("Failed to set output device: " + e.message);
    }
  });

  const commitMasterDb = async () => {
    let v = Number(els.masterDb.value);
    if (!Number.isFinite(v)) v = 0;
    v = Math.max(-60, Math.min(12, v));
    els.masterDb.value = v;
    try {
      settingsCache = await postJSON("/api/settings", { masterDb: v });
    } catch (e) {
      await alertDialog("Failed to set master trim: " + e.message);
    }
  };
  els.masterDb.addEventListener("change", commitMasterDb);
  els.masterDb.addEventListener("keydown", (e) => { if (e.key === "Enter") els.masterDb.blur(); });
}

async function loadDevices() {
  try {
    devicesCache = await fetchJSON("/api/devices");
  } catch (e) {
    devicesCache = [];
  }
  renderDeviceSelect();
}

function renderDeviceSelect() {
  if (!els || !els.deviceSelect) return;
  const current = settingsCache ? settingsCache.outputDevice : null;
  const list = devicesCache || [];
  const options = list.map((d) => {
    const sel = current != null && Number(current) === Number(d.index) ? " selected" : "";
    const tag = d.default ? " (default)" : "";
    return `<option value="${d.index}"${sel}>${esc(d.name)}${esc(tag)}</option>`;
  }).join("");
  const noneSelected = current == null ? " selected" : "";
  els.deviceSelect.innerHTML =
    `<option value=""${noneSelected}>(system default)</option>` + options;
}

// ---------------------------------------------------------------- access

function wireAccess() {
  els.pinSave.addEventListener("click", async () => {
    const pin = els.pinInput.value.trim();
    try {
      settingsCache = await postJSON("/api/settings", { pin });
      setPinNote(pin ? "PIN saved." : "PIN cleared — remote access is open.");
      await loadConnection();
    } catch (e) {
      setPinNote("Failed to save PIN: " + e.message, true);
    }
  });
  els.pinInput.addEventListener("keydown", (e) => { if (e.key === "Enter") els.pinSave.click(); });
}

function setPinNote(text, isError) {
  if (!els.pinNote) return;
  els.pinNote.textContent = text || "";
  els.pinNote.classList.toggle("bad", !!isError);
}

async function loadSettings() {
  try {
    settingsCache = await fetchJSON("/api/settings");
  } catch (e) {
    settingsCache = null;
    return;
  }
  applySettings();
}

function applySettings() {
  if (!els || !settingsCache) return;
  if (els.masterDb && document.activeElement !== els.masterDb) {
    els.masterDb.value = Number(settingsCache.masterDb) || 0;
  }
  if (els.pinInput && document.activeElement !== els.pinInput) {
    els.pinInput.value = settingsCache.pin || "";
  }
  if (els.portReadonly) {
    els.portReadonly.value = settingsCache.port != null ? String(settingsCache.port) : "";
  }
  renderDeviceSelect();
}

// ---------------------------------------------------------------- application update

let updateBusy = false; // an apply is in flight (download / restart)

function setUpdateNote(text, isError) {
  if (!els.updNote) return;
  els.updNote.textContent = text || "";
  els.updNote.classList.toggle("bad", !!isError);
}

function renderUpdateStatus(st) {
  if (!els || !els.updRow) return;
  if (!st) {
    els.appVersion.value = "";
    els.updRow.hidden = true;
    return;
  }
  els.appVersion.value = "v" + (st.current || "?");
  if (document.activeElement !== els.updAuto) {
    els.updAuto.checked = !!st.checkEnabled;
  }
  els.updRow.hidden = !st.updateAvailable;
  if (st.updateAvailable) {
    els.updLatest.textContent = "v" + (st.latest || "?");
    if (st.url) {
      els.updLink.href = st.url;
      els.updLink.hidden = false;
    } else {
      els.updLink.hidden = true;
    }
    els.updApply.disabled = updateBusy || !st.canApply;
    if (!st.canApply && !updateBusy) {
      setUpdateNote("Running from source — update with git pull, or download the release from GitHub.");
    }
  }
}

function wireUpdate() {
  els.updAuto.addEventListener("change", async () => {
    const on = els.updAuto.checked;
    try {
      settingsCache = await postJSON("/api/settings", { checkForUpdates: on });
    } catch (e) {
      els.updAuto.checked = !on;
      setUpdateNote("Failed to save setting: " + e.message, true);
      return;
    }
    if (on) loadUpdateStatus(true); // check right away when re-enabled
  });

  els.updCheck.addEventListener("click", () => loadUpdateStatus(true));

  els.updApply.addEventListener("click", async () => {
    if (updateBusy) return;
    const st = await refreshUpdateStatus();
    if (!st || !st.updateAvailable) return;
    const ok = await confirmDialog(
      `Update CueForge to v${st.latest}? The server downloads the new version ` +
      `and restarts: all playback stops and every client disconnects briefly. ` +
      `Don't do this mid-show.`,
      { title: "Update & restart", okLabel: "Update & restart", danger: true },
    );
    if (!ok) return;
    updateBusy = true;
    els.updApply.disabled = els.updCheck.disabled = true;
    setUpdateNote("Starting update…");
    try {
      await applyUpdate();
    } catch (e) {
      updateBusy = false;
      els.updApply.disabled = els.updCheck.disabled = false;
      setUpdateNote("Update failed: " + e.message, true);
      return;
    }
    pollApply();
  });
}

async function loadUpdateStatus(force) {
  let st;
  try {
    st = force ? await forceUpdateCheck() : await refreshUpdateStatus();
  } catch (e) {
    setUpdateNote("Update check failed: " + e.message, true);
    return;
  }
  if (!updateBusy) {
    setUpdateNote("");
    if (force && st && !st.updateAvailable) {
      setUpdateNote(st.error
        ? "Update check failed: " + st.error
        : "You're up to date.", !!st.error);
    }
  }
  renderUpdateStatus(st);
}

async function pollApply() {
  let st;
  try {
    st = await refreshUpdateStatus();
  } catch {
    return waitForRestart(); // server already went down
  }
  if (!st || st.phase === "downloading") {
    const pct = st && st.total ? ` ${st.percent || 0}%` : "…";
    setUpdateNote(`Downloading update${pct}`);
    setTimeout(pollApply, 1000);
  } else if (st.phase === "restarting") {
    waitForRestart();
  } else if (st.phase === "error") {
    updateBusy = false;
    els.updApply.disabled = els.updCheck.disabled = false;
    setUpdateNote("Update failed: " + (st.error || "unknown error"), true);
  } else {
    setTimeout(pollApply, 1000); // brief idle window right after starting
  }
}

// The server swaps its exe and restarts. Wait for it to actually go DOWN
// first (a too-early "/" probe would see the old instance and reload into the
// stale version), then reload once it answers again.
function waitForRestart() {
  setUpdateNote("Restarting CueForge…");
  const downDeadline = Date.now() + 20000;   // phase said restarting; ~2 s grace
  const upDeadline = Date.now() + 120000;

  function probeDown() {
    fetch("/api/update/status", { headers: authHeaders(), cache: "no-store" })
      .then(() => {
        if (Date.now() > downDeadline) probeUp();  // never saw it drop; reload anyway
        else setTimeout(probeDown, 1000);
      })
      .catch(() => setTimeout(probeUp, 1500));
  }
  function probeUp() {
    fetch("/", { cache: "no-store" })
      .then((res) => {
        if (res.ok) {
          setUpdateNote("Updated — reloading…");
          setTimeout(() => location.reload(), 500);
        } else {
          throw new Error();
        }
      })
      .catch(() => {
        if (Date.now() > upDeadline) {
          updateBusy = false;
          setUpdateNote(
            "Server did not come back — check the CueForge console window.", true,
          );
        } else {
          setTimeout(probeUp, 1500);
        }
      });
  }
  probeDown();
}

// ---------------------------------------------------------------- connect

async function loadConnection() {
  try {
    connectionCache = await fetchJSON("/api/connection");
  } catch (e) {
    connectionCache = null;
  }
  renderConnection();
}

function renderConnection() {
  if (!els) return;
  const c = connectionCache;
  if (els.lanUrl) els.lanUrl.textContent = c && c.lanUrl ? c.lanUrl : "–";
  if (els.localUrl) els.localUrl.textContent = c && c.url ? c.url : "–";
  if (els.connectPin) els.connectPin.textContent = c && c.pin ? c.pin : "(none — open access)";
  if (els.qrImg) {
    if (c && c.qr) {
      els.qrImg.src = c.qr;
      els.qrImg.hidden = false;
    } else {
      els.qrImg.removeAttribute("src");
      els.qrImg.hidden = true;
    }
  }
}
