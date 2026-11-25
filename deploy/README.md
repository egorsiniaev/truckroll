# Server deploy (Hetzner CX23, Ubuntu)

Code is copied with rsync (no GitHub credentials on the server). Secrets exist only on the server:
`/opt/truckroll/.env` (API keys) and `/etc/truckroll/auth.caddy` (login hash).

```bash
deploy/push.sh        # rsync the repo, then build and restart on the server
```

Grafana and Prometheus have no public port. Open them through an SSH tunnel:

```bash
ssh -N -L 3000:localhost:3000 -L 9090:localhost:9090 root@truckroll.siniaev.dev
```
