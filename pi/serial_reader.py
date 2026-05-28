"""Reads newline-delimited JSON frames/events from the Arduino over USB serial.

Auto-reconnects on disconnect. Pushes parsed messages onto an asyncio.Queue.
Also exposes a coroutine to send commands back (buzzer patterns, ping).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import serial_asyncio

log = logging.getLogger("lockin.serial")


class SerialReader:
    def __init__(
        self,
        port: str,
        baud: int,
        out_queue: asyncio.Queue[dict[str, Any]],
        reconnect_delay_s: float = 5.0,
    ):
        self.port = port
        self.baud = baud
        self.queue = out_queue
        self.reconnect_delay_s = reconnect_delay_s
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self.last_frame_ts: float | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def send_command(self, cmd: dict[str, Any]) -> None:
        if not self._writer:
            log.debug("send_command dropped (no writer): %s", cmd)
            return
        try:
            payload = (json.dumps(cmd) + "\n").encode("utf-8")
            self._writer.write(payload)
            await self._writer.drain()
        except (ConnectionError, OSError) as e:
            log.warning("serial write failed: %s", e)

    async def buzz(self, pattern: str) -> None:
        await self.send_command({"cmd": "buzz", "pattern": pattern})

    async def led(self, state: str) -> None:
        await self.send_command({"cmd": "led", "state": state})

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                reader, writer = await serial_asyncio.open_serial_connection(
                    url=self.port, baudrate=self.baud
                )
                self._writer = writer
                self._connected.set()
                log.info("serial connected: %s @ %d", self.port, self.baud)
                await self._read_loop(reader)
            except Exception as e:
                log.warning("serial connect failed: %s", e)
            finally:
                self._connected.clear()
                self._writer = None
                if not self._stop.is_set():
                    log.info("serial reconnecting in %.1fs", self.reconnect_delay_s)
                    await asyncio.sleep(self.reconnect_delay_s)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stop.is_set():
            try:
                line = await reader.readline()
            except Exception as e:
                log.warning("serial read failed: %s", e)
                return
            if not line:
                # EOF -> Arduino unplugged
                log.warning("serial EOF")
                return
            try:
                msg = json.loads(line.decode("utf-8", errors="ignore").strip())
            except json.JSONDecodeError:
                log.debug("non-json from arduino: %r", line)
                continue
            self.last_frame_ts = asyncio.get_running_loop().time()
            try:
                self.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # drop oldest, push newest — sensor frames are time-sensitive
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.queue.put_nowait(msg)
