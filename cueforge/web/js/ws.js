// CueForge WebSocket client.
//
// Connects to /ws on the same host/port, auto-reconnects with backoff, and
// exposes onState / onError subscriptions plus a send(action, params) helper.
// Loopback needs no PIN. If the socket closes with 1008 (remote without a valid
// PIN) we prompt for one and reconnect with ?pin=<pin>.

const STATE_LISTENERS = new Set();
const ERROR_LISTENERS = new Set();
const STATUS_LISTENERS = new Set(); // connection open/close notifications

let socket = null;
let backoff = 500;                  // ms, grows to a cap on repeated failures
const BACKOFF_MAX = 8000;
let reconnectTimer = null;
let pin = null;                     // remembered PIN for remote reconnects
let needPin = false;                // last close was a 1008 (auth) rejection
let manualClose = false;

// Persist the accepted PIN so a manual-entry remote client survives a reload
// without re-prompting. localStorage can throw (private browsing / disabled
// storage) -- treat it as best-effort.
const PIN_STORAGE_KEY = "cueforge.pin";

function loadStoredPin() {
  try { return localStorage.getItem(PIN_STORAGE_KEY); } catch { return null; }
}

function storePin(value) {
  try {
    if (value == null || value === "") localStorage.removeItem(PIN_STORAGE_KEY);
    else localStorage.setItem(PIN_STORAGE_KEY, value);
  } catch { /* best-effort */ }
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let url = proto + "//" + location.host + "/ws";
  if (pin != null && pin !== "") {
    url += "?pin=" + encodeURIComponent(pin);
  }
  return url;
}

function emit(listeners, arg) {
  for (const cb of listeners) {
    try { cb(arg); } catch (e) { console.error("[ws] listener error", e); }
  }
}

function scheduleReconnect() {
  if (manualClose || reconnectTimer) return;
  const delay = needPin ? 250 : backoff;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
  if (!needPin) backoff = Math.min(BACKOFF_MAX, backoff * 2);
}

function connect() {
  if (needPin && (pin == null || pin === "")) {
    // Wait for the PIN prompt to supply one before dialing again.
    return;
  }
  let s;
  try {
    s = new WebSocket(wsUrl());
  } catch (e) {
    console.error("[ws] construct failed", e);
    scheduleReconnect();
    return;
  }
  socket = s;

  s.addEventListener("open", () => {
    backoff = 500;
    needPin = false;
    if (pin != null && pin !== "") storePin(pin);   // this PIN works -- keep it
    emit(STATUS_LISTENERS, { open: true });
  });

  s.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "state") {
      emit(STATE_LISTENERS, msg);
    } else if (msg.type === "error") {
      emit(ERROR_LISTENERS, msg.message || "Unknown error");
    }
  });

  s.addEventListener("close", (ev) => {
    // Ignore a superseded socket's close: fast-reconnect (wake) may have already
    // dialed a replacement while this one was still CLOSING, and letting the old
    // close emit "lost" / schedule a reconnect would flicker the UI and clobber
    // the live socket. The current socket owns status + reconnect.
    if (socket !== s) return;
    socket = null;
    emit(STATUS_LISTENERS, { open: false, code: ev.code });
    if (ev.code === 1008) {
      // Remote client rejected for a missing/wrong PIN. Drop the stored PIN
      // too, so a server-side PIN change re-prompts cleanly instead of
      // looping on the stale value.
      needPin = true;
      pin = null;
      storePin(null);
      emit(STATUS_LISTENERS, { needPin: true });
      // Do not auto-dial; wait for submitPin().
      return;
    }
    scheduleReconnect();
  });

  s.addEventListener("error", () => {
    // A close event follows; reconnect handled there.
  });
}

// -------- public API --------

export function start() {
  manualClose = false;
  // Seed the PIN from the page URL (e.g. the QR/join link is
  // http://<lan-ip>:<port>/?pin=1234) so a remote client authenticates on its
  // very first dial instead of getting rejected and falling back to the prompt.
  // Fall back to the last accepted PIN (localStorage) so a manually-entered
  // PIN survives reloads.
  if (pin == null || pin === "") {
    try {
      const urlPin = new URLSearchParams(location.search).get("pin");
      if (urlPin) pin = urlPin;
    } catch { /* no URL / params -> leave pin unset, prompt handles it */ }
  }
  if (pin == null || pin === "") {
    const stored = loadStoredPin();
    if (stored) pin = stored;
  }

  // A backgrounded phone/iPad tab drops the socket, and the exponential
  // backoff could then sit out up to 8 s after the operator returns. On any
  // "we are back" signal, reset the backoff and dial immediately.
  const wake = () => {
    if (document.visibilityState === "hidden") return;
    backoff = 500;
    if (manualClose || needPin) return;
    if (socket && (socket.readyState === WebSocket.OPEN ||
                   socket.readyState === WebSocket.CONNECTING)) return;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    connect();
  };
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("focus", wake);
  window.addEventListener("online", wake);

  connect();
}

export function onState(cb) {
  STATE_LISTENERS.add(cb);
  return () => STATE_LISTENERS.delete(cb);
}

export function onError(cb) {
  ERROR_LISTENERS.add(cb);
  return () => ERROR_LISTENERS.delete(cb);
}

export function onStatus(cb) {
  STATUS_LISTENERS.add(cb);
  return () => STATUS_LISTENERS.delete(cb);
}

export function isConnected() {
  return socket != null && socket.readyState === WebSocket.OPEN;
}

// The PIN this client authenticated with ("" when none). REST calls from
// remote devices must present it too (header or ?pin=) -- loopback is the
// only origin the server trusts without one.
export function getPin() {
  return pin == null ? "" : pin;
}

// Merge the auth header for same-origin REST fetches. Always safe to send;
// the server ignores it on loopback.
export function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const p = getPin();
  if (p !== "") h["X-CueForge-Pin"] = p;
  return h;
}

// Send an action frame: { action, ...params }.
export function send(action, params) {
  if (!isConnected()) {
    console.warn("[ws] drop action while disconnected:", action);
    return false;
  }
  try {
    socket.send(JSON.stringify(Object.assign({ action }, params || {})));
    return true;
  } catch (e) {
    console.error("[ws] send failed", e);
    return false;
  }
}

// Supply a PIN after a 1008 rejection and reconnect immediately.
export function submitPin(value) {
  pin = value == null ? "" : String(value);
  needPin = false;
  storePin(pin);
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  connect();
}
