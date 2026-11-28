# Log

Hours spent, what the agent got wrong, what I fixed by hand. Budget: 40 h.

| Date | Hours | What | Agent mistakes / manual fixes |
|---|---|---|---|
| 2026-11-20 | 1 | Repo skeleton, goals, plan | - |
| 2026-11-24 | - | Week 1 locally (Hetzner out of capacity): CSMS, simulator, Toxiproxy, OTel, Prometheus, Loki, Grafana | Simulator v5.0 stops heartbeats and sessions when the CSMS restarts and doesn't resume after reconnecting. Fixed on the CSMS side: unknown stations get a TriggerMessage(BootNotification). Worth an upstream issue. Agent first emitted one status series per status, which would have double-counted after changes; caught before commit |
| 2026-11-24 | - | Live web page, control panel, real-world mode (Open-Meteo + local time), 6 scenarios, incident ledger | Agent mistakes, all caught before commit: a site-label call missed its argument (blank page, caught by screenshot); CSS overrode the SVG ring value (KPI rings empty); ledger dir root-owned under a non-root container (writes failed, caught by running a scenario); stop-before-start left the runner stuck (caught by a test). Open: control state is in memory, so a restart drops a running scenario without cleanup |
| 2026-11-24 | - | Round of 11 fixes: spaced chargers, neutral cars with status light, axis-aligned overhead lines, collapsible incident feed with what/response/recovery, day phases, compact panel, autopilot, Grafana link; plus ENTSO-E prices, site controller, limit arbiter, incident engine | User pasted the ENTSO-E token into chat: stopped, asked for rotation, token now only in gitignored `.env`. Agent mistakes caught before commit: controller release would have cleared other sites' limits; quiet-period timer reset on a zero timestamp (test caught it); fault incidents said "recovered" before the fault came back, now a monitoring state |
| 2026-11-24 | - | Demand engine with queues, economics with dynamic pricing, colour-coded Grafana, pan/zoom map, load colours, coloured cars, queue lane, stronger selection | Real bug found live: transactions started while the simulator shut down never got a StopTransaction, so the CSMS kept 4 connectors busy forever and the queue stalled. Fixed in the CSMS: a reboot or an Available status closes stale transactions. Agent mistakes: the Grafana uptime join broke on duplicate series after a restart; a leftover CSS rule hid the selection ring (second time CSS beat an SVG attribute this session) |
| 2026-11-24 | - | Neutral car colours, greyed cars when offline, status colours, KPI explanations, deselect on empty click, time-weighted uptime, energy per session | Uptime was a snapshot of working connectors, not uptime; now time-weighted (98.6 % over 24 h, not the 100 % shown before). Real bug found live: after a CSMS restart every running session was orphaned and connectors stayed busy forever. Fixed: the CSMS adopts unknown transactions from MeterValues, transaction ids start from the clock so they can't collide across restarts, and the demand engine takes over sessions it isn't tracking |
| 2026-11-24 | - | Status age per connector, expected finish time, stuck-connector incident and scenario | User spotted connectors stuck in Preparing. Cause: after a soft reset the simulator resumed sessions but kept reporting Preparing, and the demand engine only adopted Charging sessions, so they never finished. Now: any status with a transaction is adopted, and Preparing/Finishing for 3 min opens an incident (status request, then reset, then truck roll). Open judgment call: resetting a charger that is still delivering energy interrupts the driver; a good first candidate for a human decision |
| 2026-11-24 | - | Cheap-hour discounts, app-visible queue, site batteries, price explanation, incident times, state survives restarts | User saw 100 % uptime on areas: whole-percent rounding (99.6 % showed as 100 %) plus a tracker that reset on every restart; now one or two decimals and persisted state. Agent mistake caught on screenshot: live demand stats overwrote the site's profile field, hiding the tariff note |
| 2026-11-24 | - | Queue and incidents survive restarts | Agent mistakes caught in the live test: state loaded before the demand engine existed (test caught it); the shutdown save never ran because open page streams kept the server waiting until Docker killed it, so restores were up to a minute stale. Fixed with a 3 s graceful-shutdown cap; verified exact restore with a stream open |
| 2026-11-24 | - | 4 missing incident types (auth slow, meter drift, ignores limit, backend backlog) + cold-snap decoy, SLOs, human-in-the-loop decisions | Live tests caught 3 agent mistakes: backend stats were attached to the snapshot after the incident engine ran, so the auth rule never fired; the meter-drift band (0.75-1.33) sat inside honest simulator noise (measured 0.76-1.31), causing 3 false alarms and 3 needless calibration questions, now 0.5-2.0 with a 3x fault; "out of service" was reported as done when OCPP answered Scheduled |
| 2026-11-24 | - | Triage agent (Gemini 3.5 Flash-Lite, free tier) with human gate and eval | Real mistakes, by me and by the agent: gemini-2.5-flash-lite was retired for new keys (404), moved to 3.5; the auth incident never recovered because auth latency was only measured on card checks, now a 20 s probe; my first eval matched runs to the wrong incidents and scored runs from before the agent existed. The agent itself: called a slow-auth incident "backend" (right) but suggested restarting a backend worker (wrong fix; it defaulted to no), and claimed free vend was on while that was still a pending question |
| 2026-11-25 | - | Night of meter incidents investigated | ~39 meter-drift incidents overnight, mostly AMS-DEPOT. My first two theories were wrong (a stuck backlog fault; low power limits), both checked and rejected with data. Real cause: the SAP simulator itself sends physically impossible energy readings on long sessions (it logs e.g. 14.4 kWh in 283 s where a 22 kW charger can deliver 1.7 kWh). The detector caught real bad data but called it a drifting meter and asked for calibration visits. Now: impossible single readings are a separate "meter jump" incident (billing hold, fresh meter values, vendor ticket on repeats), excluded from the drift check. Upstream issue candidate. Also found: slow simulator calls and the Gemini call ran inside the 2 s poll loop and froze the map; both now run in their own loops |
| 2026-11-25 | - | Correction to the entry above | I was wrong that the simulator sends impossible readings. Its log line "14.4 kWh in 283 s" is its own validator comparing the session's running total with one interval's maximum (a false alarm in the simulator, 34,844 of them); the register itself rises smoothly. A trace script of mine then made the same kind of mistake (5 s polls vs 30 s updates). The overnight meter-drift ratios (2.4-5x) are still unexplained; drift incidents now carry diagnostics so the next occurrence explains itself. Upstream issue drafted in docs/upstream/ (not filed) |
| 2026-11-25 | - | Real cause of the meter incidents found: my detector | The drift check took the value it saw when a new session appeared as the session's start, but that is still the previous session's last reading (the simulator's register restarts per session). Diagnostics showed negative energy (-180 kWh in 531 s). Fixed: baseline only from the session's first real update, and register resets start a new baseline. The 13 open meter incidents were closed as false alarms with that explanation. Two earlier explanations of mine (stuck backlog fault; simulator sending impossible readings) were wrong |
| 2026-11-25 | - | Field service, fix-time estimates, incident response metrics and SLOs, handover, reports page, agent A/B and queries | Found a leak in my agent context: for fleet-wide incidents (site "ALL") the event filter let scenario events through, so the agent could read the injected fault's name. Fixed with a test; eval scoring restarted and the earlier triages are reported but not counted, so the 92 % from before doesn't stand. Gemini free tier answered 503 "high demand" several times; triages and drafts now retry a minute later instead of being lost |

## 2026-11-25: 24 meter-jump incidents closed by a restart bug
A redeploy at 13:29:31Z restored a timer saved by the previous build as process-clock time and read it as wall time, so the 30-minute quiet check passed unchecked and 24 incidents closed at once with recovery times of 3-5 min. Fixed: timers are saved tagged as wall time, recovery time comes from the stored opening time, and a dropped timer restarts the quiet period. The 24 records were corrected in place ("closed by bug", true open time, billing released at review).

## 2026-11-25: empty map on the server
Every deploy rebuilt the simulator from its git source, which recreated it and cut all charging sessions (73
"interrupted", 0 completed). After a restart the map refilled only through new arrivals (~56 cars/h), and the warm
start was skipped whenever any state had been restored. Fixed: deploys build only our services; the first tick tops
each site up to half an hour of arrivals, and a site whose sessions are all cut at once refills when its chargers
are back online.

## 2026-11-25: Caddy config changes not applied; short outage while fixing it
The Caddyfile was bind-mounted as a single file; rsync replaces files, so the container kept the old one and a
reload changed nothing. Moving the mount to the folder at /etc/caddy failed (the secrets mount can't sit inside a
read-only folder) and the site was down for about 2 minutes. Now the folder is mounted at /etc/caddy/conf.

## 2026-11-25: delivered energy stuck at 0, cars never left
The demand engine took each session's start as the register reading seen when the transaction appeared. That was
still the previous session's last value, and these chargers count per session, so "delivered" stayed at 0 for 19 of
46 sessions and those cars never reached their target (long queues, 3.5 h app waits). Same stale-baseline mistake as
the meter drift check. Fixed: use the CSMS's per-session energy (meter at StartTransaction); the register fallback
rebases when the register restarts.

## 2026-11-26: map mostly empty again: cars left seconds after plugging in
The CSMS kept the previous session's last meter reading when a new session started. These chargers restart their
register per session (meter start 0), so "energy this session" was the old total (often 20-40 kWh) from the first
second, the demand engine saw the car as full and stopped it. A2-HUB and UTR-RETAIL showed 0 cars at 26 and 15
arrivals per hour; UTR-RETAIL counted 1,918 completed sessions for 326 starts. The previous fix (use the CSMS
per-session figure) moved the same stale-baseline mistake into the CSMS: third time for this bug. Fixed: a new
session's reading starts at its own meter start, with a test.

## 2026-11-27: cars queued at A2-HUB next to free chargers
Three A2-HUB chargers showed Available but still carried a transaction, so the demand engine never used them (and
never took them over, because they said Available). Cause: the simulator keeps sending MeterValues for a session
after reporting Available; the CSMS closed the transaction, then read the late reading as a session from before a
CSMS restart and took it over again. My first fix (never take over a closed transaction) broke restart recovery: after
a restart the CSMS asks every charger to boot, that boot closes all transactions, and 10 sessions that were still
charging could no longer be taken back. Final fix: take over an unknown transaction only while the charger reports
it may be charging, and drop readings for transactions the connector doesn't own. Tests for both cases.

## 2026-11-28: weekends, hourly firmware, daily NIS2/CRA report
Weekends ran like weekdays: demand followed the hour only. Now Saturday, Sunday and Dutch public holidays have their
own profiles (office closed, Saturday shopping peak, Sunday shops from 12, no commute, vans parked).

Firmware rollout found two races in the live test, both by the coding agent. At startup the demand engine's warm
start and the rollout picked the same free chargers in the same second, and the simulator starts a session on an
Unavailable connector anyway, so two "installing" chargers were charging cars. Fix: the demand engine skips chargers
being updated, and an install yields to a driver who plugged in at the same moment. Second: a scenario's fault
cleanup forced an updating charger back to Available; the rollout now re-asserts out of service.

Agent mistake: rebuilt with `docker compose up -d --build control`, which also recreated the simulator and cut every
session three times. I first blamed the startup proxy cleanup for the all-site "lost contact" blips; wrong, it was
the rebuild. Use `--no-deps` when rebuilding one service.

## 2026-11-28: negative margin every day
Server data, 26-28 Nov: margin -€1,186, -€2,386 and -€93 (so far). Most of it is technician visits: about €2,100,
€5,200 and €1,350 a day (14, 35 and 9 visits at €150), mainly at A2-HUB and UTR-RETAIL. The rest is structural: the
depot sold at a fixed €0.32/kWh while grid power cost spot + €0.11, and the last 24 h averaged €265/MWh, so every
depot kWh lost money (-€74 and -€94 a day on energy alone); the office broke even. Fix for the second part: depot and
office contracts now follow cost (+€0.06 and +€0.10 per kWh). Lost revenue is not part of margin.
