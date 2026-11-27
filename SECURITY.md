# Security policy

## Reporting a vulnerability

Report privately through GitHub: **Security → Report a vulnerability** on this repository. Please don't open a
public issue for a security problem.

What to expect (coordinated disclosure):

- Acknowledgement within 5 working days.
- An assessment and, if it's real, a fix plan within 14 days.
- Public disclosure once a fix is out, or after 90 days, whichever comes first. Credit if you want it.

## Scope

In scope: the code in this repository and the public demo at `truckroll.siniaev.dev`.

Not a vulnerability, by design:

- The demo has no login. Anyone can drive the simulation, answer questions and trigger scenarios. Abuse limits
  apply instead (`control/src/control/guard.py`). Getting past those limits, or reaching anything beyond the
  simulation (the host, other services, secrets, the AI key), is in scope.
- Grafana is public and read-only.

Please don't run load tests or automated scanners against the public demo; it's one small server.

## Supply chain

Every push builds a CycloneDX SBOM (Syft) and scans it (Grype), failing on high severity
([`.github/workflows/supply-chain.yml`](.github/workflows/supply-chain.yml)). Actions are pinned to commit SHAs and
kept current by Dependabot.
