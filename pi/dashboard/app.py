"""Flask dashboard. Read-only view over snapshot.json + SQLite."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

# allow `python dashboard/app.py` from the pi/ dir
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from database import Database  # noqa: E402

CFG = config.load()
DB = Database(CFG.db_path)
SNAPSHOT_PATH = CFG.snapshot_path
CMD_PATH = CFG.cmd_path
SETTINGS_KEYS = (
    "pomodoro_duration_s",
    "focus_confirm_s",
    "desk_away_s",
    "away_timeout_s",
    "degrading_to_break_s",
    "break_to_idle_s",
    "target_pomodoros",
)

app = Flask(__name__)


def _load_snapshot() -> dict:
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "state": "OFFLINE",
            "session_id": None,
            "focused_seconds": 0,
            "total_seconds": 0,
            "distraction_count": 0,
            "break_count": 0,
            "serial_connected": False,
            "camera_online": False,
            "vision_enabled": False,
        }


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "0m"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


app.jinja_env.filters["duration"] = _fmt_duration


@app.route("/")
def index():
    snap = _load_snapshot()
    return render_template("today.html", snap=snap)


@app.route("/history")
def history():
    sessions = [dict(r) for r in DB.list_sessions(days=7)]
    # rough per-day totals
    by_day: dict[str, dict] = {}
    longest = 0
    for s in sessions:
        day = s["start_time"][:10]
        d = by_day.setdefault(
            day, {"day": day, "focused": 0, "total": 0, "sessions": 0}
        )
        d["focused"] += s.get("focused_seconds") or 0
        d["total"] += s.get("total_seconds") or 0
        d["sessions"] += 1
        if (s.get("focused_seconds") or 0) > longest:
            longest = s["focused_seconds"]
    days = sorted(by_day.values(), key=lambda x: x["day"], reverse=True)
    return render_template(
        "history.html",
        sessions=sessions,
        days=days,
        longest=longest,
    )


@app.route("/distractions")
def distractions():
    items = [dict(r) for r in DB.list_distractions(days=7)]
    for it in items:
        ip = it.get("image_path")
        if ip and Path(ip).exists():
            it["image_url"] = "/image/" + Path(ip).name
        else:
            it["image_url"] = None
    return render_template("distractions.html", items=items)


@app.route("/image/<name>")
def image(name: str):
    safe = Path(name).name  # strip any path traversal
    p = (CFG.image_dir / safe).resolve()
    if not p.exists() or not p.is_file():
        abort(404)
    return send_file(str(p), mimetype="image/jpeg")


@app.route("/settings")
def settings():
    snap = _load_snapshot()
    cfg_view = {
        "vision_interval_s": CFG.vision_interval_s,
        "focus_confirm_s": CFG.focus_confirm_s,
        "degrading_to_break_s": CFG.degrading_to_break_s,
        "away_timeout_s": CFG.away_timeout_s,
        "break_to_idle_s": CFG.break_to_idle_s,
        "desk_away_s": CFG.desk_away_s,
        "image_retention_days": CFG.image_retention_days,
        "camera_url": CFG.camera_url,
        "gemini_model": CFG.gemini_model,
    }
    return render_template("settings.html", snap=snap, cfg=cfg_view)


@app.route("/api/snapshot")
def api_snapshot():
    return jsonify(_load_snapshot())


@app.route("/api/sessions")
def api_sessions():
    return jsonify([dict(r) for r in DB.list_sessions(days=7)])


@app.route("/api/distractions")
def api_distractions():
    return jsonify([dict(r) for r in DB.list_distractions(days=7)])


@app.route("/api/export.csv")
def api_export_csv():
    import csv
    import io

    rows = DB.list_sessions(days=30)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "start_time", "end_time", "focused_seconds",
                "total_seconds", "distraction_count", "break_count", "notes"])
    for r in rows:
        w.writerow([
            r["id"], r["start_time"], r["end_time"] or "",
            r["focused_seconds"] or 0, r["total_seconds"] or 0,
            r["distraction_count"] or 0, r["break_count"] or 0,
            r["notes"] or ""
        ])
    return (
        buf.getvalue(),
        200,
        {
            "Content-Type": "text/csv",
            "Content-Disposition": "attachment; filename=lockin-sessions.csv",
        },
    )


@app.route("/api/button", methods=["POST"])
def api_button():
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "single")
    if action not in ("single", "double", "long"):
        return jsonify({"error": "invalid action"}), 400
    return _write_command({"type": "button", "action": action})


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    data = request.get_json(force=True, silent=True) or {}
    payload: dict[str, float] = {}
    for k, v in data.items():
        if k not in SETTINGS_KEYS:
            continue
        try:
            payload[k] = float(v)
        except (TypeError, ValueError):
            pass
    if not payload:
        return jsonify({"error": "no valid settings"}), 400
    return _write_command({"type": "settings", **payload})


def _write_command(cmd: dict):
    try:
        CMD_PATH.write_text(json.dumps(cmd))
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host=CFG.dashboard_host, port=CFG.dashboard_port)
