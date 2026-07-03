// CueForge shared Web Audio helpers: a single AudioContext plus a decoded
// AudioBuffer cache keyed by audioHash. Used by the waveform widget and the
// "this device" audition preview in the Library editor so both share one
// fetch + decode per audio file.

import { authHeaders } from "./ws.js";

let ctx = null;
const cache = new Map(); // audioHash -> Promise<AudioBuffer>

export function getAudioContext() {
  if (!ctx) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    ctx = new Ctor();
  }
  if (ctx.state === "suspended") {
    // Best-effort resume (some browsers require a user gesture; the caller
    // of getAudioContext() here is always in response to a click).
    ctx.resume().catch(() => {});
  }
  return ctx;
}

function fetchAndDecode(url) {
  return fetch(url, { headers: authHeaders() })
    .then((res) => {
      if (!res.ok) throw new Error("audio fetch failed: " + res.status);
      return res.arrayBuffer();
    })
    .then((buf) => getAudioContext().decodeAudioData(buf));
}

// Fetch + decode the stored FLAC for `audioHash`, cached across callers.
// iOS Safari's decodeAudioData cannot decode FLAC ("Waveform unavailable" on
// iPhone/iPad), so on a decode failure we retry with the server's WAV
// transcode (?format=wav) before giving up.
export function getAudioBuffer(audioHash) {
  if (!audioHash) return Promise.reject(new Error("no audioHash"));
  if (!cache.has(audioHash)) {
    const base = "/api/audio/" + encodeURIComponent(audioHash);
    const p = fetchAndDecode(base)
      .catch(() => fetchAndDecode(base + "?format=wav"));
    cache.set(audioHash, p);
    p.catch(() => cache.delete(audioHash));
  }
  return cache.get(audioHash);
}

// Drop a cached buffer (or everything) -- call after normalize/trim changes
// that would otherwise leave a stale decode around (the underlying stored
// bytes do not actually change today, but this keeps the cache honest).
export function clearAudioCache(audioHash) {
  if (audioHash) cache.delete(audioHash);
  else cache.clear();
}
