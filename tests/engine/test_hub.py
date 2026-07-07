# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""EngineHub routing / exclusivity / fan-out / status-merge tests.

The hub is exercised with fake single-device engines (no audio device, no
sounddevice) injected via ``engine_factory``. ``_default_device_name`` is set
directly so device-name resolution is deterministic and never touches
sounddevice.
"""

from __future__ import annotations

from cueforge.engine.hub import EngineHub

PCM = object()   # opaque; the fakes never inspect it


class FakeSingleEngine:
    """Records every control call; ``get_status`` returns a controllable dict.

    Any method the hub calls that is not explicitly defined here is captured by
    ``__getattr__`` as ``(name, args, kwargs)`` in ``self.calls``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.device_ok = True
        self.status: dict = {
            "normal": None,
            "backgrounds": [],
            "audition": None,
            "audition_active": False,
            "device_ok": True,
            "output_channels": 2,
            "bus_channels": 2,
            "paused": False,
            "scheduled": [],
        }

    def get_status(self) -> dict:
        return self.status

    def start_output(self, device=None) -> None:
        self.calls.append(("start_output", (device,), {}))

    def stop_output(self) -> None:
        self.calls.append(("stop_output", (), {}))

    def __getattr__(self, name):
        def rec(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return rec

    # -- helpers --
    def call_kwargs(self, name):
        return [c[2] for c in self.calls if c[0] == name]

    def called(self, name) -> bool:
        return any(c[0] == name for c in self.calls)


def make_hub():
    """Return (hub, engines) where engines is appended-to as the hub creates them.

    engines[0] is always the default engine.
    """
    engines: list[FakeSingleEngine] = []

    def factory():
        e = FakeSingleEngine()
        engines.append(e)
        return e

    hub = EngineHub(engine_factory=factory)
    return hub, engines


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def test_default_output_routes_to_default_engine():
    hub, engines = make_hub()
    hub.play_normal("c", PCM, output_id=None)
    default = engines[0]
    kw = default.call_kwargs("play_normal")[0]
    assert kw["out_lo"] == 0
    assert kw["out_mono"] is False


def test_named_output_resolves_channel_to_out_lo_and_mono():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 3, "mono": True},
    ])
    named = engines[1]                        # created for "Dev B"
    hub.play_normal("c", PCM, output_id="o1")
    kw = named.call_kwargs("play_normal")[0]
    assert kw["out_lo"] == 2                  # 1-based channel 3 -> 0-based column 2
    assert kw["out_mono"] is True


def test_dangling_output_id_falls_back_to_default():
    hub, engines = make_hub()
    hub.play_normal("c", PCM, output_id="ghost")
    default = engines[0]
    kw = default.call_kwargs("play_normal")[0]
    assert kw["out_lo"] == 0 and kw["out_mono"] is False


def test_unavailable_device_falls_back_to_default():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    default, named = engines[0], engines[1]
    named.device_ok = False                   # device dropped
    hub.play_normal("c", PCM, output_id="o1")
    assert default.called("play_normal")      # routed to default instead
    assert not named.called("play_normal")


def test_named_outputs_same_device_share_one_engine():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
        {"id": "o2", "name": "O2", "device": "Dev B", "channel": 3, "mono": False},
    ])
    assert len(engines) == 2                   # default + exactly one for "Dev B"


def test_named_output_matching_default_device_uses_default_engine():
    hub, engines = make_hub()
    hub._default_device_name = "Main Card"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Main Card", "channel": 3, "mono": False},
    ])
    assert len(engines) == 1                    # no dedicated engine created
    hub.play_normal("c", PCM, output_id="o1")
    kw = engines[0].call_kwargs("play_normal")[0]
    assert kw["out_lo"] == 2                     # still resolves the channel on the default engine


# ---------------------------------------------------------------------------
# exclusivity
# ---------------------------------------------------------------------------
def test_live_normal_exclusivity_stops_other_engines():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    default, named = engines[0], engines[1]
    hub.play_normal("c", PCM, output_id="o1")
    assert named.called("play_normal")          # fired on the named engine
    assert default.called("stop_normal")        # other engine's normal killed
    assert not named.called("stop_normal")      # not on the firing engine


def test_scheduled_normal_fans_out_schedule_stop_normal():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    default, named = engines[0], engines[1]
    hub.schedule_normal("c", PCM, 1234, output_id="o1")
    assert named.called("schedule_normal")
    ssn = [c for c in default.calls if c[0] == "schedule_stop_normal"]
    assert ssn and ssn[0][1] == ("c", 1234)     # same cue_id + offset


# ---------------------------------------------------------------------------
# fan-out ops
# ---------------------------------------------------------------------------
def _hub_with_named_engine():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    return hub, engines


def test_fanout_pause_resume_panic_master_gain_cancel_reach_all_engines():
    hub, engines = _hub_with_named_engine()
    default, named = engines[0], engines[1]
    hub.pause_all()
    hub.resume_all()
    hub.panic()
    hub.set_master_gain(-6.0)
    hub.cancel_all_scheduled()
    for e in (default, named):
        assert e.called("pause_all")
        assert e.called("resume_all")
        assert e.called("panic")
        assert e.called("set_master_gain")
        assert e.called("cancel_all_scheduled")


def test_stop_cue_and_stop_background_fan_out():
    hub, engines = _hub_with_named_engine()
    default, named = engines[0], engines[1]
    hub.stop_cue("c")
    hub.stop_background("bg", mode="hard")
    for e in (default, named):
        assert e.called("stop_cue")
        assert e.called("stop_background")


# ---------------------------------------------------------------------------
# status merge
# ---------------------------------------------------------------------------
def test_get_status_merges_normal_backgrounds_scheduled_and_outputs():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    default, named = engines[0], engines[1]
    default.status = {
        **default.status,
        "normal": {"cue_id": "n", "frame": 5, "total_frames": 100, "finished": False},
        "backgrounds": [{"cue_id": "bg1", "frame": 1, "total_frames": 10, "loop": True}],
        "scheduled": [{"cue_id": "s1", "remaining_frames": 20, "kind": "normal"}],
        "output_channels": 2,
    }
    named.status = {
        **named.status,
        "backgrounds": [{"cue_id": "bg2", "frame": 2, "total_frames": 20, "loop": False}],
        "scheduled": [{"cue_id": "s2", "remaining_frames": 30, "kind": "background"}],
        "output_channels": 6,
    }
    hub.pause_all()          # hub-level paused flag
    st = hub.get_status()

    assert st["normal"]["cue_id"] == "n"                    # first non-None normal
    assert {b["cue_id"] for b in st["backgrounds"]} == {"bg1", "bg2"}   # concat
    assert {s["cue_id"] for s in st["scheduled"]} == {"s1", "s2"}       # concat
    assert st["paused"] is True                             # hub flag, not any engine
    o = next(x for x in st["outputs"] if x["id"] == "o1")
    assert o["deviceOk"] is True
    assert o["deviceChannels"] == 6                         # from the named engine's status


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def test_set_outputs_creates_and_stops_device_engines():
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"

    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    assert len(engines) == 2
    named = engines[1]
    assert ("start_output", ("Dev B",), {}) in named.calls   # opened its device

    hub.set_outputs([])                                       # remove it
    assert named.called("stop_output")                       # engine torn down


def test_paused_flag_is_hub_level():
    hub, engines = make_hub()
    assert hub.get_status()["paused"] is False
    hub.pause_all()
    assert hub.get_status()["paused"] is True
    hub.resume_all()
    assert hub.get_status()["paused"] is False


def test_set_outputs_seeds_new_engine_paused_when_hub_paused():
    # AMENDMENT 2: a freshly created engine inherits the paused state.
    hub, engines = make_hub()
    hub._default_device_name = "Default Dev"
    hub.pause_all()
    hub.set_outputs([
        {"id": "o1", "name": "O1", "device": "Dev B", "channel": 1, "mono": False},
    ])
    named = engines[1]
    assert named.called("pause_all")                         # seeded paused
