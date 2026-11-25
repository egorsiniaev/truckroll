"""Triage agent: one Gemini call per incident. It reads what an on-call engineer would see, never the incident
ledger or the running scenario (that is the eval's ground truth), and writes a verdict into the timeline.

Built to be cheap: one call per incident, a small prompt, a short JSON answer, a per-minute and per-day cap, and a
long pause on rate limits instead of retries. Its suggestions become decisions that default to "no".
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TZ = ZoneInfo("Europe/Amsterdam")
WAIT_BEFORE_TRIAGE_S = 15
RATE_LIMIT_PAUSE_S = 900
TRANSIENT_PAUSE_S = 60  # Gemini 500/503 ("high demand"): try the same incident again a minute later
MAX_OUTPUT_TOKENS = 400
CATEGORIES = ["network", "hardware", "firmware", "backend", "expected"]
NEXT_STEPS = {  # agent vocabulary -> incident action
    "wait": None, "none": None, "soft_reset": "reset", "truck_roll": "truck_roll", "firmware_rollback": "rollback",
    "free_vend": "free_vend", "take_out_of_service": "inoperative", "restart_backend_worker": "restart_worker",
    "meter_calibration": "calibration",
}

SYSTEM = (
    "You are the on-call triage engineer for a network of EV charging sites (OCPP 1.6). You get one incident with "
    "its timeline and the current state of the site, its chargers and the backend. Decide the root cause category: "
    "network (site uplink, latency, reconnects), hardware (charger components, meters, locks, overheating), "
    "firmware (charger software misbehaving: reboot loops, ignored commands, stuck states), backend (our CSMS or "
    "its services: auth, overload), or expected (not a fault: designed derates, grid operator requests). "
    "Say all_clear=true only if nothing needs doing. Pick one next_step. Be brief and concrete; quote the evidence "
    "you used. If evidence is thin, lower your confidence instead of guessing."
)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING", "enum": CATEGORIES},
        "summary": {"type": "STRING", "description": "two sentences at most: what is happening and why"},
        "evidence": {"type": "STRING", "description": "the specific signals you relied on"},
        "next_step": {"type": "STRING", "enum": list(NEXT_STEPS)},
        "all_clear": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["category", "summary", "evidence", "next_step", "all_clear", "confidence"],
}


def build_context(inc: dict, snap: dict) -> dict:
    """What a person on call would see. Deliberately excludes the scenario and the ledger."""
    site = next((s for s in snap.get("sites", []) if s["code"] == inc["site"]), None)
    stations = [s for s in snap.get("stations", []) if s["id"] in inc["stations"]]
    if not stations and site:
        stations = [s for s in snap.get("stations", []) if s["site"] == site["code"]][:6]
    events = [e["text"] for e in snap.get("events", [])
              if inc["site"] in (e.get("site"), "ALL") or e.get("station") in inc["stations"]][:8]
    ctx = {
        "incident": {
            "kind": inc["kind"], "severity": inc["severity"], "title": inc["title"], "site": inc["site"],
            "stations": inc["stations"], "age_s": inc["age_s"],
            "timeline": [{"type": t["type"], "actor": t["actor"], "text": t["text"]} for t in inc["timeline"]],
        },
        "backend": snap.get("backend", {}),
        "recent_events": events,
        "price": {k: (snap.get("prices") or {}).get(k) for k in ("now_eur_mwh", "expensive")},
        "stations": [{
            "id": s["id"], "connected": s.get("connected"), "state": s.get("state"),
            "heartbeat_age_s": s.get("heartbeat_age_s"), "power_kw": round((s.get("power_w") or 0) / 1000, 1),
            "limit_kw": None if s.get("limit_w") is None else round(s["limit_w"] / 1000, 1),
            "limited_by": s.get("limit_sources"),
            "connectors": [{"status": c["status"], "error": c["error_code"], "in_status_s": c.get("status_age_s"),
                            "power_kw": round((c.get("power_w") or 0) / 1000, 1)} for c in s.get("connectors", [])],
        } for s in stations],
    }
    if site:
        ctx["site"] = {
            "code": site["code"], "kind": site.get("kind"), "power_kw": round(site["power_w"] / 1000),
            "grid_kw": round((site.get("grid_w") or 0) / 1000), "cap_kw": round(site["cap_w"] / 1000),
            "free": site.get("free"), "connectors": site.get("connectors_total"), "states": site.get("states"),
            "network": site.get("network"), "rules_active": [r["text"] for r in site.get("rules", []) if r["active"]],
            "weather": {k: (site.get("weather") or {}).get(k) for k in ("temp_c", "gust_kmh")},
            "battery": {k: (site.get("battery") or {}).get(k) for k in ("mode", "soc")},
        }
    return ctx


class TriageAgent:
    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str, log_path: Path | None,
                 daily_cap: int = 150, per_minute: int = 6, price_in: float = 0.264, price_out: float = 2.20,
                 free_tier: bool = True, clock=time.monotonic) -> None:
        self.http, self._key, self.model = http, api_key, model
        self.log_path = log_path
        self.daily_cap, self.per_minute = daily_cap, per_minute
        self.price_in, self.price_out, self.free_tier = price_in, price_out, free_tier
        self.clock = clock
        self.enabled = True
        self.done: set[str] = set()
        self.calls_recent: deque[float] = deque()
        self.paused_until = 0.0
        self.last_error: str | None = None
        self.day, self.calls_today, self.tokens_in, self.tokens_out = "", 0, 0, 0
        self._load_done()

    def _load_done(self) -> None:
        if self.log_path and self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                try:
                    self.done.add(json.loads(line)["incident_id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    def status(self) -> dict:
        cost = (self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1_000_000
        return {"enabled": self.enabled, "model": self.model, "calls_today": self.calls_today,
                "daily_cap": self.daily_cap, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cost_usd_if_paid": round(cost, 4), "free_tier": self.free_tier,
                "paused_s": max(0, round(self.paused_until - self.clock())), "last_error": self.last_error}

    def _budget_ok(self) -> bool:
        now = self.clock()
        day = datetime.now(UTC).astimezone(TZ).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.calls_today, self.tokens_in, self.tokens_out = day, 0, 0, 0
        while self.calls_recent and now - self.calls_recent[0] > 60:
            self.calls_recent.popleft()
        return (self.enabled and now >= self.paused_until and self.calls_today < self.daily_cap
                and len(self.calls_recent) < self.per_minute)

    async def tick(self, manager, snap: dict) -> None:
        """Triage at most one new incident per call. The manager records the verdict and proposes the next step."""
        todo = [i for i in manager.snapshot() if i["status"] != "recovered" and i["id"] not in self.done
                and i["age_s"] >= WAIT_BEFORE_TRIAGE_S]
        if not todo or not self._budget_ok():
            return
        inc = max(todo, key=lambda i: i["age_s"])  # oldest first
        self.done.add(inc["id"])
        verdict = await self._ask_model(build_context(inc, snap))
        if verdict is None:
            if self.last_error and self.last_error.startswith("Gemini busy"):
                self.done.discard(inc["id"])  # temporary: try this incident again
            return
        await manager.agent_verdict(inc["id"], verdict, NEXT_STEPS.get(verdict["next_step"]))
        self._record(inc, verdict)

    async def _ask_model(self, context: dict) -> dict | None:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(context, separators=(",", ":"))}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS,
                                 "responseMimeType": "application/json", "responseSchema": SCHEMA,
                                 # Thinking tokens bill as output: keep them minimal. Gemini 3 takes a level, older a budget.
                                 "thinkingConfig": ({"thinkingLevel": "minimal"} if self.model.startswith("gemini-3")
                                                    else {"thinkingBudget": 0})},
        }
        self.calls_recent.append(self.clock())
        self.calls_today += 1
        t0 = time.monotonic()
        try:
            res = await self.http.post(URL.format(model=self.model), json=body, timeout=30,
                                       headers={"x-goog-api-key": self._key})
        except httpx.HTTPError as exc:
            self.last_error = f"request failed: {exc.__class__.__name__}"
            return None
        if res.status_code == 429:
            self.paused_until = self.clock() + RATE_LIMIT_PAUSE_S
            self.last_error = "rate limited by Gemini: paused 15 min"
            log.warning(self.last_error)
            return None
        if res.status_code in (500, 503):
            self.paused_until = self.clock() + TRANSIENT_PAUSE_S
            self.last_error = f"Gemini busy (HTTP {res.status_code}): retrying in 1 min"
            return None
        if res.status_code >= 400:
            self.last_error = f"Gemini HTTP {res.status_code}"
            log.warning("%s: %s", self.last_error, res.text[:200])
            return None
        data = res.json()
        usage = data.get("usageMetadata", {})
        self.tokens_in += usage.get("promptTokenCount", 0)
        self.tokens_out += usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            verdict = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError):
            self.last_error = "unreadable answer from Gemini"
            return None
        if verdict.get("category") not in CATEGORIES or verdict.get("next_step") not in NEXT_STEPS:
            self.last_error = "answer outside the allowed values"
            return None
        self.last_error = None
        verdict["_latency_s"] = round(time.monotonic() - t0, 2)
        verdict["_tokens"] = [usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)]
        return verdict

    def _record(self, inc: dict, verdict: dict) -> None:
        if not self.log_path:
            return
        tokens_in, tokens_out = verdict["_tokens"]
        row = {
            "incident_id": inc["id"], "kind": inc["kind"], "site": inc["site"], "stations": inc["stations"],
            "opened": inc["opened"], "triaged": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "seconds_after_open": inc["age_s"] + verdict["_latency_s"], "category": verdict["category"],
            "all_clear": verdict["all_clear"], "next_step": verdict["next_step"], "confidence": verdict["confidence"],
            "summary": verdict["summary"], "model": self.model, "tokens_in": tokens_in, "tokens_out": tokens_out,
        }
        try:
            with self.log_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as exc:
            log.warning("triage log write failed: %s", exc)


SUMMARY_SYSTEM = (
    "You write a shift handover for the operator of an EV charging network who was away. Use only the facts "
    "given. Three short bullets: what mattered most, what is still open and needs them, anything unusual. No "
    "greeting, no filler."
)
SUMMARY_SCHEMA = {"type": "OBJECT", "properties": {"bullets": {"type": "ARRAY", "items": {"type": "STRING"}}},
                  "required": ["bullets"]}
SUMMARY_CACHE_S = 600


async def summarize(agent: "TriageAgent", facts: dict) -> list[str] | None:
    """One cheap call, cached for 10 minutes, counted against the agent's caps."""
    now = agent.clock()
    cached = getattr(agent, "_summary_cache", None)
    if cached and now - cached[0] < SUMMARY_CACHE_S:
        return cached[1]
    if not agent._budget_ok():
        return None
    body = {
        "systemInstruction": {"parts": [{"text": SUMMARY_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(facts, separators=(",", ":"))}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 250, "responseMimeType": "application/json",
                             "responseSchema": SUMMARY_SCHEMA,
                             "thinkingConfig": ({"thinkingLevel": "minimal"} if agent.model.startswith("gemini-3")
                                                else {"thinkingBudget": 0})},
    }
    agent.calls_recent.append(now)
    agent.calls_today += 1
    try:
        res = await agent.http.post(URL.format(model=agent.model), json=body, timeout=30,
                                    headers={"x-goog-api-key": agent._key})
    except httpx.HTTPError:
        return None
    if res.status_code == 429:
        agent.paused_until = now + RATE_LIMIT_PAUSE_S
        return None
    if res.status_code >= 400:
        return None
    data = res.json()
    usage = data.get("usageMetadata", {})
    agent.tokens_in += usage.get("promptTokenCount", 0)
    agent.tokens_out += usage.get("candidatesTokenCount", 0)
    try:
        bullets = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])["bullets"][:3]
    except (KeyError, IndexError, json.JSONDecodeError):
        return None
    agent._summary_cache = (now, bullets)
    return bullets
