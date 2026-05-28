"""Dashboard-side MQTT bridge.

paho-mqtt runs in its own background thread (Flask is sync); on every
snapshot arriving on `lockin/snapshot` it caches the latest payload and
fans out to any SSE listeners registered by browser clients.

If the broker is unreachable, `start()` still returns and the dashboard
falls back to reading snapshot.json (graceful degradation).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Callable

import paho.mqtt.client as mqtt

log = logging.getLogger("lockin.dashboard.mqtt")


class MqttBridge:
    def __init__(
        self,
        host: str,
        port: int,
        topic_snapshot: str,
        topic_cmd: str,
        client_id: str = "lockin-dashboard",
    ):
        self.host = host
        self.port = port
        self.topic_snapshot = topic_snapshot
        self.topic_cmd = topic_cmd

        self.latest_snapshot: dict | None = None
        self.connected = False
        self._listeners: set[queue.Queue] = set()
        self._lock = threading.Lock()

        self._client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        # Built-in reconnect with exponential backoff (1s -> 30s).
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ----------------------------------------------------- lifecycle
    def start(self) -> None:
        try:
            self._client.connect_async(self.host, self.port, keepalive=30)
        except OSError as e:
            log.warning("mqtt connect_async failed: %s (will retry in background)", e)
        # loop_start spawns the network thread immediately; reconnects happen there.
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------- callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            log.info("dashboard mqtt connected (%s:%d)", self.host, self.port)
            client.subscribe(self.topic_snapshot, qos=0)
        else:
            log.warning("dashboard mqtt connect refused: %s", reason_code)

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        log.warning("dashboard mqtt disconnected")

    def _on_message(self, client, userdata, msg):
        if msg.topic != self.topic_snapshot:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("dashboard mqtt dropped malformed snapshot")
            return
        if not isinstance(payload, dict):
            return

        stale: list[queue.Queue] = []
        with self._lock:
            self.latest_snapshot = payload
            for q in self._listeners:
                try:
                    # drop oldest in this listener's queue if full
                    if q.full():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                    q.put_nowait(payload)
                except Exception:  # noqa: BLE001
                    stale.append(q)
            for q in stale:
                self._listeners.discard(q)

    # ----------------------------------------------------- publish
    def publish_command(self, cmd: dict) -> bool:
        info = self._client.publish(self.topic_cmd, json.dumps(cmd), qos=0)
        try:
            info.wait_for_publish(timeout=2.0)
        except (RuntimeError, ValueError):
            return False
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # ----------------------------------------------------- listeners
    def add_listener(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.add(q)
            if self.latest_snapshot:
                try:
                    q.put_nowait(self.latest_snapshot)
                except queue.Full:
                    pass

    def remove_listener(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)
