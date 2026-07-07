# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""EngineHub -- one AudioEngine per output device, behind a single control surface.

See docs/adr/0003-one-engine-per-device-behind-a-hub.md. The hub owns the default
engine plus one dedicated :class:`~cueforge.engine.AudioEngine` per distinct named
output device. It resolves an ``output_id`` to ``(engine, out_lo, out_mono)``,
enforces normal-cue exclusivity across devices, fans global operations out to
every engine, merges their status, and manages per-device engine lifecycle.

Threading
---------
The hub is deliberately **loop-thread-only and lock-free**. Every mutation
(``set_outputs``, ``set_default_device``, routed fires) and every read
(``get_status``) happens on the single asyncio event-loop thread: WS dispatch
runs under ``AppState.dispatch_lock`` and the 15 Hz status loop runs on the same
loop. The hub therefore holds no lock of its own. Each underlying
:class:`AudioEngine` remains independently thread-safe (its own command queue),
so the audio callback threads it starts are safe regardless.

Golden rule: a show with no defined outputs behaves exactly as a single
``AudioEngine`` did -- ``_resolve(None)`` returns ``(default_engine, 0, False)``,
which is byte-identical to the old ``pair=1`` path.
"""

from __future__ import annotations

from cueforge.engine.audio_engine import AudioEngine


class EngineHub:
    """Owns one engine per referenced device (plus the default) behind one API."""

    def __init__(self, *, engine_factory=None) -> None:
        # engine_factory() -> a fresh single-device engine (AudioEngine by default).
        self._make_engine = engine_factory or (lambda: AudioEngine())
        self._default_engine = self._make_engine()
        self._default_device = None          # global config outputDevice (index or None)
        self._default_device_name = None     # resolved name of the default device, best effort
        self._outputs: list[dict] = []       # validated {id,name,device,channel,mono}
        self._outputs_by_id: dict[str, dict] = {}
        self._device_engines: dict[str, object] = {}   # device NAME -> engine (named outputs)
        self._master_db = 0.0
        self._paused = False

    # =====================================================================
    # Resolution
    # =====================================================================
    def _engine_for_device(self, device):
        """Return the engine that should own ``device`` (a name string or None).

        None or the default device's name -> the default engine (shared). Any other
        referenced device name -> its dedicated engine, created lazily in set_outputs.
        Returns None only if the device was never provisioned (defensive)."""
        if device is None or (self._default_device_name is not None
                              and device == self._default_device_name):
            return self._default_engine
        return self._device_engines.get(device)

    def _resolve(self, output_id):
        """output_id -> (engine, out_lo, out_mono). Dangling id or an unavailable
        device falls back to the Default Output (default engine, channels 1-2)."""
        if output_id is None:
            return self._default_engine, 0, False
        o = self._outputs_by_id.get(output_id)
        if o is None:
            return self._default_engine, 0, False           # dangling -> Default
        engine = self._engine_for_device(o.get("device"))
        if engine is None or not getattr(engine, "device_ok", True):
            return self._default_engine, 0, False           # device unavailable -> Default
        out_lo = max(0, int(o.get("channel", 1)) - 1)        # 1-based channel -> 0-based column
        return engine, out_lo, bool(o.get("mono", False))

    def _all_engines(self):
        """The default engine plus every distinct named-device engine (deduped by
        identity so a named output sharing the default device isn't double-counted)."""
        seen, out = set(), []
        for eng in [self._default_engine, *self._device_engines.values()]:
            if id(eng) not in seen:
                seen.add(id(eng)); out.append(eng)
        return out

    # =====================================================================
    # Routed fires (with cross-device normal exclusivity)
    # =====================================================================
    def play_normal(self, cue_id, pcm, *, gain_db=0.0, fade_in=0.0, fade_out=0.0,
                    fade_shape="linear", output_id=None):
        engine, out_lo, out_mono = self._resolve(output_id)
        # Deviation from ADR 0003 wording ("track the live-normal engine"): we
        # fan-out-stop every OTHER engine's normal. It is strictly equivalent and
        # simpler -- stop_normal is a no-op where nothing is playing, and it also
        # correctly kills a normal a scheduled fire started on another engine, a
        # case a single tracked reference would miss.
        for other in self._all_engines():
            if other is not engine:
                other.stop_normal()                 # cross-device exclusivity (no-op where idle)
        engine.play_normal(cue_id, pcm, gain_db=gain_db, fade_in=fade_in,
                           fade_out=fade_out, fade_shape=fade_shape,
                           out_lo=out_lo, out_mono=out_mono)

    def schedule_normal(self, cue_id, pcm, start_in_frames, *, gain_db=0.0, fade_in=0.0,
                        fade_out=0.0, fade_shape="linear", output_id=None):
        engine, out_lo, out_mono = self._resolve(output_id)
        engine.schedule_normal(cue_id, pcm, start_in_frames, gain_db=gain_db,
                               fade_in=fade_in, fade_out=fade_out, fade_shape=fade_shape,
                               out_lo=out_lo, out_mono=out_mono)
        for other in self._all_engines():
            if other is not engine:
                other.schedule_stop_normal(cue_id, start_in_frames)   # same cue_id -> cancels with the chain

    def play_background(self, cue_id, pcm, *, gain_db=0.0, fade_in=0.0, loop=False,
                        fade_shape="linear", output_id=None):
        engine, out_lo, out_mono = self._resolve(output_id)
        engine.play_background(cue_id, pcm, gain_db=gain_db, fade_in=fade_in, loop=loop,
                               fade_shape=fade_shape, out_lo=out_lo, out_mono=out_mono)

    def schedule_background(self, cue_id, pcm, start_in_frames, *, gain_db=0.0, fade_in=0.0,
                            loop=False, fade_shape="linear", output_id=None):
        engine, out_lo, out_mono = self._resolve(output_id)
        engine.schedule_background(cue_id, pcm, start_in_frames, gain_db=gain_db,
                                   fade_in=fade_in, loop=loop, fade_shape=fade_shape,
                                   out_lo=out_lo, out_mono=out_mono)

    def audition(self, pcm, *, gain_db=0.0, fade_in=0.0, fade_out=0.0,
                 fade_shape="linear", loop=False, output_id=None):
        engine, out_lo, out_mono = self._resolve(output_id)
        engine.audition(pcm, gain_db=gain_db, fade_in=fade_in, fade_out=fade_out,
                        fade_shape=fade_shape, loop=loop, out_lo=out_lo, out_mono=out_mono)

    # =====================================================================
    # Pure fan-out ops (no routing; safe no-ops on engines that don't hold
    # the target). Each forwards identical args to every engine.
    # =====================================================================
    def stop_background(self, cue_id, *, mode="fade", fade_seconds=2.0):
        for e in self._all_engines():
            e.stop_background(cue_id, mode=mode, fade_seconds=fade_seconds)

    def stop_all_backgrounds(self, *, mode="fade", fade_seconds=2.0):
        for e in self._all_engines():
            e.stop_all_backgrounds(mode=mode, fade_seconds=fade_seconds)

    def set_cue_gain(self, cue_id, target_db, ramp_seconds, *, shape="linear",
                     stop_when_done=False):
        for e in self._all_engines():
            e.set_cue_gain(cue_id, target_db, ramp_seconds, shape=shape,
                           stop_when_done=stop_when_done)

    def set_all_backgrounds_gain(self, target_db, ramp_seconds, *, shape="linear",
                                 stop_when_done=False):
        for e in self._all_engines():
            e.set_all_backgrounds_gain(target_db, ramp_seconds, shape=shape,
                                       stop_when_done=stop_when_done)

    def schedule_stop_all_backgrounds(self, cue_id, start_in_frames, *, mode="fade",
                                      fade_seconds=2.0):
        for e in self._all_engines():
            e.schedule_stop_all_backgrounds(cue_id, start_in_frames, mode=mode,
                                            fade_seconds=fade_seconds)

    def schedule_stop_background(self, cue_id, target_id, start_in_frames, *, mode="fade",
                                 fade_seconds=2.0):
        for e in self._all_engines():
            e.schedule_stop_background(cue_id, target_id, start_in_frames, mode=mode,
                                       fade_seconds=fade_seconds)

    def schedule_fade(self, cue_id, target, start_in_frames, target_db, ramp_seconds, *,
                      shape="linear", stop_when_done=False):
        for e in self._all_engines():
            e.schedule_fade(cue_id, target, start_in_frames, target_db, ramp_seconds,
                            shape=shape, stop_when_done=stop_when_done)

    def cancel_scheduled(self, cue_id):
        for e in self._all_engines():
            e.cancel_scheduled(cue_id)

    def cancel_all_scheduled(self):
        for e in self._all_engines():
            e.cancel_all_scheduled()

    def stop_cue(self, cue_id):
        for e in self._all_engines():
            e.stop_cue(cue_id)

    def stop_audition(self):
        for e in self._all_engines():
            e.stop_audition()

    def set_master_gain(self, db):
        self._master_db = db                    # seed newly created engines with the current trim
        for e in self._all_engines():
            e.set_master_gain(db)

    # =====================================================================
    # Pause / panic (the hub owns the single global flag)
    # =====================================================================
    def pause_all(self):
        self._paused = True
        for e in self._all_engines():
            e.pause_all()

    def resume_all(self):
        self._paused = False
        for e in self._all_engines():
            e.resume_all()

    def panic(self):
        self._paused = False
        for e in self._all_engines():
            e.panic()

    # =====================================================================
    # Lifecycle
    # =====================================================================
    def set_default_device(self, device):
        self._default_device = device
        self._default_device_name = self._lookup_device_name(device)   # best effort; None headless
        self._default_engine.start_output(device)
        # AMENDMENT 1: re-reconcile per-device engines. If the operator points the
        # global device at (or away from) a device a named Output already has a
        # dedicated engine for, that device must fold into / out of the default
        # engine -- otherwise two streams could sit on one card or an engine leaks.
        self.set_outputs(self._outputs)

    def set_outputs(self, outputs):
        """Replace the outputs table (already validated by the controller) and
        reconcile per-device engines: create engines for newly referenced devices,
        stop engines for devices no longer referenced. Outputs on the default device
        (or device None) share the default engine and get no dedicated engine."""
        self._outputs = list(outputs or [])
        self._outputs_by_id = {o["id"]: o for o in self._outputs}
        wanted = {o["device"] for o in self._outputs
                  if o.get("device") and o["device"] != self._default_device_name}
        for device in list(self._device_engines):
            if device not in wanted:
                self._device_engines.pop(device).stop_output()
        for device in wanted:
            if device not in self._device_engines:
                eng = self._make_engine()
                eng.set_master_gain(self._master_db)     # seed with current trim
                # AMENDMENT 2: a freshly created engine must inherit the paused
                # state immediately so it does not play through a global pause.
                if self._paused:
                    eng.pause_all()
                eng.start_output(device)                 # exact-name resolve at open; unavailable -> device_ok False + reconnect loop
                self._device_engines[device] = eng

    def stop_all_output(self):
        for e in self._all_engines():
            e.stop_output()

    @staticmethod
    def _lookup_device_name(device):
        """Resolve a default-device index (or None) to its sounddevice name, so a
        named output pointing at the same physical device shares the default engine.
        Best effort: returns None headless / on any failure (mocked in tests)."""
        try:
            import sounddevice as sd
            idx = device if device is not None else sd.default.device[1]
            return sd.query_devices(idx).get("name")
        except Exception:
            return None

    # =====================================================================
    # Status merge
    # =====================================================================
    def get_status(self):
        # N = 1 + distinct named devices (typically 1-3). Each get_status() drains
        # one small deque and builds one dict; statuses are computed once per tick
        # here via ``by_engine``, so the 15 Hz cost is negligible.
        by_engine = {id(e): e.get_status() for e in self._all_engines()}
        default_status = by_engine[id(self._default_engine)]
        normal = None
        audition = None
        audition_active = False
        backgrounds = []
        scheduled = []
        for e in self._all_engines():
            st = by_engine[id(e)]
            if normal is None and st.get("normal"):
                normal = st["normal"]
            backgrounds.extend(st.get("backgrounds", []))
            scheduled.extend(st.get("scheduled", []))
            if audition is None and st.get("audition"):
                audition = st.get("audition")
            audition_active = audition_active or bool(st.get("audition_active"))
        outputs = []
        for o in self._outputs:
            e = self._engine_for_device(o.get("device"))
            st = by_engine.get(id(e)) if e is not None else None
            outputs.append({
                "id": o["id"],
                "deviceOk": bool(getattr(e, "device_ok", False)) if e is not None else False,
                "deviceChannels": int(st.get("output_channels", 2)) if st else 0,
            })
        return {
            "normal": normal,
            "backgrounds": backgrounds,
            "audition": audition,
            "audition_active": audition_active,
            "device_ok": bool(default_status.get("device_ok", False)),      # legacy: default engine
            "output_channels": int(default_status.get("output_channels", 2)),  # legacy deviceChannels
            "bus_channels": int(default_status.get("bus_channels", 2)),
            "paused": bool(self._paused),
            "scheduled": scheduled,
            "outputs": outputs,
        }
