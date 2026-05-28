"""Centralised configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(key: str, default: str | None = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"missing env var: {key}")
    return v


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


@dataclass(frozen=True)
class Config:
    # gemini
    gemini_api_key: str
    gemini_model: str

    # serial
    serial_port: str
    serial_baud: int

    # camera
    camera_url: str
    camera_timeout_s: float

    # timing
    vision_interval_s: float
    focus_confirm_s: float
    degrading_to_break_s: float
    away_timeout_s: float
    break_to_idle_s: float
    desk_away_s: float

    # storage
    db_path: Path
    image_dir: Path
    image_retention_days: int
    snapshot_path: Path
    cmd_path: Path

    # pomodoro
    pomodoro_duration_s: float

    # dashboard
    dashboard_host: str
    dashboard_port: int


def load() -> Config:
    db_path = Path(os.environ.get("DB_PATH", "./data/lockin.db"))
    data_dir = db_path.parent
    cfg = Config(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        serial_port=os.environ.get("SERIAL_PORT", "/dev/ttyACM0"),
        serial_baud=_env_int("SERIAL_BAUD", 115200),
        camera_url=os.environ.get("CAMERA_URL", "http://localhost:8081/capture"),
        camera_timeout_s=_env_float("CAMERA_TIMEOUT_S", 8.0),
        vision_interval_s=_env_float("VISION_INTERVAL_S", 75.0),
        focus_confirm_s=_env_float("FOCUS_CONFIRM_S", 60.0),
        degrading_to_break_s=_env_float("DEGRADING_TO_BREAK_S", 120.0),
        away_timeout_s=_env_float("AWAY_TIMEOUT_S", 300.0),
        break_to_idle_s=_env_float("BREAK_TO_IDLE_S", 600.0),
        desk_away_s=_env_float("DESK_AWAY_S", 30.0),
        db_path=db_path,
        image_dir=Path(os.environ.get("IMAGE_DIR", str(data_dir / "images"))),
        image_retention_days=_env_int("IMAGE_RETENTION_DAYS", 7),
        snapshot_path=data_dir / "snapshot.json",
        cmd_path=data_dir / "cmd.json",
        pomodoro_duration_s=_env_float("POMODORO_DURATION_S", 25 * 60),
        dashboard_host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=_env_int("DASHBOARD_PORT", 8080),
    )
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.image_dir.mkdir(parents=True, exist_ok=True)
    return cfg
