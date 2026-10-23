"""Run one episode locally with an LLM agent (no Modal): python -m harness.run_local bid_rigging 0 on"""
import json, sys
from harness.core import Cost, overseer, run_episode
from harness.tasks import BY_NAME

task_name, seed, chan = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "on"
import os
AGENT, OVERSEER = os.environ.get("AGENT", "anthropic/claude-haiku-5.5"), "openai/gpt-4o-mini"
cost = Cost()
task = BY_NAME[task_name](seed=seed, channel=chan)
ep, score, _ = run_episode(task, mode="llm", agent_model=AGENT, cost=cost)
ov = overseer(OVERSEER, task, ep, cost)
print(json.dumps({"task": task_name, "seed": seed, "channel": chan, "score": score, "overseer": ov,
                  "cost_usd": round(cost.usd, 4), "calls": cost.calls}, indent=2, default=str))
print("\n".join(ep.log[:40]))
