# Architecture

This doc explains how Lock-In is wired together at the software level.
For the physical wiring see [`circuit.md`](circuit.md); for the state machine
see [`fsm.md`](fsm.md).

## 1. System overview

Three independent processes, three transports, one source of truth (the FSM).

```mermaid
flowchart LR
    subgraph Desk["Desk"]
        sensors["PIR · HC-SR04 · DHT22 · LDR · Button"]
        leds["3× LED + Buzzer"]
    end

    subgraph Uno["Arduino Uno (firmware)"]
        sensors --> fw["lock_in_arduino.ino<br/>1 Hz JSON frames<br/>event JSON on press / PIR"]
        fw --> leds
    end

    subgraph Pi["Raspberry Pi 5 (Python)"]
        ser["serial_reader.py"]
        orch["orchestrator.py<br/>(asyncio loop)"]
        fsm["fsm.py<br/>(pure logic)"]
        vis["vision_judge.py"]
        cam["camera_client.py"]
        db[("SQLite<br/>lockin.db")]
        snap[/"snapshot.json"/]
        cmd[/"cmd.json"/]
        dash["dashboard/app.py<br/>(Flask)"]
    end

    subgraph Mac["Mac (LAN)"]
        webcam["mac_camera_server.py"]
    end

    subgraph Cloud["Cloud"]
        gemini[("Gemini 2.5 Flash")]
    end

    Uno <-->|"USB serial<br/>115200 baud<br/>JSON lines"| ser
    ser --> orch
    orch --> fsm
    fsm --> orch
    orch --> db
    orch --> snap
    orch --> cam
    cam <-->|"HTTP /capture"| webcam
    orch --> vis
    vis <-->|"HTTPS"| gemini
    cmd --> orch
    dash --> cmd
    dash --> snap
    dash --> db
    user(["User browser"]) -->|"HTTP :8080"| dash
```

Key points:

- **One FSM, many inputs.** Serial frames, vision judgments, button events,
  and dashboard commands all funnel into a single `FocusFsm` instance. Nothing
  else owns state.
- **Files as IPC.** The orchestrator writes `snapshot.json` once per second;
  the dashboard reads it. The dashboard writes `cmd.json` to request
  state changes; the orchestrator drains it. No shared memory, no socket.
- **Vision is a sensor, not an oracle.** A failed Gemini call (timeout,
  bad JSON, network) is a no-op for the cycle. The FSM still ticks.

## 2. Async task topology (on the Pi)

```mermaid
flowchart TB
    main["main.py<br/>asyncio.run(amain)"]
    snap_task["snapshot_writer<br/>(1 Hz)"]
    orch_run["Orchestrator.run()"]

    main --> snap_task
    main --> orch_run

    orch_run --> t_serial["serial<br/>SerialReader.run()"]
    orch_run --> t_consumer["serial_consumer<br/>drain queue → FSM"]
    orch_run --> t_tick["tick<br/>(1 Hz)<br/>FSM.tick()"]
    orch_run --> t_vision["vision<br/>(every<br/>VISION_INTERVAL_S)"]
    orch_run --> t_retention["retention<br/>(12 h)"]
```

Tasks are started with `asyncio.create_task` and awaited together with
`asyncio.wait(..., return_when=FIRST_EXCEPTION)`. If any one task crashes,
the others get cancelled, the loop unwinds, and systemd restarts the process.

## 3. Vision cycle sequence

```mermaid
sequenceDiagram
    autonumber
    participant L as vision_loop
    participant C as CameraClient
    participant M as Mac webcam
    participant V as VisionJudge
    participant G as Gemini API
    participant F as FocusFsm
    participant O as Orchestrator
    participant D as SQLite

    L->>L: sleep VISION_INTERVAL_S
    L->>L: skip if session not in FOCUS/DEGRADING
    L->>C: capture()
    C->>M: GET /capture
    M-->>C: image/jpeg
    C-->>L: jpeg bytes (or None on error)
    L->>V: judge(jpeg, timeout=20s)
    V->>G: generate_content(prompt + jpeg)
    G-->>V: JSON {focused, confidence, observation}
    V-->>L: FocusJudgment (or None)
    L->>F: on_vision(judgment)
    F-->>L: list[Action]
    L->>O: _apply_actions(...)
    O->>D: log_distraction / log_transition / update_session
```

Any None response between steps 5 and 9 short-circuits the cycle — the
sensor-only paths in `FSM.tick()` continue regardless.

## 4. Data model

```mermaid
erDiagram
    sessions ||--o{ state_transitions : has
    sessions ||--o{ distraction_events : has
    sessions ||--o{ environment_log : has

    sessions {
        INTEGER id PK
        TEXT start_time
        TEXT end_time
        INTEGER focused_seconds
        INTEGER total_seconds
        INTEGER distraction_count
        INTEGER break_count
        TEXT notes
    }
    state_transitions {
        INTEGER id PK
        INTEGER session_id FK
        TEXT timestamp
        TEXT from_state
        TEXT to_state
        TEXT trigger
    }
    distraction_events {
        INTEGER id PK
        INTEGER session_id FK
        TEXT timestamp
        TEXT image_path
        TEXT observation
        REAL confidence
        INTEGER duration_seconds
    }
    environment_log {
        INTEGER id PK
        INTEGER session_id FK
        TEXT timestamp
        REAL temperature
        REAL humidity
        INTEGER light_level
    }
```

`sessions.id` is the foreign key everywhere. Sessions left open on a crash
are sealed at next startup by `mark_orphan_sessions_incomplete` and tagged
in their `notes` column.

## 5. Network ports and protocols

| Port | Process            | Role                                    |
|------|--------------------|-----------------------------------------|
| 8080 | dashboard (Pi)     | HTTP — Flask dashboard + JSON APIs      |
| 8081 | mac_camera_server  | HTTP — `/capture` returns image/jpeg    |
| USB  | Arduino ↔ Pi       | 115200 baud, newline-delimited JSON     |
| 443  | Pi → Gemini        | HTTPS — `generativelanguage.googleapis.com` |

## 6. Module map (Pi)

| Module             | Responsibility                                                       |
|--------------------|----------------------------------------------------------------------|
| `config.py`        | Load env vars into a frozen `Config` dataclass; derive paths.        |
| `database.py`      | SQLite wrapper, schema, all queries. Thread-safe via single lock.    |
| `serial_reader.py` | Async serial reader/writer with auto-reconnect on EOF.               |
| `camera_client.py` | Async HTTP client for `/capture`.                                    |
| `vision_judge.py`  | Gemini call + strict JSON parser.                                    |
| `fsm.py`           | `FocusFsm` — pure state machine, no I/O.                             |
| `orchestrator.py`  | Wires all of the above into asyncio tasks; owns the FSM instance.    |
| `main.py`          | Entry point. Sets up logging, signals, snapshot writer.              |
| `dashboard/app.py` | Flask — read-only over `snapshot.json` + SQLite; writes `cmd.json`.  |

Every I/O module degrades gracefully: a missing dependency or a network
fault logs once and returns `None` / `False`. The FSM never crashes the
orchestrator; the orchestrator never crashes systemd's restart loop.
