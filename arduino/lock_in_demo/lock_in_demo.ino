/*
 * Lock-In — hardware demo / continuity sketch
 *
 * Exercises every peripheral on the Arduino Uno frontend so you can verify
 * each one is wired up before flashing the real firmware. Open the Serial
 * Monitor at 115200 baud after upload.
 *
 * To isolate a single component, comment out every other TEST_* define at
 * the top of this file. Any test that's not #defined is fully compiled out,
 * so library code for that component won't run.
 *
 * Pin map (matches lock_in_arduino.ino):
 *   D2  PIR        D3  button      D4/D5 HC-SR04   D6  LED red
 *   D7  DHT22      D8  LED yellow  D9  buzzer      D10 LED green
 *   A0  LDR (analog, voltage divider with 10k to GND)
 */

// ============================================================
// Test toggles — comment out a line to skip that block entirely
// ============================================================
#define TEST_LEDS
#define TEST_BUZZER
#define TEST_PIR
#define TEST_BUTTON
#define TEST_ULTRASONIC
#define TEST_DHT22
#define TEST_LDR

// ============================================================
// Includes (only pulled in when the matching test is enabled)
// ============================================================
#ifdef TEST_DHT22
  #include <DHT.h>
#endif

// ============================================================
// Pin assignments
// ============================================================
const uint8_t PIN_PIR     = 2;
const uint8_t PIN_BUTTON  = 3;
const uint8_t PIN_TRIG    = 4;
const uint8_t PIN_ECHO    = 5;
const uint8_t PIN_LED_R   = 6;
const uint8_t PIN_DHT     = 7;
const uint8_t PIN_LED_Y   = 8;
const uint8_t PIN_BUZZER  = 9;
const uint8_t PIN_LED_G   = 10;
const uint8_t PIN_LDR     = A0;

// ============================================================
// Component instances
// ============================================================
#ifdef TEST_DHT22
  DHT dht(PIN_DHT, DHT22);
#endif

// ============================================================
// setup()
// ============================================================
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) { /* wait briefly for USB */ }
  Serial.println();
  Serial.println(F("=== Lock-In hardware demo ==="));

#ifdef TEST_LEDS
  pinMode(PIN_LED_R, OUTPUT);
  pinMode(PIN_LED_Y, OUTPUT);
  pinMode(PIN_LED_G, OUTPUT);
  digitalWrite(PIN_LED_R, LOW);
  digitalWrite(PIN_LED_Y, LOW);
  digitalWrite(PIN_LED_G, LOW);
  Serial.println(F("[LEDS]   pins D6/D8/D10 ready"));
#endif

#ifdef TEST_BUZZER
  pinMode(PIN_BUZZER, OUTPUT);
  Serial.println(F("[BUZZER] pin D9 ready"));
#endif

#ifdef TEST_PIR
  pinMode(PIN_PIR, INPUT);
  Serial.println(F("[PIR]    pin D2 ready — give it 30s to settle on first power-on"));
#endif

#ifdef TEST_BUTTON
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  Serial.println(F("[BTN]    pin D3 ready (INPUT_PULLUP, active-low)"));
#endif

#ifdef TEST_ULTRASONIC
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  digitalWrite(PIN_TRIG, LOW);
  Serial.println(F("[US]     HC-SR04 ready on D4 trig / D5 echo"));
#endif

#ifdef TEST_DHT22
  dht.begin();
  Serial.println(F("[DHT22]  initialised on D7"));
#endif

#ifdef TEST_LDR
  pinMode(PIN_LDR, INPUT);
  Serial.println(F("[LDR]    pin A0 ready (voltage divider with 10k to GND)"));
#endif

  Serial.println(F("--- entering loop ---"));
}

// ============================================================
// Helpers
// ============================================================
#ifdef TEST_LEDS
void blinkLed(uint8_t pin, const __FlashStringHelper* label) {
  Serial.print(F("[LEDS]   on "));
  Serial.println(label);
  digitalWrite(pin, HIGH);
  delay(400);
  digitalWrite(pin, LOW);
  delay(150);
}
#endif

#ifdef TEST_ULTRASONIC
float readDistanceCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 25000UL);
  if (us == 0) return -1.0;
  return (us * 0.0343f) / 2.0f;
}
#endif

// ============================================================
// loop() — each test runs in sequence so the serial log stays readable.
// ============================================================
void loop() {
  static uint32_t tick = 0;
  Serial.print(F("[HB]     tick "));
  Serial.println(tick++);

#ifdef TEST_LEDS
  Serial.println(F("[LEDS]   red -> yellow -> green sweep"));
  blinkLed(PIN_LED_R, F("RED"));
  blinkLed(PIN_LED_Y, F("YELLOW"));
  blinkLed(PIN_LED_G, F("GREEN"));
#endif

#ifdef TEST_BUZZER
  Serial.println(F("[BUZZER] 1kHz chirp"));
  tone(PIN_BUZZER, 1000, 150);
  delay(250);
  tone(PIN_BUZZER, 1800, 150);
  delay(250);
  noTone(PIN_BUZZER);
#endif

#ifdef TEST_PIR
  Serial.print(F("[PIR]    state = "));
  Serial.println(digitalRead(PIN_PIR) ? F("MOTION") : F("idle"));
#endif

#ifdef TEST_BUTTON
  // Sample 200ms; report 'pressed' if it was low at any point.
  bool pressed = false;
  for (int i = 0; i < 20; i++) {
    if (digitalRead(PIN_BUTTON) == LOW) { pressed = true; break; }
    delay(10);
  }
  Serial.print(F("[BTN]    "));
  Serial.println(pressed ? F("PRESSED") : F("released"));
#endif

#ifdef TEST_ULTRASONIC
  float d = readDistanceCm();
  Serial.print(F("[US]     distance = "));
  if (d < 0) Serial.println(F("out of range / no echo"));
  else { Serial.print(d, 1); Serial.println(F(" cm")); }
#endif

#ifdef TEST_DHT22
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  Serial.print(F("[DHT22]  temp = "));
  if (isnan(t)) Serial.print(F("nan")); else { Serial.print(t, 1); Serial.print(F(" C")); }
  Serial.print(F("  humidity = "));
  if (isnan(h)) Serial.println(F("nan")); else { Serial.print(h, 1); Serial.println(F(" %")); }
#endif

#ifdef TEST_LDR
  int raw = analogRead(PIN_LDR);
  Serial.print(F("[LDR]    raw = "));
  Serial.print(raw);
  Serial.print(F("  ("));
  if      (raw < 200)  Serial.print(F("dark"));
  else if (raw < 600)  Serial.print(F("dim"));
  else if (raw < 900)  Serial.print(F("bright"));
  else                 Serial.print(F("very bright"));
  Serial.println(F(")"));
#endif

  Serial.println(F("---"));
  delay(1500);  // pause between cycles so you can read the log
}
