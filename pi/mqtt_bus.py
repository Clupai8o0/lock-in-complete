"""Async MQTT pub/sub for orchestrator <-> dashboard.

Replaces file-based snapshot.json + cmd.json IPC with a real pub/sub channel
so the dashboard can update sub-second instead of waiting for the next poll.

Design choices:
- aiomqtt 2.x is used because the orchestrator is asyncio-native.
- The bus survives broker outages: if mosquitto goes down the orchestrator
  keeps running, publishes are silently dropped, and the bus reconnects on
  its own (every `retry_s`). The file-based snapshot writer in main.py acts
  as the offline fallback so the dashboard still works when MQTT is down.
- snapshot is published with retain=True so a freshly connected dashboard
  sees the current state immediately, not after the next 1Hz tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiomqtt

log = logging.getLogger("lockin.mqtt")

OnMessage = Callable[[str, dict[str, Any]], Awaitable[None]]


class MqttBus:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: str = "lockin-orchestrator",
        retry_s: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.retry_s = retry_s
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._on_message: OnMessage | None = None
        self._subscriptions: list[str] = []

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on_message(self, cb: OnMessage) -> None:
        self._on_message = cb

    def subscribe(self, topic: str) -> None:
        if topic not in self._subscriptions:
            self._subscriptions.append(topic)

    async def publish(self, topic: str, payload: dict[str, Any], retain: bool = False) -> None:
        if not self._client or not self._connected.is_set():
            return  # graceful degradation: broker down -> drop, file fallback covers it
        body = json.dumps(payload, default=str)
        try:
            await self._client.publish(topic, body, retain=retain)
        except aiomqtt.MqttError as e:
            log.warning("mqtt publish failed (%s): %s", topic, e)

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("mqtt bus starting (%s:%d)", self.host, self.port)
        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    self.host, self.port, identifier=self.client_id,
                ) as client:
                    self._client = client
                    self._connected.set()
                    log.info("mqtt connected: %s:%d", self.host, self.port)
                    for topic in self._subscriptions:
                        await client.subscribe(topic)
                    async for msg in client.messages:
                        if not self._on_message:
                            continue
                        try:
                            payload = json.loads(msg.payload.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            log.warning("dropping malformed mqtt msg on %s", msg.topic)
                            continue
                        if not isinstance(payload, dict):
                            log.warning("dropping non-dict mqtt payload on %s", msg.topic)
                            continue
                        try:
                            await self._on_message(str(msg.topic), payload)
                        except Exception as e:
                            log.warning("mqtt handler raised: %s", e)
            except aiomqtt.MqttError as e:
                log.warning("mqtt disconnected: %s (retry in %.1fs)", e, self.retry_s)
            except Exception as e:
                log.exception("mqtt loop crashed: %s", e)
            finally:
                self._client = None
                self._connected.clear()
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.retry_s)
                except asyncio.TimeoutError:
                    pass
        log.info("mqtt bus stopped")
