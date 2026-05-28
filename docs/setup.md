# Setup guide

End-to-end checklist for setting up Lock-In on a Raspberry Pi 5 with an
Arduino Uno frontend and a Mac webcam server.

## 0. Prerequisites

- Raspberry Pi 5 (8 GB) running Raspberry Pi OS Bookworm (64-bit)
- Arduino Uno + USB-B cable
- Mac (or any other machine) on the same LAN to run `mac_camera_server.py`
- PIR (HC-SR501), HC-SR04 ultrasonic, DHT22, LDR + 10 kΩ resistor,
  3× LEDs (R/Y/G) + 220 Ω resistors, push button, passive piezo buzzer,
  breadboard, jumpers
- A Gemini API key (<https://aistudio.google.com> → "Get API key")

## 1. Pi base setup

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
sudo usermod -a -G dialout "$USER"
# log out and back in for the group change to take effect
```

## 2. Clone and install

```bash
git clone <this repo> ~/lock-in
cd ~/lock-in/pi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env   # fill in GEMINI_API_KEY and CAMERA_URL (Mac IP + port 8081)
```

## 3. Arduino firmware

Install Arduino IDE 2.x and add these libraries via Library Manager:

- **Adafruit Unified Sensor** (dep of DHT)
- **DHT sensor library** (Adafruit)

Open `arduino/lock_in_arduino/lock_in_arduino.ino`, select
**Board: Arduino Uno** and the right port, then **Upload**.

After upload, watch the Serial Monitor at 115200 baud. You should see
`{"type":"hello","fw":"lock-in/1.0"}` followed by 1 Hz frames.

For peripheral-by-peripheral wiring checks before flashing the real
firmware, upload `arduino/lock_in_demo/lock_in_demo.ino` first —
it exercises every sensor and prints results to the serial monitor.

## 4. Mac webcam server

On your Mac (must be on the same LAN as the Pi):

```bash
cd mac_camera
pip install -r requirements.txt
python mac_camera_server.py --port 8081
```

Find your Mac's IP: `ipconfig getifaddr en0` (Wi-Fi) or `en1` (Ethernet).
Set `CAMERA_URL=http://<mac-ip>:8081/capture` in the Pi's `.env`.

Confirm from the Pi: `curl http://<mac-ip>:8081/capture --output test.jpg`

## 5. Wiring

```
Arduino Uno
  D2 ---- PIR OUT
  D3 ---- BUTTON  (other leg to GND; INPUT_PULLUP used in firmware)
  D4 ---- HC-SR04 TRIG
  D5 ---- HC-SR04 ECHO
  D6 ---- LED red anode   (220 Ω to GND on cathode)
  D7 ---- DHT22 DATA      (with 10 kΩ pull-up to 5 V)
  D8 ---- LED yellow anode (220 Ω to GND)
  D9 ---- BUZZER (+)
  D10 --- LED green anode  (220 Ω to GND)
  A0 ---- LDR tap          (LDR top to 5V, A0 to LDR, 10 kΩ from A0 to GND)
  5V ---- PIR VCC, HC-SR04 VCC, DHT22 VCC, LDR top
  GND --- common GND for everything
```

See [`circuit.md`](circuit.md) for the full block diagram and a
component-by-component schematic.

## 6. Run

```bash
cd ~/lock-in/pi
source .venv/bin/activate
./run.sh
```

`run.sh` starts both the orchestrator (`main.py`) and the dashboard
(`dashboard/app.py`); Ctrl-C stops both. To run them separately:

```bash
python main.py            # in one terminal
python dashboard/app.py   # in another
```

Open `http://<pi-ip>:8080/`.

## 7. Install as services

```bash
sudo cp ~/lock-in/pi/systemd/lockin-*.service /etc/systemd/system/
# adjust paths in the unit files if your home dir is not /home/pi
sudo systemctl daemon-reload
sudo systemctl enable --now lockin-orchestrator lockin-dashboard
sudo journalctl -fu lockin-orchestrator
```

## 8. Fault-injection demo script

For the marker demo:

1. Run a session normally — show focus accumulating.
2. Use phone deliberately — show DEGRADING state + image in dashboard.
3. Unplug Arduino — dashboard shows "Arduino offline"; system holds state.
4. Stop the Mac webcam server — vision pauses, sensor logic continues.
5. `sudo systemctl kill lockin-orchestrator` — watch systemd restart it
   within `RestartSec=5`.

All five behaviours appear in `journalctl -fu lockin-orchestrator`
during the demo.
