# Draft issue: SAP e-mobility charging stations simulator

**Status: draft, not filed.** Target: https://github.com/SAP/e-mobility-charging-stations-simulator/issues
(checked 2026-11-25: no open or closed issue mentions `validateMeasurandValue`).

---

**Title:** `validateMeasurandValue` logs an error on almost every sample: compares the cumulative
`Energy.Active.Import.Register` with the energy of one sample interval

**Version:** 5.0.1 (docker image built from tag `v5.0`, `docker/Dockerfile`), Node.js 26.

**Setup**
- 50 stations from custom templates, OCPP 1.6J. Example (AC): `power: 22080`, `powerUnit: W`, `currentOutType: AC`,
  `numberOfConnectors: 1`, `MeterValueSampleInterval: 30`,
  `MeterValuesSampledData: Energy.Active.Import.Register,Power.Active.Import`, connector `MeterValues` with
  `Energy.Active.Import.Register` (Wh) and `Power.Active.Import` (W), both `Sample.Periodic`.
- Automatic transaction generator disabled; transactions started and stopped over the UI WebSocket protocol
  (`startTransaction` / `stopTransaction`). Some stations run charging profiles (ChargePointMaxProfile).

**What happens**

During normal charging, `OCPPServiceUtils.validateMeasurandValue` logs at error level on nearly every periodic
sample once a transaction is a few minutes old:

```
error: ... AMS-DEPOT-00012 | OCPPServiceUtils.validateMeasurandValue: MeterValues measurand Energy.Active.Import.Register: connector id 1, transaction id 1795555161, value: 0/14404.72/1737.08, duration: 283219ms
error: ... AMS-DEPOT-00006 | OCPPServiceUtils.validateMeasurandValue: MeterValues measurand Energy.Active.Import.Register: connector id 1, transaction id 7, value: 0/44272.30/5699.15, duration: 929210ms
```

Over about 17 hours with 50 stations: **34,844** such errors across 234 transactions and 48 stations, on every
station type (AC 11 kW, AC 22 kW, DC 150 kW).

**Why we think it's the check, not the meter**

The sent register values are fine: for a given transaction they rise smoothly and never go down (two
transactions, 565 and 551 consecutive samples, increments of roughly 20-90 Wh per 30 s, never decreasing). In
the log line, `duration` is almost always the 30 s sample interval and the third number is the energy the
connector could deliver in that interval (e.g. 92 Wh at 11 kW over 30 s). The middle number is the
transaction's **cumulative** register (e.g. 311, 647, 706, 1448 Wh in successive samples). So the check compares
a running total with one interval's maximum, and fails as soon as the total exceeds what 30 s can deliver.

**Expected**

Either validate the increment since the previous sample against `max power x interval`, or validate the
cumulative register against `max power x time since transaction start`. With the current check the error log
fills with false alarms and real problems are hard to see.

**Impact for us**

We cross-check energy against integrated power in our backend; the error volume made us first suspect the
simulator's meter values, which turned out to be correct.

Happy to test a fix or send a PR if you agree on the approach.

---

Evidence on file (local, not committed): `/tmp/tr-gen/sim-errors.txt` (unique error lines), analysis scripts in
the session log.
