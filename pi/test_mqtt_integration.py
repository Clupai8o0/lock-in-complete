"""End-to-end MQTT round-trip test: orchestrator bus <-> dashboard bridge.

Requires a broker on 127.0.0.1:1883 (mosquitto). Skipped automatically when
none is reachable, so it never breaks a CI box without a broker.

Run from pi/: python -m unittest test_mqtt_integration.py
"""
from __future__ import annotations

import asyncio
import queue
import socket
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "dashboard"))

from mqtt_bus import MqttBus  # noqa: E402
from mqtt_bridge import MqttBridge  # noqa: E402

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883


def _broker_up() -> bool:
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=1.0):
            return True
    except OSError:
        return False


async def _await_true(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


@unittest.skipUnless(_broker_up(), "no MQTT broker on 127.0.0.1:1883")
class MqttRoundTripTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Unique topics per run so retained messages from previous runs
        # can't leak into assertions.
        run = uuid.uuid4().hex[:8]
        self.topic_snap = f"lockin/test/{run}/snapshot"
        self.topic_cmd = f"lockin/test/{run}/cmd"

        self.received_cmds: list[tuple[str, dict]] = []

        self.bus = MqttBus(BROKER_HOST, BROKER_PORT, client_id=f"test-orch-{run}")
        self.bus.subscribe(self.topic_cmd)

        async def on_msg(topic, payload):
            self.received_cmds.append((topic, payload))

        self.bus.on_message(on_msg)
        self.bus_task = asyncio.create_task(self.bus.run())

        self.bridge = MqttBridge(
            BROKER_HOST, BROKER_PORT,
            topic_snapshot=self.topic_snap, topic_cmd=self.topic_cmd,
            client_id=f"test-dash-{run}",
        )
        self.bridge.start()

        self.assertTrue(await _await_true(lambda: self.bus.connected), "bus never connected")
        self.assertTrue(await _await_true(lambda: self.bridge.connected), "bridge never connected")

    async def asyncTearDown(self) -> None:
        # Clear the retained snapshot so it doesn't linger in the broker.
        await self.bus.publish(self.topic_snap, {}, retain=True)
        self.bridge.stop()
        await self.bus.stop()
        self.bus_task.cancel()
        try:
            await self.bus_task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_snapshot_flows_bus_to_bridge(self):
        await self.bus.publish(self.topic_snap, {"state": "FOCUS", "seq": 1}, retain=True)
        got = await _await_true(
            lambda: self.bridge.latest_snapshot is not None
            and self.bridge.latest_snapshot.get("seq") == 1
        )
        self.assertTrue(got, "bridge never received the published snapshot")
        self.assertEqual(self.bridge.latest_snapshot["state"], "FOCUS")

    async def test_command_flows_bridge_to_bus(self):
        ok = self.bridge.publish_command({"type": "button", "action": "double"})
        self.assertTrue(ok, "publish_command reported failure")
        got = await _await_true(lambda: len(self.received_cmds) > 0)
        self.assertTrue(got, "bus never received the command")
        topic, payload = self.received_cmds[0]
        self.assertEqual(topic, self.topic_cmd)
        self.assertEqual(payload["action"], "double")

    async def test_sse_listener_receives_updates(self):
        q: queue.Queue = queue.Queue(maxsize=4)
        self.bridge.add_listener(q)
        await self.bus.publish(self.topic_snap, {"state": "BREAK", "seq": 99})

        # The listener queue is fed from paho's network thread; poll for it.
        def _has_seq_99() -> bool:
            try:
                while True:
                    item = q.get_nowait()
                    if item.get("seq") == 99:
                        return True
            except queue.Empty:
                return False

        got = await _await_true(_has_seq_99)
        self.assertTrue(got, "SSE listener never got the update")
        self.bridge.remove_listener(q)

    async def test_retained_snapshot_seen_by_late_subscriber(self):
        # Publish retained, THEN connect a fresh bridge — it should get the
        # last snapshot immediately (this is what makes a page reload instant).
        await self.bus.publish(self.topic_snap, {"state": "IDLE", "seq": 7}, retain=True)
        late = MqttBridge(
            BROKER_HOST, BROKER_PORT,
            topic_snapshot=self.topic_snap, topic_cmd=self.topic_cmd,
            client_id="test-dash-late",
        )
        late.start()
        try:
            got = await _await_true(
                lambda: late.latest_snapshot is not None
                and late.latest_snapshot.get("seq") == 7
            )
            self.assertTrue(got, "late subscriber didn't get retained snapshot")
        finally:
            late.stop()


if __name__ == "__main__":
    unittest.main()
