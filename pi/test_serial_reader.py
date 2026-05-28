"""Tests for SerialReader auto-reconnect, queue overflow, and parse robustness.

No real serial port is touched — `serial_asyncio.open_serial_connection` is
patched to return an in-memory asyncio.StreamReader that the test feeds.

Run from pi/: python -m unittest test_serial_reader.py
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import serial_reader
from serial_reader import SerialReader


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("writer closed")
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _make_reader() -> asyncio.StreamReader:
    return asyncio.StreamReader()


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll a predicate until it returns truthy or timeout. Returns True if
    the predicate became truthy, False on timeout — beats hard-coded sleeps."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


class SerialReaderTest(unittest.IsolatedAsyncioTestCase):

    async def _start_reader(self, connections: list[tuple[asyncio.StreamReader, _FakeWriter]],
                            *, reconnect_delay_s: float = 0.02,
                            queue_size: int = 128) -> tuple[SerialReader, asyncio.Task, asyncio.Queue]:
        """Spin up a SerialReader whose open_serial_connection returns the
        pre-built (reader, writer) pairs in order, then blocks forever."""

        async def fake_open(*args, **kwargs):
            if connections:
                return connections.pop(0)
            # Future connections: just block so the test can decide when to stop.
            never = asyncio.get_running_loop().create_future()
            await never

        q: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        sr = SerialReader("/dev/null", 115200, q, reconnect_delay_s=reconnect_delay_s)
        patcher = mock.patch.object(
            serial_reader.serial_asyncio, "open_serial_connection",
            side_effect=fake_open,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        task = asyncio.create_task(sr.run())
        self.addAsyncCleanup(self._teardown, sr, task)
        return sr, task, q

    async def _teardown(self, sr: SerialReader, task: asyncio.Task) -> None:
        await sr.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ----------------------------------------------------- happy path
    async def test_parses_one_frame(self):
        reader = _make_reader()
        sr, _, q = await self._start_reader([(reader, _FakeWriter())])
        await _wait_until(lambda: sr.is_connected)
        self.assertTrue(sr.is_connected)

        reader.feed_data(b'{"type":"frame","presence":true}\n')
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(msg["type"], "frame")
        self.assertTrue(msg["presence"])

    # ----------------------------------------------------- reconnect
    async def test_eof_triggers_reconnect(self):
        reader1 = _make_reader()
        reader2 = _make_reader()
        writer1, writer2 = _FakeWriter(), _FakeWriter()
        sr, _, q = await self._start_reader(
            [(reader1, writer1), (reader2, writer2)],
            reconnect_delay_s=0.02,
        )
        await _wait_until(lambda: sr.is_connected)

        # First frame on connection #1
        reader1.feed_data(b'{"type":"hello","fw":"first"}\n')
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(msg["fw"], "first")

        # Simulate Arduino unplugged -> EOF
        reader1.feed_eof()
        await _wait_until(lambda: not sr.is_connected)
        self.assertFalse(sr.is_connected)

        # Wait for reconnection to second fake serial
        connected_again = await _wait_until(lambda: sr.is_connected, timeout=2.0)
        self.assertTrue(connected_again, "did not reconnect after EOF")

        reader2.feed_data(b'{"type":"hello","fw":"second"}\n')
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(msg["fw"], "second")

    async def test_connect_failure_then_recovery(self):
        # First open raises -> reader retries -> second open succeeds.
        good_reader = _make_reader()
        good_writer = _FakeWriter()
        attempts = {"n": 0}

        async def fake_open(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("port busy")
            return good_reader, good_writer

        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        sr = SerialReader("/dev/null", 115200, q, reconnect_delay_s=0.02)
        with mock.patch.object(
            serial_reader.serial_asyncio, "open_serial_connection",
            side_effect=fake_open,
        ):
            task = asyncio.create_task(sr.run())
            try:
                connected = await _wait_until(lambda: sr.is_connected, timeout=2.0)
                self.assertTrue(connected, "should recover after first failure")
                self.assertGreaterEqual(attempts["n"], 2)
            finally:
                await sr.stop()
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ----------------------------------------------------- parse robustness
    async def test_bad_json_skipped_without_crash(self):
        reader = _make_reader()
        sr, _, q = await self._start_reader([(reader, _FakeWriter())])
        await _wait_until(lambda: sr.is_connected)

        reader.feed_data(b'not-json-at-all\n')
        reader.feed_data(b'{"type":"frame","presence":false}\n')
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(msg["type"], "frame")
        self.assertTrue(q.empty(), "garbage line should have been dropped, not queued")

    async def test_bad_utf8_skipped_without_crash(self):
        reader = _make_reader()
        sr, _, q = await self._start_reader([(reader, _FakeWriter())])
        await _wait_until(lambda: sr.is_connected)

        # Invalid UTF-8 sequence then a clean message
        reader.feed_data(b'\xff\xfe\xfd\n')
        reader.feed_data(b'{"type":"event","event":"pir"}\n')
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(msg["event"], "pir")

    # ----------------------------------------------------- queue overflow
    async def test_queue_overflow_drops_oldest(self):
        reader = _make_reader()
        sr, _, q = await self._start_reader(
            [(reader, _FakeWriter())], queue_size=3,
        )
        await _wait_until(lambda: sr.is_connected)

        # Push 5 frames into a queue of size 3 — first two should be dropped.
        for i in range(5):
            reader.feed_data(json.dumps({"type": "frame", "seq": i}).encode() + b'\n')

        # Wait for the reader to fully drain the wire
        await _wait_until(lambda: q.qsize() == 3, timeout=1.0)
        self.assertEqual(q.qsize(), 3)

        seqs = []
        while not q.empty():
            seqs.append((await q.get())["seq"])
        # Newest three survived; oldest two were dropped.
        self.assertEqual(seqs, [2, 3, 4])

    # ----------------------------------------------------- writer commands
    async def test_buzz_command_serialised(self):
        reader = _make_reader()
        writer = _FakeWriter()
        sr, _, _ = await self._start_reader([(reader, writer)])
        await _wait_until(lambda: sr.is_connected)

        await sr.buzz("chirp")
        self.assertTrue(writer.written, "buzz command should have written bytes")
        decoded = json.loads(writer.written[0].decode().rstrip())
        self.assertEqual(decoded, {"cmd": "buzz", "pattern": "chirp"})

    async def test_led_command_serialised(self):
        reader = _make_reader()
        writer = _FakeWriter()
        sr, _, _ = await self._start_reader([(reader, writer)])
        await _wait_until(lambda: sr.is_connected)

        await sr.led("FOCUS")
        decoded = json.loads(writer.written[0].decode().rstrip())
        self.assertEqual(decoded, {"cmd": "led", "state": "FOCUS"})

    async def test_send_command_silent_when_disconnected(self):
        # No (reader, writer) provided -> stays disconnected. send_command
        # should not raise.
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        sr = SerialReader("/dev/null", 115200, q, reconnect_delay_s=10.0)
        await sr.buzz("chirp")  # must not raise


if __name__ == "__main__":
    unittest.main()
