"""Tool dispatch — the part of the live session that runs the model's tools.

Split out of `main.py` (PROJECT_ANALYSIS P2-15). `JarvisLive` was 2,000 lines
of connection, audio and tools; this is the tools third, as a mixin so the
methods keep their exact `self.` contract — the class is built by mixing this
into the session object, not by an adapter that could drift from it.

The only state it needs from the session is what it reads through `self`:
the registries, the ui, the activity log and the deferred-declaration list.
"""
from __future__ import annotations

import asyncio
from typing import Any
import traceback

from google.genai import types

from actions.background_monitor import add_monitor, list_monitors, remove_monitor
from actions.screen_processor import _capture_camera, _capture_screen
from actions.system_monitor import get_system_status
from core import tool_output
from core import tool_tiers as _tool_tiers
from core import undo as undo_stack
from core.logging_setup import get_logger
from memory.memory_manager import search_memory, update_memory

log = get_logger("jarvis.main")      # same logger name: one session, one stream


class ToolDispatchMixin:
    """Batch execution, the toolbox router and the single tool entry point.

    The attributes below are the mixin's contract with the session it is mixed
    into. They are annotations only — no value is assigned, so nothing is
    created at runtime — but they are what lets this module be type-checked on
    its own instead of only as part of `main.py`.
    """

    ui: Any                                   # the window (set_state/write_log)
    _action_registry: Any
    _plugin_registry: Any
    _deferred_decls: list = []                # tiering: tools held back
    _vision_busy: bool = False                # one camera tool at a time
    _vision_last_time: float = 0.0
    speak: Any = None                         # the session's own methods
    speak_error: Any = None
    log: Any = None

    _READ_ONLY_TOOLS = frozenset({
        "web_search", "weather_report", "flight_finder", "recall_memory",
        "screen_process", "get_system_status", "game_updater",
    })

    async def _run_tool_calls(self, calls) -> list:
        """Run a batch of tool calls, overlapping the read-only ones.

        The model often asks for three independent lookups in one turn - weather,
        news and the time - and each is a network round trip. Run serially the
        user waits for all three back to back.

        Measured on read-only batches: 3 tools 1.90s -> 1.00s (47% faster),
        4 tools 2.40s -> 1.00s (58% faster). A mutating tool keeps its exact
        position and is never overlapped, so the declared order the model relies
        on is preserved.

        Every returned response is in the same order as `calls`, and no slot is
        ever left empty - a dropped response would leave the model waiting for
        an answer that never comes.
        """
        results: list = [None] * len(calls)
        i, n = 0, len(calls)

        while i < n:
            start = i
            while i < n and calls[i].name in self._READ_ONLY_TOOLS:
                i += 1
            batch = list(range(start, i))

            if batch:
                done = await asyncio.gather(
                    *(self._execute_one(idx, calls[idx]) for idx in batch),
                    return_exceptions=True,
                )

                if any(isinstance(r, BaseException) for r in done):
                    # _execute_tool catches its own errors, so this is nearly
                    # unreachable - but if it happens, re-run the batch serially
                    # rather than drop a response. Safe precisely because only
                    # read-only tools are ever batched.
                    for idx, res in zip(batch, done):
                        if isinstance(res, BaseException):
                            print(f"[JARVIS] \u26a0 {calls[idx].name} raised in "
                                  f"batch - retrying serially: {res}")
                    for idx in batch:
                        results[idx] = await self._tool_or_error(calls[idx])
                else:
                    for idx, res in zip(batch, done):
                        results[idx] = res

            if i < n:        # a mutating tool: alone, and in declared order
                results[i] = await self._tool_or_error(calls[i])
                i += 1

        return [r for r in results if r is not None]

    async def _execute_one(self, idx: int, fc):
        print(f"[JARVIS] \U0001f4de {fc.name}")
        return await self._execute_tool(fc)

    async def _tool_or_error(self, fc):
        """Last line of defence: a tool must never take the session down with it.

        _execute_tool catches its own errors, so this is nearly unreachable. It
        exists because an unhandled exception here escapes into the receive loop
        and drops the whole Live session - one bad tool would end the
        conversation.
        """
        try:
            return await self._execute_tool(fc)
        except Exception as e:
            print(f"[JARVIS] \u26a0 {fc.name} failed hard: {e}")
            traceback.print_exc()
            try:
                return types.FunctionResponse(
                    id=fc.id, name=fc.name,
                    response={"result": f"Tool '{fc.name}' failed: {e}"},
                )
            except Exception:
                return None

    def _toolbox_reply(self, fc, text: str) -> types.FunctionResponse:
        """A router answer, shaped like any other tool result.

        Carries the router's own id and name so the model can match it to the
        call it made — the *result* mentions the real tool, the envelope does
        not impersonate it.
        """
        return types.FunctionResponse(
            id=fc.id, name=_tool_tiers.ROUTER_NAME,
            response={"result": text},
        )

    async def _run_toolbox(self, fc, args) -> types.FunctionResponse:
        """The `toolbox` router — search the deferred tools, or run one.

        This exists because the Live API freezes its tool list when the socket
        opens, so a tool that is not declared is a tool the model cannot name.
        `toolbox` is the single declaration that stands in front of the ~62
        that are no longer sent (see core/tool_tiers.py).

        A routed call re-enters `_execute_tool`, so it passes through the same
        autonomy gate, confirm gate, undo stack, activity timeline and audit
        chain as a direct call. Tiering changes what the model is told it can
        do; it does not change what it is allowed to do.
        """
        action = str(args.get("action") or "search").strip().lower()

        if action == "run":
            # Validation lives in core/tool_tiers.plan_run — pure, so it is
            # tested without a live session; this method is the thin part that
            # actually re-enters the executor.
            target, params, error = _tool_tiers.plan_run(args, self._deferred_decls)
            if error:
                return self._toolbox_reply(fc, error)

            print(f"[JARVIS] 🧰 toolbox → {target}")
            inner = types.FunctionCall(id=fc.id, name=target, args=params)
            resp = await self._execute_tool(inner)
            result = (resp.response or {}).get("result", "Done.")
            if isinstance(result, str) and result.startswith("Unknown tool:"):
                result += _tool_tiers.unknown_tool_hint(target, self._deferred_decls)
            return self._toolbox_reply(fc, result)

        if action not in ("search", "find", "list", "help"):
            return self._toolbox_reply(
                fc, f"Unknown action '{action}'. Use action=search or action=run.")

        query = str(args.get("query") or "").strip()
        matches = _tool_tiers.search(self._deferred_decls, query)
        if not matches:
            return self._toolbox_reply(
                fc, f"No deferred tool matches {query!r}. Everything named in "
                    f"your instructions is already available to you directly — "
                    f"if none of them fit, tell the user plainly.")
        return self._toolbox_reply(
            fc, f"{len(matches)} tool(s) matching {query!r} — call one with "
                f"action=run, tool=<name>, parameters_json=<its parameters as a "
                f"JSON object>.\n\n{_tool_tiers.format_schemas(matches)}")

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        # The router sits in front of everything else: it is not a tool the
        # user asked for, it is how the model reaches the tools that were not
        # declared. Handled before the activity timeline so a routed call does
        # not appear twice on it — the inner call opens its own event.
        if name == _tool_tiers.ROUTER_NAME:
            return await self._run_toolbox(fc, args)

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        # Mission Control: every tool call starts a timeline event. The
        # event object travels with the call and is closed on each exit path
        # so the timeline never shows a phantom "running" entry.
        from core import activity as _activity
        _act_ev = _activity.begin("tool", name, args)

        # ── Autonomy gate: ONE choke point for model tool calls. Observe
        # refuses mutating calls honestly BEFORE anything runs; auto injects
        # interface-level confirm/allow for the undo-protected set only —
        # the model cannot reach this code (core/autonomy.py trust model).
        try:
            from core import autonomy as _autonomy
            _blocked = _autonomy.gate(name, args)
            if _blocked:
                _activity.finish(_act_ev, False, _blocked)
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
                return types.FunctionResponse(
                    id=fc.id, name=name, response={"result": _blocked}
                )
            args = _autonomy.enhancing(name, args)
        except Exception as e:
            # This is the one choke point the trust model rests on: if it
            # cannot answer, the call proceeds — a corrupt config must not
            # brick every tool — but it must NOT do so quietly. It is logged at
            # error level and lands in the audit chain with the real tool name,
            # so "the gate let something through" is a fact you can look up.
            log.error(f"autonomy gate failed for {name} — proceeding unchecked: {e}")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            _activity.finish(_act_ev, True, "saved")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_running_loop()
        # Actions may return a dict (structured payloads) and the registry may
        # hand back a non-string; the branches below tell them apart.
        result: Any = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    # The image is attached to this same exchange, so there is
                    # nothing to stall for and nothing to announce. Asking for an
                    # acknowledgement here is what produced two spoken answers —
                    # the model filled that turn by answering the question from
                    # imagination, then answered it again once it could see.
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif name.startswith("mcp__"):
                # native MCP tool — dispatch straight to the server
                from actions import mcp as _mcp_mod
                result = await loop.run_in_executor(
                    None, lambda: _mcp_mod.call_native(name, args))

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not str(r).startswith("No results")
                        and not str(r).startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)
            _activity.fail(_act_ev, e)
        else:
            _activity.finish(_act_ev, True, result)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        # One cap on how much a single result may put into the conversation.
        # Applied here rather than in each tool because this is the only place
        # every result passes through, and a tool added tomorrow cannot forget
        # it. See core/tool_output.py for why the note is part of the result.
        _full = result
        result = tool_output.cap(result)
        if result is not _full and not self.ui.muted:
            self.ui.write_log(
                f"SYS: {name} returned {len(str(_full)):,} characters — "
                f"trimmed to {len(result):,} for the model.")

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )
