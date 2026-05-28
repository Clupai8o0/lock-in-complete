"""Lock-In orchestrator entry point.

Runs the asyncio event loop with serial reader, FSM, vision worker, and
a snapshot file shared with the Flask dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import config
from orchestrator import Orchestrator


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


async def snapshot_writer(orch: Orchestrator, path: Path, stop: asyncio.Event) -> None:
    """Writes a JSON snapshot every second so the Flask dashboard can show
    live state without poking into asyncio internals."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    while not stop.is_set():
        try:
            tmp.write_text(json.dumps(orch.snapshot(), default=str))
            tmp.replace(path)
        except OSError:
            pass
        await asyncio.sleep(1.0)


async def amain() -> None:
    setup_logging()
    cfg = config.load()
    orch = Orchestrator(cfg)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop.set()
        asyncio.create_task(orch.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows / unusual envs — fall back to default handlers
            pass

    snap_task = asyncio.create_task(
        snapshot_writer(orch, cfg.snapshot_path, stop), name="snapshot"
    )
    try:
        await orch.run()
    finally:
        stop.set()
        snap_task.cancel()
        try:
            await snap_task
        except asyncio.CancelledError:
            pass
        orch.db.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
