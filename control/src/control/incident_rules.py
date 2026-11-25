"""Detectors for backend and metering problems. Each works only from what the system can observe: backend
latency stats, session start failures, meter readings and power limits. None of them reads the injected chaos."""

from __future__ import annotations

from collections.abc import Mapping

AUTH_SLOW_S = 5.0
AUTH_OK_S = 1.0
REPLY_SLOW_S = 5.0
REPLY_OK_S = 0.5
RECOVER_AFTER_S = 60
DRIFT_MIN_S = 300  # watch a session this long before judging its meter
DRIFT_MIN_KWH = 3.0
# Energy register vs integrated power. Honest simulated meters measured 0.76-1.31 over 5 min windows (median 1.01),
# so only clear errors are flagged; impossible single readings are handled as jumps, not drift.
DRIFT_BAND = (0.5, 1.6)
OVER_LIMIT_FACTOR, OVER_LIMIT_W, OVER_LIMIT_S = 1.15, 2000, 60
JUMP_HEADROOM, JUMP_SLACK_WH = 1.25, 100  # a reading may exceed rated power x time by this much before it's impossible
JUMP_REPEAT, JUMP_WINDOW_S, JUMP_QUIET_S = 3, 3600, 1800


def _calm_for(inc, ok: bool, now: float) -> bool:
    if not ok:
        inc.memo.pop("calm_since", None)
        return False
    inc.memo.setdefault("calm_since", now)
    return now - inc.memo["calm_since"] >= RECOVER_AFTER_S


async def auth_backend(m, snapshot: Mapping) -> None:
    backend = snapshot.get("backend") or {}
    latency = backend.get("auth_latency_s")
    if latency is None:
        return
    key = "auth-slow"
    failing = {s["code"]: (s.get("demand") or {}).get("recent_start_failures", 0) for s in snapshot.get("sites", [])}
    if latency >= AUTH_SLOW_S and key not in m.open:
        inc = m._new(key, "auth_slow", "critical", "Sessions failing to start: RFID auth backend slow", "ALL", [])
        hit = ", ".join(f"{c} {n}" for c, n in failing.items() if n) or "none yet"
        inc.add("detected", f"Each card check takes {latency:.0f} s, past the chargers' timeout. Recent failed "
                            f"starts: {hit}.", short="auth slow")
        await m._ask(inc, "free_vend", {}, "Turn on free vend (accept every card) until auth recovers?",
                     "Drivers can charge again, but sessions go unchecked for that time.", timeout_s=45)
        return
    if key not in m.open:
        return
    inc = m.open[key]
    if _calm_for(inc, latency <= AUTH_OK_S, m.clock()):
        if inc.memo.get("free_vend"):
            await m.clients.csms.chaos(free_vend=False)
            inc.add("response", "Auth backend healthy again: free vend off.", "auto-rule", short="free vend off")
        m._close(key, f"Card checks back to {latency:.1f} s; sessions start normally.", short="auth ok")


async def backend_backlog(m, snapshot: Mapping) -> None:
    backend = snapshot.get("backend") or {}
    latency = backend.get("reply_latency_s")
    if latency is None:
        return
    key = "backlog"
    if latency >= REPLY_SLOW_S and key not in m.open:
        inc = m._new(key, "backlog", "critical", "Backend overloaded: replies to chargers delayed", "ALL", [])
        inc.add("detected", f"The backend takes {latency:.0f} s to answer each charger message. Chargers stay "
                            f"connected but their messages pile up.", short="backend slow")
        await m._ask(inc, "restart_worker", {}, "Restart the stuck backend worker?",
                     "Chargers reconnect within seconds; a few in-flight messages are retried.", timeout_s=30)
        return
    if key in m.open and _calm_for(m.open[key], latency <= REPLY_OK_S, m.clock()):
        m._close(key, f"Replies back to {latency:.1f} s.", short="backend ok")


def _rated_w(m, site: str) -> float:
    return float(m.sites.get(site, {}).get("power_w") or 22_000)


async def meter_drift(m, snapshot: Mapping, now: float) -> None:
    """Two checks per session. Jump: one reading rose more than the charger can physically deliver in the time
    since the last one (bad data or firmware). Drift: over 5+ minutes, plausible readings disagree with the
    integrated power (a meter reading wrong)."""
    live = set()
    jumps: list[tuple[str, str, float, float, float]] = []
    for st in snapshot.get("stations", []):
        for c in st.get("connectors", []):
            tx = c.get("transaction_id")
            if not tx or not st.get("connected"):
                continue
            k = f"{st['id']}#{c['id']}"
            live.add(k)
            energy = c.get("energy_wh") or 0.0
            w = m.meters.get(k)
            if w is None or w["tx"] != tx:
                # The value seen when a session appears can be the previous session's last reading: wait for this
                # session's first real update before setting a baseline.
                m.meters[k] = {"tx": tx, "seen": energy, "armed": False, "station": st["id"], "site": st["site"]}
                continue
            if not w["armed"]:
                if energy == w["seen"]:
                    continue
                w.update({"armed": True, "t0": now, "last": now, "e0": energy, "integral": 0.0, "e_prev": energy,
                          "t_prev": now, "jumped_wh": 0.0, "e_updates": 0, "p_min": None, "p_max": None,
                          "limits": set(), "gap_max": 0.0})
                continue
            if energy < w["e_prev"]:
                # Registers reset (new session, reboot): that's not drift. Start a fresh baseline.
                w.update({"t0": now, "last": now, "e0": energy, "integral": 0.0, "e_prev": energy, "t_prev": now,
                          "jumped_wh": 0.0, "e_updates": 0})
                continue
            power = c.get("power_w") or 0.0
            w["integral"] += power * (now - w["last"]) / 3600
            w["gap_max"] = max(w["gap_max"], now - w["last"])  # long gaps mean the poll loop stalled
            w["last"] = now
            w["p_min"] = power if w["p_min"] is None else min(w["p_min"], power)
            w["p_max"] = power if w["p_max"] is None else max(w["p_max"], power)
            w["limits"].add(st.get("limit_w"))
            if energy != w["e_prev"]:
                rise, dt = energy - w["e_prev"], now - w["t_prev"]
                allowed = _rated_w(m, st["site"]) * dt / 3600 * JUMP_HEADROOM + JUMP_SLACK_WH
                if rise > allowed:
                    jumps.append((st["id"], st["site"], rise, dt, allowed))
                    w["jumped_wh"] += rise  # keep impossible readings out of the drift check
                w["e_prev"], w["t_prev"] = energy, now
                w["e_updates"] += 1
            w["energy"] = energy
    for k in [k for k in m.meters if k not in live]:
        m.meters.pop(k)
    for sid, site, rise, dt, allowed in jumps:
        await _meter_jump(m, sid, site, rise, dt, allowed, now)

    drifting: dict[str, float] = {}
    for w in m.meters.values():
        if not w.get("armed"):
            continue
        if now - w["t0"] < DRIFT_MIN_S or w["integral"] < DRIFT_MIN_KWH * 1000 or "energy" not in w:
            continue
        ratio = (w["energy"] - w["e0"] - w["jumped_wh"]) / w["integral"]
        if not DRIFT_BAND[0] <= ratio <= DRIFT_BAND[1]:
            drifting[w["station"]] = ratio
            m.meter_diag[w["station"]] = (
                f"Diagnostics: window {now - w['t0']:.0f} s, energy {w['energy'] - w['e0']:.0f} Wh vs integrated power "
                f"{w['integral']:.0f} Wh, {w['e_updates']} energy updates, power {w['p_min'] or 0:.0f}-{w['p_max'] or 0:.0f} W, "
                f"{len(w['limits'])} different limits, longest gap between checks {w['gap_max']:.0f} s.")
    for sid, ratio in drifting.items():
        key = f"meter:{sid}"
        if key in m.open:
            continue
        site = sid.rsplit("-", 1)[0]
        inc = m._new(key, "meter_drift", "warning", f"{sid} meter disagrees with its own power readings", site, [sid])
        inc.add("detected", f"Over 5+ minutes the energy register moved {ratio:.2f}x what its power readings add up "
                            f"to: drivers may be billed wrongly.", short="meter drift")
        if sid in m.meter_diag:
            inc.add("update", m.meter_diag.pop(sid), short="diagnostics")
        inc.add("response", "Billing hold on this charger's sessions until the meter is checked.", "auto-rule",
                short="billing hold")
        await m._ask(inc, "calibration", {"station": sid}, f"Book a meter calibration visit for {sid}?",
                     "A steady disagreement points at the meter itself (about €150); the charger keeps working "
                     "meanwhile.", timeout_s=90)
    for key in [k for k in m.open if k.startswith("meter:")]:
        sid = key.split(":", 1)[1]
        watched = [w for w in m.meters.values() if w["station"] == sid and w.get("armed") and now - w["t0"] >= DRIFT_MIN_S]
        if watched and sid not in drifting and _calm_for(m.open[key], True, now):
            m._close(key, "Energy and power readings agree again; billing hold lifted.", short="meter ok")
    for key in [k for k in m.open if k.startswith("jump:")]:
        inc = m.open[key]
        if now - inc.memo.get("last_jump", now) >= JUMP_QUIET_S:
            m._close(key, f"No impossible readings for {JUMP_QUIET_S // 60} min; affected sessions stay on billing "
                          f"hold for review.", short="readings ok")


async def _meter_jump(m, sid: str, site: str, rise: float, dt: float, allowed: float, now: float) -> None:
    key = f"jump:{sid}"
    detail = (f"Energy rose {rise / 1000:.1f} kWh in {dt:.0f} s; a {_rated_w(m, site) / 1000:.0f} kW charger can "
              f"deliver at most {allowed / 1000:.1f} kWh in that time.")
    inc = m.open.get(key)
    if inc is None:
        inc = m._new(key, "meter_jump", "warning", f"{sid} meter reported an impossible reading", site, [sid])
        inc.add("detected", detail + " That's bad data (firmware or meter communication), not a slow meter.",
                short="impossible reading")
        inc.add("response", "Billing hold on the affected session; asked the charger for fresh meter values.",
                "auto-rule", short="billing hold")
        try:
            await m.clients.csms.trigger(sid, "MeterValues")
        except Exception as exc:  # noqa: BLE001 - a failed request must not stop detection
            inc.add("response", f"Meter values request failed: {exc}.", "auto-rule", short="request failed")
        inc.memo["jumps"] = []
    inc.memo["jumps"] = [t for t in inc.memo.get("jumps", []) if now - t <= JUMP_WINDOW_S] + [now]
    inc.memo["last_jump"] = now
    if len(inc.memo["jumps"]) > 1:
        inc.add("update", f"Again: {detail}", short="again")
    if len(inc.memo["jumps"]) >= JUMP_REPEAT and not m._asked(inc, "vendor_ticket"):
        await m._ask(inc, "vendor_ticket", {"station": sid}, f"Open a firmware ticket with the vendor for {sid}?",
                     f"{len(inc.memo['jumps'])} impossible readings within an hour: likely a firmware or meter "
                     f"communication bug. A technician visit wouldn't fix it.", timeout_s=90)


async def ignores_limit(m, snapshot: Mapping, now: float) -> None:
    for st in snapshot.get("stations", []):
        sid, limit = st["id"], st.get("limit_w")
        key = f"limit:{sid}"
        over = (st.get("connected") and limit is not None
                and st.get("power_w", 0) > limit * OVER_LIMIT_FACTOR + OVER_LIMIT_W)
        if over:
            m.over_limit_since.setdefault(sid, now)
        else:
            m.over_limit_since.pop(sid, None)
        since = m.over_limit_since.get(sid)
        if over and since is not None and now - since >= OVER_LIMIT_S and key not in m.open:
            inc = m._new(key, "ignores_limit", "critical", f"{sid} ignores its power limit", st["site"], [sid],
                         symptom_age_s=now - since)
            inc.add("detected", f"It draws {st['power_w'] / 1000:.0f} kW against a {limit / 1000:.0f} kW limit "
                                f"for {OVER_LIMIT_S} s: the limit was accepted but not applied.", short="over limit")
            if m.limits is not None:
                await m.limits.reapply(sid)
            inc.add("response", "Re-sent the limit (OCPP SetChargingProfile).", "auto-rule", short="re-sent limit")
            inc.memo["resent_at"] = now
            continue
        if key not in m.open:
            continue
        inc = m.open[key]
        if not over:
            if _calm_for(inc, True, now):
                m._close(key, "Power back under the limit.", short="under limit")
        elif now - inc.memo.get("resent_at", now) >= OVER_LIMIT_S and not m._asked(inc, "inoperative"):
            await m._ask(inc, "inoperative", {"station": sid}, f"Take {sid} out of service?",
                         "It still ignores its limit and can push the site over its grid cap; the driver's "
                         "session ends.", timeout_s=45)
