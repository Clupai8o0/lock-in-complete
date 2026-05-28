/*
 * Lock-In - Arduino Uno sensor frontend
 *
 * Streams 1Hz sensor frames over serial to the Pi as JSON.
 * Detects button press patterns (single, double, long) in interrupt handler.
 * Drives the local buzzer for immediate audio cues independent of the Pi.
 *
 * Wiring (see README for the Fritzing diagram):
 *   PIR        -> D2  (INT0, hardware interrupt)
 *   Button     -> D3  (INT1, hardware interrupt, pull-up)
 *   Ultrasonic -> D4 trig, D5 echo
 *   LED red    -> D6  (220Ω to GND)
 *   DHT22      -> D7
 *   LED yellow -> D8  (220Ω to GND)
 *   Buzzer     -> D9  (PWM)
 *   LED green  -> D10 (220Ω to GND)
 *   LDR        -> A0  (voltage divider with 10k to GND, LDR top to 5V)
 *
 * Serial protocol (115200 baud, newline-delimited JSON):
 *   Pi   -> Arduino: {"cmd":"buzz","pattern":"chirp|confirm|alert|silence"}
 *   Pi   -> Arduino: {"cmd":"led","state":"AWAY|IDLE|FOCUS|DEGRADING|BREAK"}
 *   Pi   -> Arduino: {"cmd":"ping"}
 *   Ard  -> Pi: {"t":<ms>,"type":"frame","presence":bool,"dist_cm":float,
 *                "temp_c":float,"hum":float,"light":float}    // light is raw LDR (0-1023)
 *   Ard  -> Pi: {"t":<ms>,"type":"event","event":"pir"|"button","action":"single|double|long"}
 *   Ard  -> Pi: {"t":<ms>,"type":"hello","fw":"lock-in/1.0"}
 */

#include <DHT.h>

// ---------- pins ----------
const uint8_t PIN_PIR    = 2;
const uint8_t PIN_BUTTON = 3;
const uint8_t PIN_TRIG   = 4;
const uint8_t PIN_ECHO   = 5;
const uint8_t PIN_LED_R  = 6;
const uint8_t PIN_DHT    = 7;
const uint8_t PIN_LED_Y  = 8;
const uint8_t PIN_BUZZER = 9;
const uint8_t PIN_LED_G  = 10;
const uint8_t PIN_LDR    = A0;

// ---------- timing ----------
const unsigned long FRAME_INTERVAL_MS   = 1000;
const unsigned long BUTTON_DEBOUNCE_MS  = 40;
const unsigned long DOUBLE_PRESS_MS     = 600;
const unsigned long LONG_PRESS_MS       = 2500;
const unsigned long PIR_COOLDOWN_MS     = 1500;

// ---------- DHT ----------
DHT dht(PIN_DHT, DHT22);

// ---------- LDR (analog light) ----------
int  lastLdr = -1;   // raw 0-1023; -1 = no reading yet

// ---------- ISR shared state ----------
volatile bool     pirFired         = false;
volatile uint32_t pirLastFiredMs   = 0;

volatile bool     buttonChanged    = false;
volatile uint8_t  buttonLevel      = HIGH;
volatile uint32_t buttonEdgeMs     = 0;

// ---------- button state machine (main loop) ----------
enum ButtonPhase { BTN_IDLE, BTN_PRESSED, BTN_RELEASED_WAIT };
ButtonPhase btnPhase   = BTN_IDLE;
uint32_t    btnDownMs  = 0;
uint32_t    btnUpMs    = 0;
uint8_t     pressCount = 0;

// ---------- sensor cache ----------
float    lastTemp   = NAN;
float    lastHum    = NAN;
uint32_t lastFrameMs = 0;
uint32_t lastDhtMs   = 0;
const uint32_t DHT_INTERVAL_MS = 2000;  // DHT22 max 0.5Hz

// ---------- buzzer ----------
struct BuzzStep { uint16_t freq; uint16_t ms; };
const BuzzStep BUZZ_CHIRP[]   = {{2000, 60}, {0, 0}};
const BuzzStep BUZZ_CONFIRM[] = {{1500, 80}, {0, 40}, {2200, 120}, {0, 0}};
const BuzzStep BUZZ_ALERT[]   = {{800, 200}, {0, 80}, {800, 200}, {0, 0}};
const BuzzStep BUZZ_BLOCKED[] = {{1800, 80}, {0, 60}, {1800, 80}, {0, 0}};

const BuzzStep* buzzSeq    = nullptr;
uint8_t         buzzIdx    = 0;
uint32_t        buzzNextMs = 0;
bool            buzzOn     = false;

void buzzStart(const BuzzStep* seq) {
  buzzSeq = seq;
  buzzIdx = 0;
  buzzNextMs = millis();
  buzzOn = false;
  noTone(PIN_BUZZER);
}

void buzzStop() {
  buzzSeq = nullptr;
  noTone(PIN_BUZZER);
  buzzOn = false;
}

void buzzTick() {
  if (!buzzSeq) return;
  if ((int32_t)(millis() - buzzNextMs) < 0) return;

  const BuzzStep& step = buzzSeq[buzzIdx];
  if (step.freq == 0 && step.ms == 0) {  // terminator
    buzzStop();
    return;
  }
  if (step.freq == 0) {
    noTone(PIN_BUZZER);
  } else {
    tone(PIN_BUZZER, step.freq);
  }
  buzzNextMs = millis() + step.ms;
  buzzIdx++;
}

// ---------- LED state ----------
enum LedMode { LED_OFF, LED_RED, LED_YELLOW, LED_GREEN, LED_YELLOW_BLINK };
LedMode  ledMode       = LED_OFF;
uint32_t ledBlinkNextMs = 0;
bool     ledBlinkOn     = false;
const uint16_t LED_BLINK_MS = 500;

// Explicit prototype — Arduino's auto-prototype generator emits one above
// the LedMode enum otherwise, breaking compilation.
void ledSetMode(LedMode m);

void ledWrite(bool r, bool y, bool g) {
  digitalWrite(PIN_LED_R, r ? HIGH : LOW);
  digitalWrite(PIN_LED_Y, y ? HIGH : LOW);
  digitalWrite(PIN_LED_G, g ? HIGH : LOW);
}

void ledApply() {
  switch (ledMode) {
    case LED_OFF:           ledWrite(false, false, false); break;
    case LED_RED:           ledWrite(true,  false, false); break;
    case LED_YELLOW:        ledWrite(false, true,  false); break;
    case LED_GREEN:         ledWrite(false, false, true);  break;
    case LED_YELLOW_BLINK:  ledWrite(false, ledBlinkOn, false); break;
  }
}

void ledSetMode(LedMode m) {
  ledMode = m;
  ledBlinkOn = true;
  ledBlinkNextMs = millis() + LED_BLINK_MS;
  ledApply();
}

void ledTick() {
  if (ledMode != LED_YELLOW_BLINK) return;
  if ((int32_t)(millis() - ledBlinkNextMs) < 0) return;
  ledBlinkOn = !ledBlinkOn;
  ledBlinkNextMs = millis() + LED_BLINK_MS;
  ledApply();
}

// ---------- ISRs ----------
void isrPir() {
  uint32_t now = millis();
  if (now - pirLastFiredMs > PIR_COOLDOWN_MS) {
    pirFired = true;
    pirLastFiredMs = now;
  }
}

void isrButton() {
  buttonLevel = digitalRead(PIN_BUTTON);
  buttonEdgeMs = millis();
  buttonChanged = true;
}

// ---------- ultrasonic ----------
float readDistanceCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  // 25 ms timeout -> max ~4 m, good enough for desk range
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 25000UL);
  if (us == 0) return -1.0;
  return (us * 0.0343f) / 2.0f;
}

// ---------- helpers ----------
void sendHello() {
  Serial.print(F("{\"t\":"));
  Serial.print(millis());
  Serial.println(F(",\"type\":\"hello\",\"fw\":\"lock-in/1.0\"}"));
}

void sendEvent(const __FlashStringHelper* evt, const __FlashStringHelper* action) {
  Serial.print(F("{\"t\":"));
  Serial.print(millis());
  Serial.print(F(",\"type\":\"event\",\"event\":\""));
  Serial.print(evt);
  Serial.print(F("\",\"action\":\""));
  Serial.print(action);
  Serial.println(F("\"}"));
}

void sendFrame(bool presence, float distCm, float tempC, float hum, int ldr) {
  Serial.print(F("{\"t\":"));
  Serial.print(millis());
  Serial.print(F(",\"type\":\"frame\",\"presence\":"));
  Serial.print(presence ? F("true") : F("false"));
  Serial.print(F(",\"dist_cm\":"));
  if (distCm < 0) Serial.print(F("null")); else Serial.print(distCm, 1);
  Serial.print(F(",\"temp_c\":"));
  if (isnan(tempC)) Serial.print(F("null")); else Serial.print(tempC, 1);
  Serial.print(F(",\"hum\":"));
  if (isnan(hum)) Serial.print(F("null")); else Serial.print(hum, 1);
  Serial.print(F(",\"light\":"));
  if (ldr < 0) Serial.print(F("null")); else Serial.print(ldr);
  Serial.println(F("}"));
}

// ---------- command parser (very small JSON-ish) ----------
void handleCommand(const String& line) {
  // looking for "cmd":"..." and optional "pattern":"..."
  int ci = line.indexOf("\"cmd\"");
  if (ci < 0) return;
  int q1 = line.indexOf('"', line.indexOf(':', ci) + 1);
  int q2 = line.indexOf('"', q1 + 1);
  if (q1 < 0 || q2 < 0) return;
  String cmd = line.substring(q1 + 1, q2);

  if (cmd == "ping") {
    sendHello();
    return;
  }
  if (cmd == "buzz") {
    int pi = line.indexOf("\"pattern\"");
    if (pi < 0) { buzzStart(BUZZ_CHIRP); return; }
    int p1 = line.indexOf('"', line.indexOf(':', pi) + 1);
    int p2 = line.indexOf('"', p1 + 1);
    if (p1 < 0 || p2 < 0) return;
    String pat = line.substring(p1 + 1, p2);
    if      (pat == "chirp")   buzzStart(BUZZ_CHIRP);
    else if (pat == "confirm") buzzStart(BUZZ_CONFIRM);
    else if (pat == "alert")   buzzStart(BUZZ_ALERT);
    else if (pat == "blocked") buzzStart(BUZZ_BLOCKED);
    else if (pat == "silence") buzzStop();
  }
  if (cmd == "led") {
    int si = line.indexOf("\"state\"");
    if (si < 0) return;
    int s1 = line.indexOf('"', line.indexOf(':', si) + 1);
    int s2 = line.indexOf('"', s1 + 1);
    if (s1 < 0 || s2 < 0) return;
    String st = line.substring(s1 + 1, s2);
    if      (st == "AWAY")      ledSetMode(LED_OFF);
    else if (st == "IDLE")      ledSetMode(LED_YELLOW);
    else if (st == "FOCUS")     ledSetMode(LED_GREEN);
    else if (st == "DEGRADING") ledSetMode(LED_RED);
    else if (st == "BREAK")     ledSetMode(LED_YELLOW_BLINK);
    else if (st == "BLOCKED")   ledSetMode(LED_RED);
  }
}

String rxLine;

// ---------- setup ----------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_LED_R, OUTPUT);
  pinMode(PIN_LED_Y, OUTPUT);
  pinMode(PIN_LED_G, OUTPUT);
  pinMode(PIN_LDR, INPUT);
  ledSetMode(LED_OFF);  // AWAY at boot

  dht.begin();

  attachInterrupt(digitalPinToInterrupt(PIN_PIR), isrPir, RISING);
  attachInterrupt(digitalPinToInterrupt(PIN_BUTTON), isrButton, CHANGE);

  delay(200);
  sendHello();
}

// ---------- main loop ----------
void loop() {
  uint32_t now = millis();

  // ----- serial input -----
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleCommand(rxLine);
      rxLine = "";
    } else if (c != '\r' && rxLine.length() < 120) {
      rxLine += c;
    }
  }

  // ----- PIR -----
  if (pirFired) {
    pirFired = false;
    sendEvent(F("pir"), F("trigger"));
  }

  // ----- button (debounced press-pattern detection) -----
  if (buttonChanged) {
    noInterrupts();
    uint8_t lvl = buttonLevel;
    uint32_t edge = buttonEdgeMs;
    interrupts();
    if ((uint32_t)(millis() - edge) >= BUTTON_DEBOUNCE_MS) {
      noInterrupts();
      buttonChanged = false;
      interrupts();
      if (lvl == LOW) {  // press (active low)
        if (btnPhase == BTN_IDLE || btnPhase == BTN_RELEASED_WAIT) {
          btnPhase = BTN_PRESSED;
          btnDownMs = edge;
        }
      } else {           // release
        if (btnPhase == BTN_PRESSED) {
          btnUpMs = edge;
          uint32_t held = btnUpMs - btnDownMs;
          if (held >= LONG_PRESS_MS) {
            sendEvent(F("button"), F("long"));
            pressCount = 0;
            btnPhase = BTN_IDLE;
          } else {
            pressCount++;
            btnPhase = BTN_RELEASED_WAIT;
          }
        }
      }
    }
  }

  // resolve single vs double after release window
  if (btnPhase == BTN_RELEASED_WAIT && (now - btnUpMs) >= DOUBLE_PRESS_MS) {
    if (pressCount >= 2)      sendEvent(F("button"), F("double"));
    else if (pressCount == 1) sendEvent(F("button"), F("single"));
    pressCount = 0;
    btnPhase = BTN_IDLE;
  }

  // long press while still held
  if (btnPhase == BTN_PRESSED && (now - btnDownMs) >= LONG_PRESS_MS) {
    sendEvent(F("button"), F("long"));
    pressCount = 0;
    btnPhase = BTN_IDLE;  // consume; require release before next event
  }

  // ----- DHT22 (slow sensor, cache value) -----
  if (now - lastDhtMs >= DHT_INTERVAL_MS) {
    lastDhtMs = now;
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t)) lastTemp = t;
    if (!isnan(h)) lastHum  = h;
  }

  // ----- 1Hz sensor frame -----
  if (now - lastFrameMs >= FRAME_INTERVAL_MS) {
    lastFrameMs = now;
    float dist = readDistanceCm();
    lastLdr = analogRead(PIN_LDR);
    // presence: ultrasonic within 150cm OR PIR active recently
    bool presence = (dist > 0 && dist < 150.0) ||
                    (now - pirLastFiredMs < 30000UL);
    sendFrame(presence, dist, lastTemp, lastHum, lastLdr);
  }

  // ----- buzzer cadence -----
  buzzTick();

  // ----- LED blink cadence -----
  ledTick();
}
