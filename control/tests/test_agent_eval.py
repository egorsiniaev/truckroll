import json

import httpx

from control.agent import TriageAgent, build_context
from control.eval import evaluate

INC = {"id": "INC-0001", "kind": "fault", "severity": "critical", "title": "S-1 reports HighTemperature",
       "site": "A2-HUB", "stations": ["A2-HUB-00001"], "age_s": 30, "opened": "2026-11-24T10:00:10Z", "status": "open",
       "timeline": [{"type": "detected", "actor": "system", "text": "2 connector(s) faulted", "short": "fault"}]}
SNAP = {"sites": [{"code": "A2-HUB", "kind": "hub", "power_w": 1, "cap_w": 2, "grid_w": 1, "rules": []}],
        "stations": [{"id": "A2-HUB-00001", "site": "A2-HUB", "connected": True, "state": "faulted", "power_w": 0,
                      "connectors": [{"status": "Faulted", "error_code": "HighTemperature", "power_w": 0}]}],
        "scenario": {"running": {"id": "overheat", "title": "Overheating charger"}}, "events": [],
        "backend": {"reply_latency_s": 0}}


def test_context_never_contains_the_scenario_or_ledger():
    text = json.dumps(build_context(INC, SNAP))
    assert "overheat" not in text and "Overheating charger" not in text and "root_cause" not in text
    assert "HighTemperature" in text


class Manager:
    def __init__(self):
        self.verdicts = []

    def snapshot(self):
        return [INC]

    async def agent_verdict(self, incident_id, verdict, action):
        self.verdicts.append((incident_id, verdict["category"], action))


def gemini(answer, status=200, usage=(1800, 120)):
    def handler(req):
        assert req.headers["x-goog-api-key"] == "k" and "key=" not in str(req.url)
        body = json.loads(req.content)
        assert body["generationConfig"]["maxOutputTokens"] <= 400
        if status != 200:
            return httpx.Response(status, json={})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}],
                                         "usageMetadata": {"promptTokenCount": usage[0], "candidatesTokenCount": usage[1]}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


ANSWER = {"category": "hardware", "summary": "Overheating.", "evidence": "HighTemperature", "next_step": "truck_roll",
          "all_clear": False, "confidence": 0.8}


async def test_one_call_per_incident_with_usage_and_a_log_row(tmp_path):
    agent = TriageAgent(gemini(ANSWER), "k", "m", tmp_path / "t.jsonl")
    m = Manager()
    await agent.tick(m, SNAP)
    await agent.tick(m, SNAP)  # already triaged: no second call
    assert m.verdicts == [("INC-0001", "hardware", "truck_roll")]
    assert agent.status()["calls_today"] == 1 and agent.status()["tokens_in"] == 1800
    assert json.loads((tmp_path / "t.jsonl").read_text())["category"] == "hardware"


async def test_rate_limit_pauses_and_caps_hold(tmp_path):
    agent = TriageAgent(gemini(ANSWER, status=429), "k", "m", None)
    await agent.tick(Manager(), SNAP)
    assert agent.status()["paused_s"] > 0 and "rate limited" in agent.status()["last_error"]
    capped = TriageAgent(gemini(ANSWER), "k", "m", None, daily_cap=0)
    m = Manager()
    await capped.tick(m, SNAP)
    assert m.verdicts == []


def test_eval_scores_against_the_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    triage = tmp_path / "triage.jsonl"
    rows = [
        {"run_id": "a", "scenario": "overheat", "root_cause": "hardware: x", "started": "2026-11-24T10:00:00Z",
         "ended": "2026-11-24T10:04:00Z", "outcome": "completed"},
        {"run_id": "b", "scenario": "site-drop", "root_cause": "network: y", "started": "2026-11-24T11:00:00Z",
         "ended": "2026-11-24T11:03:00Z", "outcome": "completed"},
        {"run_id": "c", "scenario": "cold-snap", "root_cause": "expected: z", "started": "2026-11-24T12:00:00Z",
         "ended": "2026-11-24T12:04:00Z", "outcome": "completed"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows))
    tri = [
        {"incident_id": "I1", "site": "A2-HUB", "opened": "2026-11-24T10:00:10Z", "category": "hardware",
         "all_clear": False, "seconds_after_open": 18, "summary": "s", "tokens_in": 1800, "tokens_out": 100, "triaged": "2026-11-24T09:00:00Z"},
        {"incident_id": "I2", "site": "AMS-DEPOT", "opened": "2026-11-24T11:00:20Z", "category": "hardware",
         "all_clear": True, "seconds_after_open": 22, "summary": "s", "tokens_in": 1600, "tokens_out": 90},
    ]
    triage.write_text("\n".join(json.dumps(t) for t in tri))
    res = evaluate(ledger, triage)
    assert res["runs"] == 3 and res["runs_triaged"] == 2
    assert res["accuracy"] == 0.5 and res["accuracy_first"] == 0.5 and res["false_all_clear"] == 1
    assert res["by_scenario"]["cold-snap"]["correct"] == 1  # no incident for a decoy is right
    assert res["median_seconds_to_summary"] == 20


def test_eval_ignores_incidents_from_before_the_run_and_counts_any_correct_triage(tmp_path):
    ledger, triage = tmp_path / "l.jsonl", tmp_path / "t.jsonl"
    ledger.write_text(json.dumps({"run_id": "a", "scenario": "auth-slow", "root_cause": "backend: x",
                                  "started": "2026-11-24T10:00:00Z", "ended": "2026-11-24T10:06:00Z", "outcome": "completed"}))
    triage.write_text("\n".join(json.dumps(t) for t in [
        {"incident_id": "old", "site": "A2-HUB", "opened": "2026-11-24T09:50:00Z", "category": "hardware", "all_clear": False,
         "seconds_after_open": 20, "summary": "s", "tokens_in": 1, "tokens_out": 1, "triaged": "2026-11-24T09:59:00Z"},
        {"incident_id": "noise", "site": "A2-HUB", "opened": "2026-11-24T10:01:00Z", "category": "hardware", "all_clear": False,
         "seconds_after_open": 20, "summary": "s", "tokens_in": 1, "tokens_out": 1},
        {"incident_id": "auth", "site": "ALL", "opened": "2026-11-24T10:02:00Z", "category": "backend", "all_clear": False,
         "seconds_after_open": 20, "summary": "s", "tokens_in": 1, "tokens_out": 1}]))
    res = evaluate(ledger, triage)
    assert res["accuracy"] == 1.0 and res["accuracy_first"] == 0.0
    assert res["runs_detail"][0]["agent_categories"] == ["hardware", "backend"]


def test_runs_before_the_agent_existed_are_not_scored(tmp_path):
    ledger, triage = tmp_path / "l.jsonl", tmp_path / "t.jsonl"
    ledger.write_text(json.dumps({"run_id": "a", "scenario": "overheat", "root_cause": "hardware: x",
                                  "started": "2026-11-24T08:00:00Z", "ended": "2026-11-24T08:04:00Z", "outcome": "completed"}))
    triage.write_text(json.dumps({"incident_id": "I", "site": "A2-HUB", "opened": "2026-11-24T09:00:00Z", "category": "hardware",
                                  "all_clear": False, "seconds_after_open": 20, "summary": "s", "tokens_in": 1,
                                  "tokens_out": 1, "triaged": "2026-11-24T09:00:20Z"}))
    res = evaluate(ledger, triage)
    assert res["runs"] == 0 and res["runs_before_agent"] == 1
