#!/usr/bin/env python3
"""Dry-run worker: behaves like an agent without calling a model. Used to test the factory."""
import json, os, random, re, subprocess, sys, time
from pathlib import Path
t = json.loads(Path(".factory/task.json").read_text())
p = json.loads(t["params"]) if isinstance(t["params"], str) else t["params"]
stub = p.get("stub", {})
time.sleep(random.uniform(0.05, 0.4))
res = {"status": "done", "notes": "stub"}
if t["role"] != "fixer":
    if stub.get("fail_first") and t["attempts"] == 1:
        print(json.dumps({"total_cost_usd": 0.01})); sys.exit(3)
    if stub.get("always_fail"):
        Path("BROKEN").write_text("x")  # out of scope -> scope guard fails
if stub.get("split") and t["role"] != "fixer":
    res = {"status": "split", "new_tasks": stub["split"]}
else:
    outs = json.loads(t["outputs"]) if isinstance(t["outputs"], str) else t["outputs"]
    target = stub.get("file") or re.sub(r"\*+", t["id"].replace(":", "_"), outs[0]).replace("/" + t["id"] + "/", "/")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(f"{t['id']} attempt {t['attempts']} {time.time()}\n")
    if stub.get("outside") and t["role"] != "fixer":
        Path("elsewhere.txt").write_text("oops")
    if stub.get("new"):
        res["new_tasks"] = stub["new"]
    subprocess.run("git add -A -- . ':!.factory' && git -c user.name=stub -c user.email=s@x commit -qm " + json.dumps(t["id"]), shell=True)
Path(".factory/result.json").write_text(json.dumps(res))
print(json.dumps({"type": "result", "total_cost_usd": 0.02}))
