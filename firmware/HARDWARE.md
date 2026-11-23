# Hardware

Low voltage only: 12 V DC max. The relay is rated for mains, but it only ever switches 12 V here.

## Inventory -> role

| Have | Role in the charger | Notes |
|---|---|---|
| ESP32 DevKitC V4 (WROOM-32) | Charger controller: MicroOcpp, Wi-Fi, TLS to Hetzner | 4 MB flash: use `min_spiffs` partitions for OTA |
| IRLZ44N | Dims the "car" load with PWM | Logic level, OK at 3.3 V gate for a few amps. 100 Ω gate resistor, 10 k gate-to-GND pull-down |
| IRL3103 | Spare for the same job | Also logic level |
| IRFZ44N, P16NF06 | Not used | Need ~10 V on the gate; won't switch fully from 3.3 V |
| 7БЕ0 (?) | Unknown | Check the full marking |
| 3-channel relay | **Contactor**: clicks closed when charging starts, opens on stop or fault | Real chargers have one; the click is great on camera. Check the module triggers from 3.3 V (many are active-low, 5 V coil) |
| Latching button | "Cable plugged in" -> status `Preparing` | Replaces the reed switch |
| Momentary button 1 | Spare start/stop (if RFID misbehaves) | |
| Momentary button 2 | Fault injection -> `StatusNotification: Faulted` | |
| RGB LED (or 3 single LEDs) | Status: green Available, blue Charging, red Faulted, amber grid stress | 220 Ω per channel |
| 3 V LEDs | Extra indicators (Wi-Fi/OCPP connected, contactor closed) | |
| Raspberry Pi 3B + Logitech webcam | **Camera box**: webcam -> H.264 stream pushed out to the Hetzner relay | Replaces the ESP32-CAM; hardware encode, 720p is fine |
| Small screen | kW, kWh, price, grid cap on the device | Check the model: SSD1306/SH1106 OLED (I2C) or a 1602 LCD with I2C backpack. Both work |
| Keyestudio RFID-RC522 + cards | Real badge-in -> `Authorize` | Check the pins: SDA/SCK/MOSI/MISO/RST = SPI (pin plan below); only SDA/SCL = I2C variant, goes on the shared bus |
| DS18B20 (Keyestudio module) | Hairdryer -> real derate | Module usually has the pull-up on board |
| DHT11 (Keyestudio) | Enclosure temperature + humidity as extra telemetry | Optional; slow (1 reading / 2 s) |
| Keyestudio sensor kit | Spares; possible extra fault sources later | |
| 12 V LED strip or lamp | The "car" that dims | Finding it; needs a 12 V supply |

## Still needed

| Part | Why | Priority |
|---|---|---|
| INA226 (or INA219) module | Real voltage/current for `MeterValues` | must |
| Buck converter 12 V -> 5 V | Power the ESP32 from the same supply | should (USB works meanwhile) |
| 12 V 2 A supply | Only if the LED strip doesn't come with one | if needed |
| WS2812 ring (8-16 LEDs) | Prettier status on camera than one RGB LED | nice |
| Breadboard, jumpers, resistors (100 Ω, 220 Ω, 10 k), 1N4007 | Wiring; diode across the relay coil if the module lacks one | if not on hand |

## Pin plan (ESP32 DevKitC V4)

| Function | GPIO | Notes |
|---|---|---|
| I2C SDA / SCL (OLED + INA) | 21 / 22 | shared bus |
| Load PWM -> IRLZ44N gate | 25 | LEDC, ~1 kHz |
| Relay (contactor) | 26 | check active level |
| Cable plugged (latching) | 32 | `INPUT_PULLUP`, to GND |
| Badge button | 33 | `INPUT_PULLUP`, to GND |
| Fault button | 27 | `INPUT_PULLUP`, to GND |
| RGB LED R / G / B | 16 / 17 / 4 | LEDC PWM |
| DS18B20 data | 13 | 4.7 k pull-up to 3.3 V |
| RC522 SPI SCK / MISO / MOSI / CS / RST | 18 / 19 / 23 / 5 / 14 | later |
| Control pilot (stretch) | 34 | ADC1 only; ADC2 is dead with Wi-Fi on |

Avoid GPIO 0, 2, 12, 15 (boot strapping) and 6-11 (flash).

## Charging sequence on the desk

1. Latch "cable" -> RGB amber, `Preparing`.
2. Press "badge" -> `Authorize` -> relay clicks closed -> `StartTransaction` -> RGB blue, lamp on.
3. CSMS or site controller sets a charging profile -> lamp dims to the limit; INA reading drops to match.
4. Press "badge" again or unlatch -> relay opens -> `StopTransaction` -> green.
5. Fault button -> relay opens, RGB red, `Faulted`.
