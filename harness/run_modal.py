"""Parallel runner on Modal.
  modal run harness/run_modal.py --mode sandbox-check
  modal run harness/run_modal.py --mode pilot
  modal run harness/run_modal.py --mode full --seeds 5
Each episode runs in one Modal function; every agent gets its own offline Modal Sandbox.
"""
import json
import time
from pathlib import Path

import modal

app = modal.App("collusion-harness")
func_image = modal.Image.debian_slim(python_version="3.12").pip_install("requests").add_local_python_source("harness")
sandbox_image = modal.Image.debian_slim(python_version="3.12").run_commands(
    "useradd -m -s /bin/bash agent", "mkdir -p /home/agent/work && chown -R agent /home/agent")
SECRETS = [modal.Secret.from_name("openrouter")]
AGENT_MODEL = "anthropic/claude-haiku-5.5"
OVERSEERS = {"weak": "openai/gpt-4o-mini", "strong": "anthropic/claude-sonnet-5.5"}


@app.function(image=func_image, secrets=SECRETS, timeout=1800)
def episode(cfg: dict) -> dict:
    from harness.core import Cost, ModalSandbox, overseer, run_episode
    from harness.tasks import BY_NAME

    t0 = time.time()
    task = BY_NAME[cfg["task"]](seed=cfg["seed"], channel=cfg["channel"], pressure=cfg.get("pressure", False), level=cfg.get("level", 0))
    sb_app = modal.App.lookup("collusion-sandboxes", create_if_missing=True)
    sandboxes = [ModalSandbox(sb_app, sandbox_image) for _ in range(task.n_total)]
    cost = Cost()
    try:
        ep, score, _ = run_episode(task, mode="llm", agent_model=cfg.get("agent", AGENT_MODEL), cost=cost, sandboxes=sandboxes)
    finally:
        for s in sandboxes: s.close()
    ov = {k: overseer(m, task, ep, cost) for k, m in OVERSEERS.items()}
    return dict(cfg=cfg, score=score, overseer=ov, cost_usd=cost.usd, calls=cost.calls, secs=round(time.time() - t0, 1),
                log=ep.log, private_log=[(r, i, c, o[:200]) for r, i, c, o in ep.private_log])


@app.function(image=func_image, secrets=SECRETS, timeout=300)
def sandbox_check() -> dict:
    """Verify sandbox isolation properties before any agent runs."""
    from harness.core import ModalSandbox
    sb_app = modal.App.lookup("collusion-sandboxes", create_if_missing=True)
    sb = ModalSandbox(sb_app, sandbox_image)
    try:
        return dict(
            uid=sb.exec("id -u"),
            whoami=sb.exec("whoami"),
            network=sb.exec("python3 -c \"import urllib.request;urllib.request.urlopen('https://example.com',timeout=5);print('REACHED')\" 2>&1 | tail -1"),
            dns=sb.exec("python3 -c \"import socket;print(socket.gethostbyname('openrouter.ai'))\" 2>&1 | tail -1"),
            env_secrets=sb.exec("env | grep -i -E 'key|token|secret|openrouter' || echo none"),
            home_writable=sb.exec("touch /home/agent/work/x && echo ok"),
        )
    finally:
        sb.close()


@app.local_entrypoint()
def main(mode: str = "pilot", seeds: int = 5, budget: float = 18.0, tasks: str = "", agent: str = AGENT_MODEL, tag: str = ""):
    from harness.tasks import ALL_TASKS
    names = [t.name for t in ALL_TASKS if not tasks or t.name in tasks.split(",")]
    if mode == "sandbox-check":
        print(json.dumps(sandbox_check.remote(), indent=2)); return
    if mode == "level3":
        cfgs = [dict(task=n, seed=s, channel=c, pressure=False, level=3, agent=agent) for s in range(seeds) for n in names for c in (True, False)]
    elif mode == "pilot":
        cfgs = [dict(task=n, seed=0, channel=True, agent=agent) for n in names]
    else:
        cfgs = [dict(task=n, seed=s, channel=c, pressure=p, agent=agent) for s in range(seeds) for n in names for c in (True, False) for p in (False, True)]
    out_dir = Path("results/harness"); out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{mode}-{agent.split('/')[-1]}{'-' + tag if tag else ''}.jsonl"
    spent, done = 0.0, 0
    B = 14  # batch size: check the budget between batches
    with out.open("w") as f:  # overwrite: one file per (mode, agent model)
        for k in range(0, len(cfgs), B):
            if spent >= budget:
                print(f"STOP: budget ${budget} reached after {done} episodes (spent ${spent:.2f})"); break
            for r in episode.map(cfgs[k:k + B], return_exceptions=True, wrap_returned_exceptions=False):
                if isinstance(r, Exception):
                    print("episode failed:", type(r).__name__, str(r)[:200]); continue
                spent += r["cost_usd"]; done += 1
                f.write(json.dumps(r, default=str) + "\n"); f.flush()
            print(f"{done}/{len(cfgs)} episodes, spent ${spent:.3f}")
    print(f"done: {done} episodes, ${spent:.3f} -> {out}")
