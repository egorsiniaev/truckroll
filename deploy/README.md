# Server deploy (Hetzner CX23, Ubuntu)

Code is copied with rsync (no GitHub credentials on the server). Secrets exist only on the server:
`/opt/truckroll/.env` (API keys) and `/etc/truckroll/auth.caddy` (login hash).

```bash
deploy/push.sh        # rsync the repo, then build and restart on the server
```

Grafana: https://truckroll.siniaev.dev/grafana/ (same login). Prometheus has no public route; use a tunnel:

```bash
ssh -N -L 3001:localhost:3000 -L 9091:localhost:9090 root@142.132.244.71
```

Then Grafana is at http://localhost:3001 and Prometheus at http://localhost:9091 (3001/9091, so they don't clash
with a local stack on 3000/9090).
