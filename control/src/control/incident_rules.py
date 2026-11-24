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
# so only gross errors are flagged.
DRIFT_BAND = (0.5, 2.0)
OVER_LIMIT_FACTOR, OVER_LIMIT_W, OVER_LIMIT_S = 1.15, 2000, 60


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


async def meter_drift(m, snapshot: Mapping, now: float) -> None:
    """Cross-check each session's energy register against the power it reported, integrated over time."""
    live = set()
    for st in snapshot.get("stations", []):
        for c in st.get("connectors", []):
            tx = c.get("transaction_id")
            if not tx or not st.get("connected"):
                continue
            k = f"{st['id']}#{c['id']}"
            live.add(k)
            w = m.meters.get(k)
            if w is None or w["tx"] != tx:
                m.meters[k] = {"tx": tx, "t0": now, "last": now, "e0": c.get("energy_wh") or 0.0, "integral": 0.0,
                               "station": st["id"], "site": st["site"]}
                continue
            w["integral"] += (c.get("power_w") or 0.0) * (now - w["last"]) / 3600
            w["last"] = now
            w["energy"] = c.get("energy_wh") or 0.0
    for k in [k for k in m.meters if k not in live]:
        m.meters.pop(k)

    drifting: dict[str, float] = {}
    for w in m.meters.values():
        if now - w["t0"] < DRIFT_MIN_S or w["integral"] < DRIFT_MIN_KWH * 1000 or "energy" not in w:
            continue
        ratio = (w["energy"] - w["e0"]) / w["integral"]
        if not DRIFT_BAND[0] <= ratio <= DRIFT_BAND[1]:
            drifting[w["station"]] = ratio
    for sid, ratio in drifting.items():
        key = f"meter:{sid}"
        if key in m.open:
            continue
        site = sid.rsplit("-", 1)[0]
        inc = m._new(key, "meter_drift", "warning", f"{sid} meter disagrees with its own power readings", site, [sid])
        inc.add("detected", f"The energy register moved {ratio:.2f}x what its power readings add up to: drivers "
                            f"may be billed wrongly.", short="meter drift")
        inc.add("response", "Billing hold on this charger's sessions until the meter is checked.", "auto-rule",
                short="billing hold")
        await m._ask(inc, "calibration", {"station": sid}, f"Book a meter calibration visit for {sid}?",
                     "Needed for billing accuracy (about €150); the charger keeps working meanwhile.", timeout_s=90)
    for key in [k for k in m.open if k.startswith("meter:")]:
        sid = key.split(":", 1)[1]
        watched = [w for w in m.meters.values() if w["station"] == sid and now - w["t0"] >= DRIFT_MIN_S]
        if watched and sid not in drifting and _calm_for(m.open[key], True, now):
            m._close(key, "Energy and power readings agree again; billing hold lifted.", short="meter ok")


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
            inc = m._new(key, "ignores_limit", "critical", f"{sid} ignores its power limit", st["site"], [sid])
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
