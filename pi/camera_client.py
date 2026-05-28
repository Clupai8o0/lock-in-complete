"""Pulls JPEGs from the Mac webcam HTTP server (mac_camera/) over HTTP."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("lockin.camera")


@dataclass
class CameraCapture:
    jpeg: bytes
    content_type: str = "image/jpeg"


class CameraClient:
    def __init__(self, url: str, timeout_s: float = 8.0):
        self.url = url
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None
        self.online = False

    async def __aenter__(self) -> "CameraClient":
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def capture(self) -> CameraCapture | None:
        assert self._session is not None
        try:
            async with self._session.get(self.url) as resp:
                if resp.status != 200:
                    self.last_error = f"HTTP {resp.status}"
                    self.online = False
                    log.warning("camera HTTP %s", resp.status)
                    return None
                jpeg = await resp.read()
                if not jpeg or len(jpeg) < 1024:
                    self.last_error = f"tiny payload ({len(jpeg)}B)"
                    self.online = False
                    return None
                self.last_error = None
                self.online = True
                return CameraCapture(jpeg=jpeg)
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            self.online = False
            log.warning("camera timeout")
            return None
        except aiohttp.ClientError as e:
            self.last_error = f"client error: {e}"
            self.online = False
            log.warning("camera client error: %s", e)
            return None
