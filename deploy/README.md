# Server deploy (Hetzner CX23, Ubuntu)

Code is copied with rsync (no GitHub credentials on the server). Secrets exist only on the server:
`/opt/truckroll/.env` (API keys).

```bash
TRUCKROLL_SERVER=user@host deploy/push.sh   # rsync the repo, then build and restart on the server
```

Access: open, no login. Anyone can view the page and reports and use the control panel (it only drives the
simulation), within the rate limits in `control/src/control/guard.py`; Grafana is read-only. Grafana: https://truckroll.siniaev.dev/grafana/. Prometheus has no public route; use a tunnel:

```bash
ssh -N -L 3001:localhost:3000 -L 9091:localhost:9090 "$TRUCKROLL_SERVER"
```

Then Grafana is at http://localhost:3001 and Prometheus at http://localhost:9091 (3001/9091, so they don't clash
with a local stack on 3000/9090).

Security updates install on their own; if one needs a reboot, the server reboots at 04:00 UTC (about 35 s down,
state is restored).
