"""Smoke tests for the FSM — no hardware required.

Run from pi/: python -m unittest test_fsm.py
"""
from __future__ import annotations

import unittest

from fsm import (
    ActionKind,
    FocusFsm,
    FsmConfig,
    SensorFrame,
    State,
    VisionInput,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _kinds(actions) -> list[ActionKind]:
    return [a.kind for a in actions]


class FsmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.fsm = FocusFsm(
            FsmConfig(
                away_to_idle_hold_s=5,
                desk_threshold_cm=100,
                desk_away_s=30,
                focus_confirm_s=60,
                degrading_to_break_s=120,
                break_to_idle_s=600,
                away_timeout_s=300,
                vision_confidence_min=0.6,
            ),
            clock=self.clock,
        )

    def _frame(self, presence=True, dist_cm=60.0):
        return SensorFrame(presence=presence, dist_cm=dist_cm, temp_c=22.0,
                           humidity=50.0, light=400)

    def test_away_to_idle_requires_hold(self):
        self.assertEqual(self.fsm.state, State.AWAY)
        self.fsm.on_sensor_frame(self._frame())  # at desk
        self.clock.advance(2.0)
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.AWAY)  # not enough hold yet
        self.clock.advance(5.0)
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.IDLE)

    def test_button_starts_session(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0)
        self.fsm.tick()
        actions = self.fsm.on_button("single")
        self.assertEqual(self.fsm.state, State.FOCUS)
        self.assertIn(ActionKind.SESSION_START, _kinds(actions))

    def test_vision_distraction_then_recovery(self):
        # set up focus
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.state, State.FOCUS)

        # distracted vision -> degrading
        actions = self.fsm.on_vision(VisionInput(False, 0.9, "phone in hand"))
        self.assertEqual(self.fsm.state, State.DEGRADING)
        self.assertIn(ActionKind.DISTRACTION_OPEN, _kinds(actions))

        # focused vision starts recovery timer
        self.fsm.on_sensor_frame(self._frame())  # at desk
        self.fsm.on_vision(VisionInput(True, 0.9, "looking at screen"))
        self.clock.advance(70.0)
        self.fsm.on_sensor_frame(self._frame())
        actions = self.fsm.tick()
        self.assertEqual(self.fsm.state, State.FOCUS)
        self.assertIn(ActionKind.DISTRACTION_CLOSE, _kinds(actions))

    def test_low_confidence_vision_ignored(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.fsm.on_vision(VisionInput(False, 0.3, "blurry"))
        self.assertEqual(self.fsm.state, State.FOCUS)

    def test_double_press_triggers_break(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.state, State.FOCUS)
        actions = self.fsm.on_button("double")
        self.assertEqual(self.fsm.state, State.BREAK)
        self.assertIn(ActionKind.BREAK_START, _kinds(actions))

    def test_break_count_increments_per_break(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.break_count, 0)
        self.fsm.on_button("double")
        self.assertEqual(self.fsm.break_count, 1)
        self.fsm.on_button("single")  # resume from break
        self.assertEqual(self.fsm.state, State.FOCUS)
        self.fsm.on_button("double")
        self.assertEqual(self.fsm.break_count, 2)
        # ending the session emits the count
        actions = self.fsm.on_button("long")
        end = next(a for a in actions if a.kind == ActionKind.SESSION_END)
        self.assertEqual(end.payload["break_count"], 2)
        # next session resets
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.break_count, 0)

    def test_long_press_ends_session(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        actions = self.fsm.on_button("long")
        self.assertEqual(self.fsm.state, State.IDLE)
        self.assertIn(ActionKind.SESSION_END, _kinds(actions))

    def test_desk_away_triggers_degrading(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.state, State.FOCUS)
        # walk away
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.clock.advance(31.0)
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.DEGRADING)

    def test_null_distance_treated_as_away_from_desk(self):
        # focus, then ultrasonic returns null (no echo) — should still trip desk_away
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.state, State.FOCUS)
        self.fsm.on_sensor_frame(self._frame(dist_cm=None))
        self.clock.advance(31.0)
        self.fsm.on_sensor_frame(self._frame(dist_cm=None))
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.DEGRADING)

    def test_desk_return_recovers_from_degrading(self):
        # focus -> walk away -> DEGRADING via desk_away
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.assertEqual(self.fsm.state, State.FOCUS)
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.clock.advance(31.0)
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.DEGRADING)

        # walk back; ultrasonic alone (no vision) should recover after focus_confirm_s
        self.fsm.on_sensor_frame(self._frame(dist_cm=60.0))
        self.clock.advance(61.0)
        self.fsm.on_sensor_frame(self._frame(dist_cm=60.0))
        actions = self.fsm.tick()
        self.assertEqual(self.fsm.state, State.FOCUS)
        self.assertIn(ActionKind.DISTRACTION_CLOSE, _kinds(actions))

    def test_desk_return_does_not_recover_vision_triggered_degrading(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        # vision says distracted while still at desk
        self.fsm.on_vision(VisionInput(False, 0.9, "phone in hand"))
        self.assertEqual(self.fsm.state, State.DEGRADING)
        # sitting at desk alone shouldn't recover — vision drove this
        self.clock.advance(70.0)
        self.fsm.on_sensor_frame(self._frame())
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.DEGRADING)

    def test_desk_return_recovery_cancels_if_user_leaves_again(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.clock.advance(31.0)
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.fsm.tick()
        self.assertEqual(self.fsm.state, State.DEGRADING)

        # come back briefly
        self.fsm.on_sensor_frame(self._frame(dist_cm=60.0))
        self.clock.advance(5.0)
        # leave again before focus_confirm_s elapses
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.clock.advance(70.0)
        self.fsm.on_sensor_frame(self._frame(dist_cm=200.0))
        self.fsm.tick()
        # should NOT have flipped to FOCUS just because focus_confirm_s elapsed
        # since the original brief return
        self.assertEqual(self.fsm.state, State.DEGRADING)

    def test_degrading_to_break_on_timeout(self):
        self.fsm.on_sensor_frame(self._frame())
        self.clock.advance(6.0); self.fsm.tick()
        self.fsm.on_button("single")
        self.fsm.on_vision(VisionInput(False, 0.9, "head down"))
        self.assertEqual(self.fsm.state, State.DEGRADING)
        self.clock.advance(125.0)
        actions = self.fsm.tick()
        self.assertEqual(self.fsm.state, State.BREAK)
        self.assertIn(ActionKind.BREAK_START, _kinds(actions))


if __name__ == "__main__":
    unittest.main()
