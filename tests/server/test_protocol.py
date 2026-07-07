# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Pure message-shaping tests for the protocol layer."""

from __future__ import annotations

from cueforge.server import protocol


def test_map_engine_status_passes_paused_and_scheduled():
    status = {
        "normal": None,
        "backgrounds": [],
        "paused": True,
        "scheduled": [
            {"cue_id": "p1", "remaining_frames": 24000, "kind": "normal"},
            {"cue_id": "p2", "remaining_frames": 48000, "kind": "background"},
        ],
    }
    mapped = protocol.map_engine_status(status)
    assert mapped["paused"] is True
    assert mapped["scheduled"] == [
        {"placementId": "p1", "cueId": "p1", "kind": "normal", "remainingMs": 500},
        {"placementId": "p2", "cueId": "p2", "kind": "background", "remainingMs": 1000},
    ]


def test_map_engine_status_defaults_when_keys_absent():
    # An older engine / FakeEngine omits both keys entirely.
    mapped = protocol.map_engine_status({"normal": None, "backgrounds": []})
    assert mapped["paused"] is False
    assert mapped["scheduled"] == []
    assert mapped["outputs"] == []


def test_actions_include_named_output_actions():
    assert protocol.SET_OUTPUTS in protocol.ACTIONS
    assert protocol.TEST_OUTPUT in protocol.ACTIONS


def test_compound_actions_registered():
    assert protocol.CREATE_COMPOUND in protocol.ACTIONS
    assert protocol.UPDATE_TIMELINE in protocol.ACTIONS
    assert protocol.RENDER_COMPOUND in protocol.ACTIONS


def test_map_engine_status_includes_outputs():
    status = {
        "normal": None,
        "backgrounds": [],
        "outputs": [{"id": "o1", "deviceOk": True, "deviceChannels": 4}],
    }
    mapped = protocol.map_engine_status(status)
    assert mapped["outputs"] == [{"id": "o1", "deviceOk": True, "deviceChannels": 4}]


def test_build_runtime_surfaces_device_channels(controller, fake_engine):
    fake_engine.status["output_channels"] = 8
    rt = controller.build_runtime()
    assert rt["deviceChannels"] == 8


def test_build_runtime_device_channels_defaults_to_2(controller, fake_engine):
    fake_engine.status.pop("output_channels", None)
    rt = controller.build_runtime()
    assert rt["deviceChannels"] == 2
