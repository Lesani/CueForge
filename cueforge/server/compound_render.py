# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Background compound-cue render orchestration.

Renders mutate the session (audio blob + item fields) from a worker thread,
while WS actions mutate the same session on the event loop. Discipline:
compute in a thread; read inputs and apply results ON THE LOOP under the
dispatch lock; one render at a time per item, latest edit wins.

Concurrency invariants:
- schedule/_trigger/task creation run only on the event loop (invoked from the
  dispatch hook, which runs inside apply_and_broadcast's dispatch_lock -- but
  schedule only *creates* a task, never awaits the lock, so no deadlock).
- All session reads/writes happen under state.dispatch_lock; the numpy render is
  the only thing off the lock and it touches only the deep-copied timeline +
  path_map + writes a NEW content-addressed file (never mutates existing blobs).
- Exactly one in-flight render per item (_active); a second request during flight
  sets _again and reruns once (latest-wins, collapses bursts).
- Session-identity guard prevents a late render writing into a different project.
"""
from __future__ import annotations

import asyncio
import copy

from cueforge.project.renderer import RenderError, compound_signature, render_timeline

DEBOUNCE_SECONDS = 1.0


def _has_clips(timeline) -> bool:
    return any((tr.get("clips") or []) for tr in (timeline or {}).get("tracks", []) or [])


def _resolve_hash(session, item_id):
    src = session.show.library.get(item_id)
    return src.audio_hash if src is not None else None


class CompoundRenderManager:
    def __init__(self, state) -> None:
        self.state = state                # AppState (.loop, .controller, .dispatch_lock, .broadcast_state)
        self._debounce: dict[str, asyncio.Task] = {}
        self._active: dict[str, asyncio.Task] = {}
        self._again: set[str] = set()

    # -- entry points (called on the event loop) --
    def schedule(self, item_id: str, *, immediate: bool = False) -> None:
        old = self._debounce.pop(item_id, None)
        if old is not None:
            old.cancel()
        delay = 0.0 if immediate else DEBOUNCE_SECONDS
        self._debounce[item_id] = self.state.loop.create_task(self._debounced(item_id, delay))

    def schedule_dirty_all(self) -> None:
        """On project open: re-render compounds whose signature is stale."""
        ctrl = self.state.controller
        if ctrl.session is None:
            return
        for item in list(ctrl.session.show.library.values()):
            if item.type != "compound" or not _has_clips(item.timeline):
                continue
            cur = compound_signature(item.timeline, lambda sid: _resolve_hash(ctrl.session, sid))
            if cur != item.render_signature or not item.audio_hash:
                self.schedule(item.id)

    # -- internals --
    async def _debounced(self, item_id, delay):
        try:
            if delay:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._debounce.pop(item_id, None)
        self._trigger(item_id)

    def _trigger(self, item_id):
        if item_id in self._active:
            self._again.add(item_id)        # latest-wins: rerun after current finishes
            return
        self._active[item_id] = self.state.loop.create_task(self._run(item_id))

    async def _run(self, item_id):
        try:
            await self._render_once(item_id)
        finally:
            self._active.pop(item_id, None)
            if item_id in self._again:
                self._again.discard(item_id)
                self._trigger(item_id)

    async def _render_once(self, item_id):
        ctrl = self.state.controller
        # (1) Read inputs + flip to "rendering" on the loop under the lock.
        async with self.state.dispatch_lock:
            session0 = ctrl.session
            if session0 is None:
                return
            item = session0.show.library.get(item_id)
            if item is None or item.type != "compound":
                return
            timeline = copy.deepcopy(item.timeline or {"tracks": []})
            signature = compound_signature(timeline, lambda sid: _resolve_hash(session0, sid))
            if not _has_clips(timeline):
                item.render_state = "pending"
                item.render_error = ""
                session0.autosave()
            else:
                item.render_state = "rendering"
                item.render_error = ""
                audio_dir = session0.audio_dir
                path_map = {}
                for tr in timeline.get("tracks", []):
                    for cl in tr.get("clips", []):
                        sid = cl.get("itemId")
                        h = _resolve_hash(session0, sid)
                        path_map[sid] = session0.audio_path(h) if h else None
                session0.autosave()
        if not _has_clips(timeline):
            await self.state.broadcast_state()
            return
        await self.state.broadcast_state()   # show "Rendering..."

        # (2) Heavy work off the loop.
        try:
            audio_hash, duration = await asyncio.to_thread(
                render_timeline, timeline, path_map, audio_dir)
        except Exception as exc:             # RenderError or decode failure
            async with self.state.dispatch_lock:
                if ctrl.session is session0:
                    it = ctrl.session.show.library.get(item_id)
                    if it is not None and it.type == "compound":
                        it.render_state = "error"
                        it.render_error = str(exc)[:300]
                        ctrl.session.autosave()
            await self.state.broadcast_state()
            return

        # (3) Apply results on the loop under the lock.
        async with self.state.dispatch_lock:
            if ctrl.session is not session0:  # project switched mid-render
                return                        # blob orphaned in old work dir; harmless
            it = ctrl.session.show.library.get(item_id)
            if it is None or it.type != "compound":
                return                        # deleted mid-render
            it.audio_hash = audio_hash        # last completed render (fired even if newer edits pend)
            it.duration = duration
            it.render_signature = signature
            cur = compound_signature(it.timeline or {"tracks": []},
                                     lambda sid: _resolve_hash(ctrl.session, sid))
            if cur != signature:
                it.render_state = "pending"   # edits arrived while rendering
                self._again.add(item_id)      # _run's finally reschedules
            else:
                it.render_state = "ready"
                it.render_error = ""
            ctrl.session.autosave()
        await self.state.broadcast_state()
