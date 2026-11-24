# Log

Hours spent, what the agent got wrong, what I fixed by hand. Budget: 40 h.

| Date | Hours | What | Agent mistakes / manual fixes |
|---|---|---|---|
| 2026-11-20 | 1 | Repo skeleton, goals, plan | - |
| 2026-11-24 | - | Week 1 locally (Hetzner out of capacity): CSMS, simulator, Toxiproxy, OTel, Prometheus, Loki, Grafana | Simulator v5.0 stops heartbeats and sessions when the CSMS restarts and doesn't resume after reconnecting. Fixed on the CSMS side: unknown stations get a TriggerMessage(BootNotification). Worth an upstream issue. Agent first emitted one status series per status, which would have double-counted after changes; caught before commit |
| 2026-11-24 | - | Live web page, control panel, real-world mode (Open-Meteo + local time), 6 scenarios, incident ledger | Agent mistakes, all caught before commit: a site-label call missed its argument (blank page, caught by screenshot); CSS overrode the SVG ring value (KPI rings empty); ledger dir root-owned under a non-root container (writes failed, caught by running a scenario); stop-before-start left the runner stuck (caught by a test). Open: control state is in memory, so a restart drops a running scenario without cleanup |
