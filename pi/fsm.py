"""Finite state machine: AWAY -> IDLE -> FOCUS <-> DEGRADING -> BREAK.

The FSM is intentionally separate from the orchestrator so it can be unit-tested
without any I/O. It owns only state transition logic, hysteresis bookkeeping,
and accumulated session counters. The orchestrator pushes sensor frames, vision
judgments, button events, and timeouts in; the FSM pushes Actions out.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

log = logging.getLogger("lockin.fsm")


class State(str, Enum):
    AWAY = "AWAY"
    IDLE = "IDLE"
    FOCUS = "FOCUS"
    DEGRADING = "DEGRADING"
    BREAK = "BREAK"


class ActionKind(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BREAK_START = "break_start"
    BREAK_END = "break_end"
    DISTRACTION_OPEN = "distraction_open"   # entered DEGRADING -> log image + obs
    DISTRACTION_CLOSE = "distraction_close" # back to FOCUS -> close the event
    BUZZ = "buzz"
    LED = "led"


@dataclass
class Action:
    kind: ActionKind
    payload: dict = field(default_factory=dict)


@dataclass
class SensorFrame:
    presence: bool
    dist_cm: float | None
    temp_c: float | None
    humidity: float | None
    light: float | None  # raw LDR 0-1023 (higher = brighter)


@dataclass
class VisionInput:
    focused: bool
    confidence: float
    observation: str


@dataclass
class FsmConfig:
    away_to_idle_hold_s: float = 5.0
    away_timeout_s: float = 300.0          # AWAY after no presence this long
    desk_threshold_cm: float = 100.0       # within 1m = at desk
    desk_away_s: float = 30.0              # ultrasonic > threshold this long -> DEGRADING
    focus_confirm_s: float = 60.0          # must look focused continuously to leave DEGRADING
    degrading_to_break_s: float = 120.0    # held in DEGRADING -> BREAK
    break_to_idle_s: float = 600.0         # no presence in BREAK -> IDLE
    vision_confidence_min: float = 0.6     # below this, vision judgment ignored
    distraction_min_close_s: float = 5.0   # debounce against flickery vision


@dataclass
class FsmStatus:
    state: State
    session_id: int | None
    session_start_monotonic: float | None
    focused_seconds: int
    total_seconds: int
    distraction_count: int
    break_count: int
    last_observation: str | None
    last_vision_focused: bool | None
    last_vision_confidence: float | None
    last_distance_cm: float | None
    last_environment: dict | None
    open_distraction_id: int | None


class FocusFsm:
    """Drives all state transitions. Pure logic — no I/O.

    Times use a monotonic clock so the FSM is robust against NTP adjustments
    and can be exercised deterministically from tests by injecting a clock.
    """

    def __init__(
        self,
        cfg: FsmConfig | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.cfg = cfg or FsmConfig()
        self._clock = clock or time.monotonic

        self.state: State = State.AWAY
        # session_id is set by the orchestrator when it gets the real DB row id;
        # the FSM also tracks _session_active so its decisions don't depend on
        # the orchestrator wiring (and so tests work without one).
        self.session_id: int | None = None
        self._session_active: bool = False
        self.session_start_mono: float | None = None
        self.focused_seconds: float = 0.0
        self.total_seconds: float = 0.0
        self.distraction_count: int = 0
        self.break_count: int = 0
        # open_distraction_id is the DB id; the FSM separately tracks whether
        # a distraction is currently open so it can always pair OPEN/CLOSE.
        self.open_distraction_id: int | None = None
        self._distraction_open: bool = False

        # last-known sensor view
        self.last_frame: SensorFrame | None = None
        self.last_frame_t: float | None = None
        self.last_presence_t: float | None = None
        self.last_at_desk_t: float | None = None
        self.last_away_from_desk_t: float | None = None
        # set when the user returns to the desk after being away; used to
        # recover from DEGRADING when the trigger was desk_away (ultrasonic).
        self._desk_return_t: float | None = None

        # hysteresis trackers
        self._away_idle_candidate_since: float | None = None
        self._focus_recovery_since: float | None = None
        self._degrading_since: float | None = None
        self._degrading_trigger: str | None = None
        self._break_no_presence_since: float | None = None
        self._last_tick_t: float | None = None

        # vision view
        self.last_vision: VisionInput | None = None
        self.last_vision_t: float | None = None

    # ------------------------------------------------------------------ status
    def status(self) -> FsmStatus:
        env = None
        if self.last_frame:
            env = {
                "temp_c": self.last_frame.temp_c,
                "humidity": self.last_frame.humidity,
                "light": self.last_frame.light,
            }
        return FsmStatus(
            state=self.state,
            session_id=self.session_id,
            session_start_monotonic=self.session_start_mono,
            focused_seconds=int(self.focused_seconds),
            total_seconds=int(self.total_seconds),
            distraction_count=self.distraction_count,
            break_count=self.break_count,
            last_observation=self.last_vision.observation if self.last_vision else None,
            last_vision_focused=self.last_vision.focused if self.last_vision else None,
            last_vision_confidence=(
                self.last_vision.confidence if self.last_vision else None
            ),
            last_distance_cm=self.last_frame.dist_cm if self.last_frame else None,
            last_environment=env,
            open_distraction_id=self.open_distraction_id,
        )

    # ------------------------------------------------------------------- ticks
    def tick(self) -> list[Action]:
        """Time-driven transitions and counter accumulation.
        Call at ~1Hz from the orchestrator."""
        actions: list[Action] = []
        now = self._clock()

        # accumulate session counters
        if self._session_active and self._last_tick_t is not None:
            dt = max(0.0, now - self._last_tick_t)
            self.total_seconds += dt
            if self.state == State.FOCUS:
                self.focused_seconds += dt
        self._last_tick_t = now

        # AWAY -> IDLE confirmation hold
        if (
            self.state == State.AWAY
            and self._away_idle_candidate_since is not None
            and now - self._away_idle_candidate_since >= self.cfg.away_to_idle_hold_s
        ):
            actions += self._transition(State.IDLE, trigger="presence_hold")
            self._away_idle_candidate_since = None

        # IDLE/FOCUS/DEGRADING -> AWAY when no presence for a long time
        if (
            self.state in (State.IDLE, State.FOCUS, State.DEGRADING, State.BREAK)
            and self.last_presence_t is not None
            and now - self.last_presence_t >= self.cfg.away_timeout_s
            and self.state != State.AWAY
        ):
            if self._session_active:
                # close session quietly — long absence
                actions += self._end_session(trigger="away_timeout")
            actions += self._transition(State.AWAY, trigger="away_timeout")

        # FOCUS -> DEGRADING via desk-away (ultrasonic)
        if self.state == State.FOCUS and self.last_away_from_desk_t is not None:
            if now - self.last_away_from_desk_t >= self.cfg.desk_away_s:
                actions += self._enter_degrading(
                    trigger="desk_away",
                    observation="user left the desk",
                    confidence=0.95,
                    image_path=None,
                )

        # DEGRADING -> FOCUS via at-desk return (mirrors desk_away entry).
        # Only when DEGRADING was triggered by desk_away — vision-triggered
        # distractions still require a vision-confirmed recovery.
        if (
            self.state == State.DEGRADING
            and self._degrading_trigger == "desk_away"
            and self._desk_return_t is not None
            and self._focus_recovery_since is None
        ):
            self._focus_recovery_since = self._desk_return_t

        # DEGRADING -> FOCUS via sustained recovery (must hold focus_confirm_s)
        if (
            self.state == State.DEGRADING
            and self._focus_recovery_since is not None
            and now - self._focus_recovery_since >= self.cfg.focus_confirm_s
        ):
            actions += self._transition(State.FOCUS, trigger="focus_confirmed")
            actions += self._close_distraction(now)
            self._focus_recovery_since = None

        # DEGRADING -> BREAK on long hold
        if (
            self.state == State.DEGRADING
            and self._degrading_since is not None
            and now - self._degrading_since >= self.cfg.degrading_to_break_s
        ):
            actions += self._enter_break(trigger="degrading_timeout")

        # BREAK -> IDLE on long no-presence
        if (
            self.state == State.BREAK
            and self._break_no_presence_since is not None
            and now - self._break_no_presence_since >= self.cfg.break_to_idle_s
        ):
            actions += self._end_session(trigger="break_timeout")
            actions += self._transition(State.IDLE, trigger="break_timeout")
            self._break_no_presence_since = None

        return actions

    # --------------------------------------------------------- sensor ingestion
    def on_sensor_frame(self, frame: SensorFrame) -> list[Action]:
        actions: list[Action] = []
        now = self._clock()
        self.last_frame = frame
        self.last_frame_t = now

        if frame.presence:
            self.last_presence_t = now
            self._break_no_presence_since = None
        else:
            if self.state == State.BREAK and self._break_no_presence_since is None:
                self._break_no_presence_since = now

        # desk presence: in-range echo = at desk; anything else (no echo or
        # echo beyond threshold) = away. The Arduino emits null when pulseIn
        # times out, which only happens when nothing's within ~4m of the
        # sensor — treating it as away lets us react immediately instead of
        # waiting for an arbitrary far-distance reading.
        at_desk = (
            frame.dist_cm is not None
            and frame.dist_cm > 0
            and frame.dist_cm <= self.cfg.desk_threshold_cm
        )
        if at_desk:
            was_away_from_desk = self.last_away_from_desk_t is not None
            self.last_at_desk_t = now
            self.last_away_from_desk_t = None
            if was_away_from_desk and self._desk_return_t is None:
                self._desk_return_t = now
            # candidate for AWAY -> IDLE
            if self.state == State.AWAY and self._away_idle_candidate_since is None:
                self._away_idle_candidate_since = now
        else:
            if self.last_away_from_desk_t is None:
                self.last_away_from_desk_t = now
            # left the desk again — cancel any in-flight recovery
            self._desk_return_t = None
            if self._degrading_trigger == "desk_away":
                self._focus_recovery_since = None
            # broke the candidate window
            if self.state == State.AWAY:
                self._away_idle_candidate_since = None

        return actions

    def on_pir(self) -> list[Action]:
        now = self._clock()
        self.last_presence_t = now
        if self.state == State.AWAY and self._away_idle_candidate_since is None:
            # require ultrasonic to confirm desk presence — PIR alone doesn't promote
            pass
        return []

    # ----------------------------------------------------------- vision input
    def on_vision(self, v: VisionInput) -> list[Action]:
        actions: list[Action] = []
        now = self._clock()
        self.last_vision = v
        self.last_vision_t = now

        if v.confidence < self.cfg.vision_confidence_min:
            return actions  # too low-confidence, ignore

        if self.state == State.FOCUS and not v.focused:
            actions += self._enter_degrading(
                trigger="vision_not_focused",
                observation=v.observation,
                confidence=v.confidence,
                image_path=None,  # filled by orchestrator
            )
            return actions

        if self.state == State.DEGRADING:
            if v.focused:
                # require at-desk too before crediting recovery, but tolerate
                # brief ultrasonic echo gaps (dist=None when nothing's in the
                # cone for one tick) by accepting a recent at-desk timestamp.
                at_desk_now = (
                    self.last_frame is not None
                    and self.last_frame.dist_cm is not None
                    and self.last_frame.dist_cm > 0
                    and self.last_frame.dist_cm <= self.cfg.desk_threshold_cm
                )
                at_desk_recent = (
                    self.last_at_desk_t is not None
                    and now - self.last_at_desk_t <= 10.0
                )
                if at_desk_now or at_desk_recent:
                    if self._focus_recovery_since is None:
                        self._focus_recovery_since = now
                else:
                    self._focus_recovery_since = None
            else:
                # still distracted, reset recovery timer
                self._focus_recovery_since = None

        return actions

    # ----------------------------------------------------------- button input
    def on_button(self, action: str) -> list[Action]:
        actions: list[Action] = []
        if action == "single":
            if self.state == State.IDLE:
                actions += self._start_session(trigger="button_single")
            elif self.state == State.BREAK:
                actions += self._transition(State.FOCUS, trigger="button_resume")
                actions.append(Action(ActionKind.BREAK_END))
                self._break_no_presence_since = None
            elif self.state == State.AWAY:
                # treat as "I'm here, start"
                actions += self._transition(State.IDLE, trigger="button_wake")
                actions += self._start_session(trigger="button_single")
        elif action == "double":
            if self.state in (State.FOCUS, State.DEGRADING):
                actions += self._enter_break(trigger="button_double")
        elif action == "long":
            if self._session_active:
                actions += self._end_session(trigger="button_long")
            if self.state != State.IDLE:
                actions += self._transition(State.IDLE, trigger="button_long")
        return actions

    # ----------------------------------------------------- internal transitions
    def _transition(self, to: State, *, trigger: str) -> list[Action]:
        if to == self.state:
            return []
        log.info("FSM: %s -> %s (%s)", self.state.value, to.value, trigger)
        out = [
            Action(
                ActionKind.BUZZ,
                {"pattern": _buzz_for_transition(self.state, to)},
            ),
            Action(ActionKind.LED, {"state": to.value}),
        ]
        # transition-side bookkeeping
        prev = self.state
        self.state = to
        if prev == State.DEGRADING:
            self._degrading_trigger = None
            self._focus_recovery_since = None
        if to == State.IDLE:
            self._away_idle_candidate_since = None
        return out

    def _start_session(self, *, trigger: str) -> list[Action]:
        now = self._clock()
        self._session_active = True
        self.session_start_mono = now
        self.focused_seconds = 0.0
        self.total_seconds = 0.0
        self.distraction_count = 0
        self.break_count = 0
        actions: list[Action] = [Action(ActionKind.SESSION_START, {"trigger": trigger})]
        actions += self._transition(State.FOCUS, trigger=trigger)
        return actions

    def _end_session(self, *, trigger: str) -> list[Action]:
        actions: list[Action] = []
        actions += self._close_distraction(self._clock())
        actions.append(
            Action(
                ActionKind.SESSION_END,
                {
                    "trigger": trigger,
                    "session_id": self.session_id,
                    "focused_seconds": int(self.focused_seconds),
                    "total_seconds": int(self.total_seconds),
                    "distraction_count": self.distraction_count,
                    "break_count": self.break_count,
                },
            )
        )
        self.session_id = None
        self._session_active = False
        self.session_start_mono = None
        return actions

    def _close_distraction(self, now: float) -> list[Action]:
        if not self._distraction_open:
            return []
        duration = int(now - self._degrading_since) if self._degrading_since else 0
        payload = {"event_id": self.open_distraction_id, "duration_s": duration}
        self._distraction_open = False
        self.open_distraction_id = None
        self._degrading_since = None
        return [Action(ActionKind.DISTRACTION_CLOSE, payload)]

    def _enter_degrading(
        self, *, trigger: str, observation: str, confidence: float, image_path: str | None
    ) -> list[Action]:
        actions: list[Action] = []
        actions += self._transition(State.DEGRADING, trigger=trigger)
        self._degrading_since = self._clock()
        self._degrading_trigger = trigger
        self._focus_recovery_since = None
        self.distraction_count += 1
        self._distraction_open = True
        actions.append(
            Action(
                ActionKind.DISTRACTION_OPEN,
                {
                    "observation": observation,
                    "confidence": confidence,
                    "image_path": image_path,
                },
            )
        )
        return actions

    def _enter_break(self, *, trigger: str) -> list[Action]:
        actions: list[Action] = []
        actions += self._transition(State.BREAK, trigger=trigger)
        self.break_count += 1
        focused_min = int(self.focused_seconds // 60)
        actions.append(Action(ActionKind.BREAK_START, {"focused_minutes": focused_min}))
        actions += self._close_distraction(self._clock())
        return actions


def _buzz_for_transition(prev: State, to: State) -> str:
    if to == State.FOCUS and prev in (State.IDLE, State.AWAY):
        return "confirm"
    if to == State.DEGRADING:
        return "chirp"
    if to == State.BREAK:
        return "alert"
    if to == State.IDLE:
        return "chirp"
    return "chirp"
