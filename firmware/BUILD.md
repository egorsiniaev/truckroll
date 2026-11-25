# Building the desk charger

What it looks like: [docs/desk-scene.svg](../docs/desk-scene.svg). Parts and pins: [HARDWARE.md](HARDWARE.md).
Low voltage only: one 12 V supply, nothing touches mains.

Order: test every part on the breadboard first, then print, then fit everything into the case. A part that fails
on the breadboard costs 5 minutes; inside a glued case it costs an evening.

Status of what I still owe you (each lands before you reach that step):

| Step | Needs from me | Ready |
|---|---|---|
| 1-3 | nothing | yes |
| 4 | `bringup` firmware: a serial menu that tests each part | next |
| 6 | OCPP firmware + the desk station in the CSMS | after bring-up works |
| 7 | OpenSCAD files + STLs for every printed part | before you print |

## 0. What's where

```
 12 V supply ─► GRID BOX ──(wires under the base)──► CHARGER ──GX12 cable──► TAXI
                 DC jack, LM2596 (5 V)              ESP32, relay, INA226,    LED strip
                 ring 2 on top                      MOSFET, OLED, ring 1,
                                                    RC522, buttons, DS18B20
```

Power really comes "from the grid": the 12 V input sits in the grid box, like a site's grid connection.

### Power path to the car (the "charging" current)

```
12 V ─► relay (contactor) ─► INA226 VIN+ → VIN- ─► GX12 pin 1 ─► strip + (in the taxi)
                                                  GX12 pin 2 ◄─ strip -
                                                      │
                                                IRLZ44N drain, source ─► GND   (PWM = charging limit)
GX12 pin 3 ─► GPIO32 (pull-up)      GX12 pin 4 ─► GND      (bridged 3-4 inside the taxi = plug present)
```

### Budget

| Load | Current |
|---|---|
| LED strip, 12 V | 0.5-0.7 A (cut to about 40 cm) |
| ESP32 + OLED + RC522 + INA226, 5 V | about 0.3 A |
| 2 rings, 32 LEDs, 5 V, brightness capped at 25 % in code | about 0.5 A (2 A at full white: never) |
| From the 12 V supply in total | about 1.2 A: a 12 V 2 A supply is enough |

## 1. Check the parts (15 min)

- ESP32 DevKitC V4, LM2596, IRLZ44N, 3-channel relay, INA226, OLED, RC522 + 3 cards, DS18B20, 74AHCT125,
  2 rings (16 LEDs, 68 mm), 2 momentary buttons, LED strip, 12 V 2 A supply, 2 GX12 4-pin pairs.
- Small stuff: 330 Ω, 100 Ω, 10 kΩ, 4.7 kΩ resistors, 1000 µF capacitor (10 V or more), jumpers, heat inserts M3.
- Tools: multimeter, soldering iron, heat shrink, step drill bit (for the taxi), hot glue.

## 2. Power first (20 min)

1. Connect the 12 V supply to the LM2596 input. **Nothing else connected.**
2. Turn the LM2596 screw until the output reads **5.0 V** on the multimeter.
3. Only then connect 5 V to the ESP32 `5V` pin and GND to `GND`. Never feed USB and the 5 V pin at the same time:
   unplug one before plugging the other.

Check: ESP32 power LED on, 5.0 V still on the output.

## 3. Breadboard wiring, one part at a time (2 h)

Wire one part, test it in step 4, then add the next. Pins are in [HARDWARE.md](HARDWARE.md#pin-plan-esp32-devkitc-v4).

| # | Part | Wiring | Watch out |
|---|---|---|---|
| a | OLED | VCC 3V3, GND, SDA 21, SCL 22 | 3.3 V |
| b | INA226 | VCC 3V3, GND, SDA 21, SCL 22 (same bus); VIN+ / VIN- in the 12 V line to the strip | Address 0x40, OLED is 0x3C |
| c | MOSFET | GPIO25 → 100 Ω → gate; 10 kΩ gate to GND; drain to strip -; source to GND | IRLZ44N only (logic level) |
| d | Relay | IN1 → GPIO26, VCC 5 V, GND; contacts COM / NO in the 12 V line | Most modules are active-low: LOW = clicked |
| e | Rings | 5 V and GND to both rings; GPIO16 → 74AHCT125 (powered 5 V, OE to GND) → 330 Ω → ring 1 DIN; ring 1 DOUT → ring 2 DIN | 1000 µF across 5 V / GND near ring 1 |
| f | RC522 | 3V3, GND, SCK 18, MISO 19, MOSI 23, NSS 5, RST 14 | **3.3 V only**, 5 V kills it |
| g | Buttons | Fault: GPIO27 to GND. Spare badge: GPIO33 to GND | Internal pull-ups, no resistors |
| h | DS18B20 | Data GPIO13, 3V3, GND; 4.7 kΩ data to 3V3 if the module has none | |
| i | Plug detect | GX12 pin 3 → GPIO32, pin 4 → GND | Bridge 3-4 in the socket that goes into the taxi |

The strip at this stage: a short piece on the breadboard, not in the taxi yet.

## 4. Test each part with the bring-up firmware (30 min)

1. Install VS Code + the PlatformIO extension.
2. Open `firmware/`, build and upload the `bringup` environment, open the serial monitor at 115200.
3. Run each test from the menu. Done when every line is OK:

| Test | Expected |
|---|---|
| OLED | "Truckroll" on the screen |
| INA226 | 12.0 V ± 0.3, 0.00 A with the relay open |
| Relay | Click; the INA226 bus voltage stays, current still 0 until the MOSFET is on |
| MOSFET 0-100 % | Strip dims smoothly; INA226 current follows (about 0.6 A at 100 %) |
| Rings | Ring 1 runs green, blue, red, amber; then ring 2 the same |
| RFID | Each card prints its UID. Write the 3 UIDs down: they go into the CSMS as fleet, expired and unknown |
| Buttons, plug | Each press or plug in prints once (no bouncing) |
| DS18B20 | Room temperature; warm it with your fingers, the value rises |

## 5. The taxi (45 min)

1. Unscrew the base plate (usually 2-4 screws underneath).
2. Drill the GX12 hole (12 mm) in the rear side panel with the step bit. Deburr, then glue in the printed collar.
3. Fit the GX12 panel socket; solder the strip to pins 1 (+) and 2 (-), bridge pins 3 and 4.
4. Stick the strip under the roof, LEDs pointing down onto the seats. **Cover every bare joint with heat shrink:**
   the body is metal and conducts.
5. Optional roof sign: a single 12 V LED segment in the sign, in parallel with the strip.
6. Before closing: plug in, check plug detect and dimming from the bring-up menu.

## 6. Connect to the CSMS (30 min)

1. Upload the `charger` environment (OCPP firmware).
2. First on your Wi-Fi to the stack on the Mac, then over TLS to `truckroll.siniaev.dev`.
3. Done when: the desk charger appears on the live map as its own site and sends a heartbeat every 30 s.

## 7. Print (about 12 h of printer time, mostly unattended)

PLA, 0.2 mm layers, 15 % infill unless noted. Anycubic Slicer Next. White or natural PLA for the ring diffusers.

| Part | Size (mm) | Orientation | Notes |
|---|---|---|---|
| Charger body | 90 x 55 x 200 | upright | M3 inserts for the front and the cap |
| Charger front plate | 90 x 200 x 2.5 | flat | Ring slot, OLED window, 2 mm thin tap zone, e-stop hole |
| Ring 1 diffuser | 68 / 54 ring | flat | Natural PLA, 0.8 mm |
| Charger cap | 98 x 63 | upside down | Teal |
| Base tile A, B | 250 x 180 x 8 | flat | Pin joint; wire channel underneath |
| Grid box | 80 x 80 x 70 | upright | DC jack and LM2596 inside, ring 2 in the top |
| Ring 2 diffuser | 68 / 54 ring | flat | Natural PLA |
| Card rack | 3 slots | flat | Wide slots: the robot will use it later |
| GX12 collar + holster | small | flat | Collar for the taxi, holster on the charger |
| E-stop cap | 25 dia | flat | Red, press-fit on the fault button |

## 8. Assemble (1.5 h)

1. Heat inserts into the charger body and grid box.
2. Charger: front plate with ring 1 + diffuser, OLED behind its window, RC522 behind the tap zone, e-stop.
   ESP32, relay, INA226, MOSFET on a small perfboard at the back; DS18B20 next to the MOSFET ("power module").
3. Grid box: DC jack, LM2596, ring 2 under its diffuser.
4. Wires between grid box and charger in the base channel: 12 V, GND, 5 V, ring data.
5. GX12 cable from the charger to the taxi; holster on the charger side.

## 9. Scenario tests (done when all pass on camera)

| # | Do | Expect |
|---|---|---|
| 1 | Plug in, tap fleet card, wait 1 min, unplug | Amber, click, blue, taxi glows; receipt with kWh and € on the OLED; session on the map |
| 2 | Start a price spike or grid request on the control panel | Taxi dims, ring 2 amber or pulsing, INA226 current drops to match |
| 3 | Tap the expired card, then the unknown one | Red flash, "Card blocked" / "Unknown card", no click |
| 4 | Start "auth slow", tap the fleet card | Charges anyway from the local card list |
| 5 | Hairdryer on the charger for a minute | Charger lowers its own power; "derate" incident, marked expected |
| 6 | Press the e-stop while charging | Relay opens, red; incident; approve "remote reset" on the page; back to normal |
| 7 | Switch Wi-Fi off mid-session for 2 min, then on | Keeps charging; the session arrives in the CSMS after reconnecting |

## Safety

- One 12 V supply with its own fuse. Nothing on the desk carries more than 12 V.
- Set the LM2596 to 5 V before anything is connected to it.
- The taxi body is metal: insulate every joint inside it.
- The MOSFET gets warm at 100 % for a long time: leave the back of the charger open or add vent slots.
