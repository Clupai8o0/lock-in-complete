"""Lock-In — Mac webcam HTTP server.

Drop-in replacement for the ESP32-CAM. Runs on the Mac, serves a fresh JPEG
on /capture so the Pi can pull frames on its own schedule. Same HTTP shape
as the ESP32-CAM, so the Pi side (camera_client.py) needs no changes — just
point CAMERA_URL at this server.

Setup:
    pip install opencv-python flask
Run:
    python mac_camera_server.py --port 8081

Then on the Pi, set in .env:
    CAMERA_URL=http://<mac-ip>:8081/capture

Find your Mac's LAN IP with `ipconfig getifaddr en0` (Wi-Fi) or `en1` (Ethernet).
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import cv2
from flask import Flask, Response, jsonify


class WebcamGrabber:
    """Thread-safe wrapper around cv2.VideoCapture.

    Held open across requests because re-opening the camera every capture
    is slow (~1s) and unreliable on macOS.
    """

    def __init__(self, index: int = 0, width: int = 1280, height: int = 720):
        self.index = index
        self.width = width
        self.height = height
        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.Lock()
        self.last_capture_ts: float | None = None

    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise RuntimeError(f"could not open webcam index {self.index}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # warm up — first few frames are often black/garbage
        for _ in range(5):
            cap.read()
        return cap

    def capture_jpeg(self, quality: int = 80) -> bytes:
        with self._lock:
            if self._cap is None:
                self._cap = self._open()
            ok, frame = self._cap.read()
            if not ok or frame is None:
                # camera went away — try once more after reopen
                self._cap.release()
                self._cap = self._open()
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    raise RuntimeError("webcam read failed")
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                raise RuntimeError("JPEG encode failed")
            self.last_capture_ts = time.time()
            return bytes(buf)

    def close(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None


def build_app(grabber: WebcamGrabber, jpeg_quality: int) -> Flask:
    app = Flask(__name__)

    @app.get("/capture")
    def capture():
        try:
            jpeg = grabber.capture_jpeg(quality=jpeg_quality)
        except RuntimeError as e:
            return jsonify(error=str(e)), 503
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/status")
    def status():
        return jsonify(
            online=True,
            last_capture_ts=grabber.last_capture_ts,
            resolution=[grabber.width, grabber.height],
        )

    return app


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    p.add_argument("--port", type=int, default=8081, help="HTTP port (default: 8081)")
    p.add_argument("--device", type=int, default=0, help="webcam index (default: 0)")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100 (default: 80)")
    args = p.parse_args()

    grabber = WebcamGrabber(index=args.device, width=args.width, height=args.height)

    # Pre-open so the first request isn't slow. Also surfaces "camera missing"
    # at startup instead of on first capture.
    try:
        grabber.capture_jpeg(quality=args.quality)
    except RuntimeError as e:
        print(f"webcam init failed: {e}", file=sys.stderr)
        print("\nOn macOS the first run will prompt for Camera permission.", file=sys.stderr)
        print("Allow it in System Settings -> Privacy & Security -> Camera, then re-run.", file=sys.stderr)
        return 1

    print(f"camera ready ({args.width}x{args.height}). serving on http://{args.host}:{args.port}/capture")
    app = build_app(grabber, jpeg_quality=args.quality)
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        grabber.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
