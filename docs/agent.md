# Triage agent

The triage agent lives in the control service: [`control/src/control/agent.py`](../control/src/control/agent.py),
scored by [`control/src/control/eval.py`](../control/src/control/eval.py).

- **What it sees:** the incident and its timeline, the site, the affected chargers, backend latency stats, recent
  site events and the power price. Never the incident ledger or the running scenario: that is the eval's ground
  truth.
- **What it does:** one Gemini call per incident (15 s after it opens), answering in a fixed JSON schema: root-cause
  category (network, hardware, firmware, backend, expected), a two-sentence summary, the evidence it used, one next
  step, "all clear" yes/no, confidence. The verdict goes into the incident timeline as "AI agent". Firmware rollout
  incidents (failed install, failing release) are skipped: their cause is our own update.
- **Human gate:** its suggested next step becomes a question on the map with a 90 s deadline. What happens when
  nobody answers depends on the agent's confidence: at 70 % or more the default is **yes** and the step runs; below
  70 % the default is **no**. The question says which, up front. Anyone visiting the live demo can answer.
- **Why it defaults to yes in the simulation:** few questions get a human answer (12 % on 26 Nov), and a demo
  where nothing ever happens teaches nothing. The actions are limited to a fixed list and only touch simulated
  chargers. **A real operator should not do this:** agent proposals should default to no, only signed-in on-call
  staff should be able to answer, and anything that costs money or interrupts a driver (truck roll, free vend,
  firmware rollback, taking a charger out of service) should always need an explicit yes.
- **Cost controls:** model `gemini-3.5-flash-lite` with minimal thinking, max 400 output tokens, 6 calls/minute,
  150/day (`AGENT_DAILY_CAP`), a 15 min pause on rate limits, no retries. Measured: about 570 tokens in and 120
  out per triage. Key from `GEMINI_API_KEY` in the gitignored `.env`.
- **A/B mode:** every other incident gets raw signals only (no rules' title, type or timeline), to measure what
  the agent adds beyond the rules.
- **Queries when unsure:** below 60 % confidence it may request up to 2 read-only Prometheus/LogQL queries
  (allow-listed, last 30 min, results cut to 1,500 characters), then answers again. At most 3 calls per incident.
- **Also writes:** the "while you were away" handover (one call per 10 min at most) and, on demand, post-mortems
  and NIS2/CRA drafts on the reports page. Rules decide which regulation applies; a person approves; nothing is
  sent.
- **Eval** (`GET /api/eval`, and on the page under service levels): per scenario run since the agent went live,
  was the root cause category right (any triage in the run, and first triage only), any false "all clear" about a
  real fault, median time from incident open to summary, tokens per triage.
