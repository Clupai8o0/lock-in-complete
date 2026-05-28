"""Glue layer: serial reader + camera + Gemini + FSM + database.

Four concurrent asyncio tasks:
  - serial_consumer:  pulls Arduino frames/events, feeds the FSM
  - tick_loop:        ~1Hz FSM ticks + session counter updates
  - vision_loop:      every VISION_INTERVAL_S, capture image and judge
  - retention_loop:   daily image purge

Plus the serial reader's own loop. The orchestrator never blocks on a slow
sensor (vision in particular); it just keeps logging frames and ticking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from camera_client import CameraClient
from config import Config
from database import Database
from fsm import (
    Action,
    ActionKind,
    FocusFsm,
    FsmConfig,
    SensorFrame,
    State,
    VisionInput,
)
from serial_reader import SerialReader
from vision_judge import VisionJudge

log = logging.getLogger("lockin.orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.db.mark_orphan_sessions_incomplete()

        self.fsm = FocusFsm(
            FsmConfig(
                away_timeout_s=cfg.away_timeout_s,
                focus_confirm_s=cfg.focus_confirm_s,
                degrading_to_break_s=cfg.degrading_to_break_s,
                break_to_idle_s=cfg.break_to_idle_s,
                desk_away_s=cfg.desk_away_s,
            )
        )

        self.serial_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self.serial = SerialReader(cfg.serial_port, cfg.serial_baud, self.serial_queue)

        self.vision: VisionJudge | None = None
        if cfg.gemini_api_key:
            try:
                self.vision = VisionJudge(cfg.gemini_api_key, cfg.gemini_model)
            except Exception as e:
                log.error("vision init failed: %s", e)
                self.vision = None
        else:
            log.warning("GEMINI_API_KEY missing — vision disabled")

        self.camera = CameraClient(cfg.camera_url, cfg.camera_timeout_s)

        # JPEG bytes from the most recent vision cycle that judged "not focused";
        # only written to disk if the FSM opens a DISTRACTION_OPEN this tick.
        self._pending_jpeg: bytes | None = None
        # Last JPEG from any successful vision cycle this session — fallback
        # image for non-vision-triggered distractions (e.g. desk_away).
        self._last_jpeg: bytes | None = None
        self._stop = asyncio.Event()
        # snapshot of latest sensor frame for the dashboard
        self.last_frame_dict: dict[str, Any] | None = None
        self.start_wall_ts = datetime.now(timezone.utc)
        self._pomo_duration_s: float = cfg.pomodoro_duration_s
        self._pomo_baseline: float = 0.0       # fsm.focused_seconds when current pomodoro started
        self._pomo_prev_state: State = State.AWAY
        self._target_pomodoros: int = 4
        self._cmd_path = cfg.cmd_path

    # ----------------------------------------------------------------- run


    async def _interruptible_sleep(self, duration: float) -> None:
        """Sleep for duration but return immediately if _stop is set."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=duration)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        log.info("orchestrator starting")
        async with self.camera:
            tasks = [
                asyncio.create_task(self.serial.run(), name="serial"),
                asyncio.create_task(self._serial_consumer(), name="serial_consumer"),
                asyncio.create_task(self._tick_loop(), name="tick"),
                asyncio.create_task(self._vision_loop(), name="vision"),
                asyncio.create_task(self._retention_loop(), name="retention"),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in done:
                if t.exception():
                    log.exception("task crashed: %s", t.get_name(), exc_info=t.exception())
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.serial.stop()

    async def stop(self) -> None:
        self._stop.set()
        await self.serial.stop()

    # --------------------------------------------------------- serial input
    async def _serial_consumer(self) -> None:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(self.serial_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._handle_serial_message(msg)

    def _handle_serial_message(self, msg: dict[str, Any]) -> None:
        log.debug("serial msg: %s", msg)
        mtype = msg.get("type")
        if mtype == "frame":
            frame = SensorFrame(
                presence=bool(msg.get("presence")),
                dist_cm=_safe_float(msg.get("dist_cm")),
                temp_c=_safe_float(msg.get("temp_c")),
                humidity=_safe_float(msg.get("hum")),
                light=_safe_float(msg.get("light")),
            )
            self._frame_count += 1
            log.debug(
                "frame #%d: pir=%s dist=%s cm temp=%s°C hum=%s%% lux=%s",
                self._frame_count, frame.presence, frame.dist_cm,
                frame.temp_c, frame.humidity, frame.light,
            )
            if self._frame_count % 5 == 1:
                log.info(
                    "sensor #%d | pir=%s dist=%s cm temp=%s°C hum=%s%% lux=%s | state=%s",
                    self._frame_count, frame.presence, frame.dist_cm,
                    frame.temp_c, frame.humidity, frame.light,
                    self.fsm.state.value,
                )
            self.last_frame_dict = {
                **asdict(frame),
                "received_at": _utc_iso(),
            }
            self._check_sensor_blocked(frame)
            actions = self.fsm.on_sensor_frame(frame)
            # environment log every ~30s when session active
            self._maybe_log_environment(frame)
            asyncio.create_task(self._apply_actions(actions))
        elif mtype == "event":
            evt = msg.get("event")
            action = msg.get("action")
            if evt == "pir":
                log.info("event: pir")
                actions = self.fsm.on_pir()
            elif evt == "button" and isinstance(action, str):
                log.info("event: button %s", action)
                actions = self.fsm.on_button(action)
            else:
                log.warning("event: unrecognised %s", msg)
                actions = []
            asyncio.create_task(self._apply_actions(actions))
        elif mtype == "hello":
            log.info("arduino hello: %s", msg.get("fw"))
        else:
            log.debug("unknown serial msg: %s", msg)

    # ----------------------------------------------------- frame logging
    _frame_count: int = 0
    _sensor_blocked: bool = False
    _blocked_alerted_t: float = 0.0
    _BLOCKED_THRESHOLD_CM: float = 10.0
    _BLOCKED_ALERT_INTERVAL_S: float = 5.0

    # ----------------------------------------------------- env logging cadence
    _last_env_log_t: float = 0.0

    def _check_sensor_blocked(self, frame: SensorFrame) -> None:
        if frame.dist_cm is None or frame.dist_cm <= 0:
            return
        blocked = frame.dist_cm < self._BLOCKED_THRESHOLD_CM
        if blocked:
            now = time.monotonic()
            if now - self._blocked_alerted_t < self._BLOCKED_ALERT_INTERVAL_S:
                return
            self._blocked_alerted_t = now
            self._sensor_blocked = True
            log.warning("ultrasonic blocked: %.1f cm — something obstructing sensor", frame.dist_cm)
            asyncio.create_task(self.serial.buzz("blocked"))
            asyncio.create_task(self.serial.led("BLOCKED"))
        elif self._sensor_blocked:
            self._sensor_blocked = False
            log.info("ultrasonic unblocked — restoring LED to %s", self.fsm.state.value)
            asyncio.create_task(self.serial.led(self.fsm.state.value))

    def _maybe_log_environment(self, frame: SensorFrame) -> None:
        now = time.monotonic()
        if self.fsm.session_id is None:
            return
        if now - self._last_env_log_t < 30.0:
            return
        self._last_env_log_t = now
        self.db.log_environment(
            self.fsm.session_id, frame.temp_c, frame.humidity, frame.light
        )

    # ------------------------------------------------------------ tick loop
    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            self._process_commands()
            self._check_pomodoro()
            actions = self.fsm.tick()
            await self._apply_actions(actions)
            if self.fsm.session_id is not None:
                self.db.update_session_progress(
                    self.fsm.session_id,
                    int(self.fsm.focused_seconds),
                    int(self.fsm.total_seconds),
                )

    # ----------------------------------------------------------- vision loop
    async def _vision_loop(self) -> None:
        while not self._stop.is_set():
            await self._interruptible_sleep(self.cfg.vision_interval_s)
            # only judge when a session is active
            if self.fsm.session_id is None:
                continue
            if self.fsm.state not in (State.FOCUS, State.DEGRADING):
                continue
            await self._do_vision_cycle()

    async def _do_vision_cycle(self) -> None:
        capture = await self.camera.capture()
        if not capture:
            log.info("vision skipped (camera offline)")
            return
        if not self.vision:
            log.info("vision skipped (no gemini)")
            return
        judgment = await self.vision.judge(capture.jpeg)
        if not judgment:
            log.info("vision skipped (no judgment)")
            return
        log.info(
            "vision: focused=%s conf=%.2f obs=%s",
            judgment.focused,
            judgment.confidence,
            judgment.observation,
        )
        # remember the latest frame so desk_away distractions get an image too
        self._last_jpeg = capture.jpeg
        # stash the JPEG bytes; only persist to disk if DISTRACTION_OPEN fires.
        self._pending_jpeg = capture.jpeg if not judgment.focused else None
        actions = self.fsm.on_vision(
            VisionInput(
                focused=judgment.focused,
                confidence=judgment.confidence,
                observation=judgment.observation,
            )
        )
        await self._apply_actions(actions)
        self._pending_jpeg = None

    def _save_image(self, jpeg: bytes) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = self.cfg.image_dir / f"distraction-{ts}.jpg"
        try:
            path.write_bytes(jpeg)
        except OSError as e:
            log.warning("image save failed: %s", e)
            return ""
        return str(path)

    # -------------------------------------------------------- retention loop
    async def _retention_loop(self) -> None:
        # one immediate pass, then every 12 hours
        while not self._stop.is_set():
            try:
                n = self.db.purge_old_images(
                    self.cfg.image_retention_days, self.cfg.image_dir
                )
                if n:
                    log.info("retention: purged %d expired images", n)
            except Exception as e:
                log.warning("retention sweep failed: %s", e)
            await self._interruptible_sleep(12 * 3600)

    # --------------------------------------------------------- pomodoro + commands
    def _pomo_remaining(self) -> float | None:
        if self.fsm.state != State.FOCUS or self.fsm.session_id is None:
            return None
        return max(0.0, self._pomo_duration_s - (self.fsm.focused_seconds - self._pomo_baseline))

    def _check_pomodoro(self) -> None:
        cur = self.fsm.state
        # reset baseline when resuming from break
        if cur == State.FOCUS and self._pomo_prev_state == State.BREAK:
            self._pomo_baseline = self.fsm.focused_seconds
        # reset baseline when session ends
        if self.fsm.session_id is None and self._pomo_prev_state != State.AWAY:
            self._pomo_baseline = 0.0
        self._pomo_prev_state = cur

        if cur != State.FOCUS or self.fsm.session_id is None:
            return
        elapsed = self.fsm.focused_seconds - self._pomo_baseline
        if elapsed >= self._pomo_duration_s:
            log.info("pomodoro complete after %.0f min — starting break", elapsed / 60)
            self._pomo_baseline = self.fsm.focused_seconds
            actions = self.fsm.on_button("double")
            asyncio.create_task(self._apply_actions(actions))

    def _process_commands(self) -> None:
        try:
            if not self._cmd_path.exists():
                return
            cmd = json.loads(self._cmd_path.read_text())
            self._cmd_path.unlink(missing_ok=True)
            ctype = cmd.get("type")
            if ctype == "button":
                action = cmd.get("action", "single")
                log.info("dashboard cmd: button %s", action)
                actions = self.fsm.on_button(action)
                asyncio.create_task(self._apply_actions(actions))
            elif ctype == "settings":
                self._apply_settings({k: v for k, v in cmd.items() if k != "type"})
        except Exception as e:
            log.warning("command processing failed: %s", e)

    def _apply_settings(self, payload: dict[str, Any]) -> None:
        fsm_cfg_keys = (
            "focus_confirm_s",
            "desk_away_s",
            "away_timeout_s",
            "degrading_to_break_s",
            "break_to_idle_s",
        )
        for key, raw in payload.items():
            val = float(raw)
            if key == "pomodoro_duration_s":
                self._pomo_duration_s = val
            elif key == "target_pomodoros":
                self._target_pomodoros = max(1, int(val))
            elif key in fsm_cfg_keys:
                setattr(self.fsm.cfg, key, val)
            else:
                log.warning("unknown setting: %s", key)
                continue
            log.info("setting updated: %s = %s", key, val)

    # ---------------------------------------------------------- apply actions
    async def _apply_actions(self, actions: list[Action]) -> None:
        for a in actions:
            await self._apply_action(a)

    async def _apply_action(self, a: Action) -> None:
        prev_state = self.fsm.state  # already mutated, but ok for logging
        kind = a.kind
        p = a.payload
        if kind == ActionKind.SESSION_START:
            sid = self.db.start_session()
            self.fsm.session_id = sid
            self.db.log_transition(sid, "IDLE", "FOCUS", p.get("trigger", "?"))
            log.info("session %d started", sid)
        elif kind == ActionKind.SESSION_END:
            sid = self.fsm.session_id
            # session_id was cleared inside FSM; pull from action if needed
            # (but FSM clears AFTER emitting SESSION_END payload; we kept it)
            # Use latest non-null id by re-reading.
            # Workaround: stash on payload
            session_id = p.get("session_id") or self._last_known_session_id()
            if session_id:
                self.db.end_session(
                    session_id,
                    p.get("focused_seconds", 0),
                    p.get("total_seconds", 0),
                    p.get("distraction_count", 0),
                    p.get("break_count", 0),
                )
                self.db.log_transition(session_id, prev_state.value, "IDLE", p.get("trigger", "?"))
                log.info("session %d ended", session_id)
            self._last_jpeg = None
            self._pending_jpeg = None
        elif kind == ActionKind.BREAK_START:
            if self.fsm.session_id:
                self.db.log_transition(
                    self.fsm.session_id, "DEGRADING", "BREAK", "break_start"
                )
        elif kind == ActionKind.BREAK_END:
            if self.fsm.session_id:
                self.db.log_transition(self.fsm.session_id, "BREAK", "FOCUS", "break_end")
        elif kind == ActionKind.DISTRACTION_OPEN:
            if self.fsm.session_id:
                image_path = p.get("image_path")
                jpeg = self._pending_jpeg or self._last_jpeg
                if not image_path and jpeg:
                    image_path = self._save_image(jpeg) or None
                event_id = self.db.log_distraction(
                    self.fsm.session_id,
                    p.get("observation", "?"),
                    float(p.get("confidence", 0.0)),
                    image_path,
                )
                self.fsm.open_distraction_id = event_id
                self.db.log_transition(
                    self.fsm.session_id, "FOCUS", "DEGRADING", "distraction"
                )
        elif kind == ActionKind.DISTRACTION_CLOSE:
            eid = p.get("event_id")
            if eid is not None:
                self.db.update_distraction_duration(eid, int(p.get("duration_s", 0)))
        elif kind == ActionKind.BUZZ:
            pattern = p.get("pattern", "chirp")
            await self.serial.buzz(pattern)
        elif kind == ActionKind.LED:
            await self.serial.led(p.get("state", "AWAY"))

    _last_session_id_cache: int | None = None

    def _last_known_session_id(self) -> int | None:
        # ask DB for the most recent open-or-just-closed session
        with self.db._cursor() as cur:  # noqa: SLF001
            cur.execute(
                "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            return row["id"] if row else None

    # --------------------------------------------------- snapshot for dashboard
    def snapshot(self) -> dict[str, Any]:
        s = self.fsm.status()
        return {
            "state": s.state.value,
            "session_id": s.session_id,
            "focused_seconds": s.focused_seconds,
            "total_seconds": s.total_seconds,
            "distraction_count": s.distraction_count,
            "break_count": s.break_count,
            "last_observation": s.last_observation,
            "last_vision_focused": s.last_vision_focused,
            "last_vision_confidence": s.last_vision_confidence,
            "last_distance_cm": s.last_distance_cm,
            "last_environment": s.last_environment,
            "frame": self.last_frame_dict,
            "serial_connected": self.serial.is_connected,
            "camera_online": self.camera.online,
            "vision_enabled": self.vision is not None,
            "started_at": self.start_wall_ts.isoformat(timespec="seconds"),
            "pomodoro_duration_s": self._pomo_duration_s,
            "pomodoro_remaining_s": self._pomo_remaining(),
            "break_duration_s": self.fsm.cfg.break_to_idle_s,
            "target_pomodoros": self._target_pomodoros,
            "pomodoros_completed": s.break_count,
        }


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


