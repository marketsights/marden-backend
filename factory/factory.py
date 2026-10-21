#!/usr/bin/env python3
"""Marden agent factory: a dependency-aware, self-checking orchestrator for many
concurrent coding agents working on one repository.

Each task runs in its own git worktree and branch. A task is merged into main
only after its gates pass (scope guard, tests, contracts, visual diff, data
checks) and it rebases cleanly. Failures retry with the failure log; repeated
failures escalate to a fixer task. Large tasks can be split by the agent.
A watchdog keeps main green and stops the line when it is not.

Commands:
  python3 factory.py preflight            check tools, credentials and the repo
  python3 factory.py init  tasks.json     load a task graph into state
  python3 factory.py run                  run until done, budget spent or stopped
  python3 factory.py status               one-screen summary
  python3 factory.py report               write reports/morning_report.md
  python3 factory.py add  task.json       add tasks while running
  python3 factory.py retry <id>|failed    re-queue failed tasks
  python3 factory.py stop                 finish running tasks, start no new ones
"""
from __future__ import annotations

import fnmatch, json, os, re, shlex, shutil, signal, sqlite3, subprocess, sys, threading, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    import yaml  # type: ignore
except ImportError:  # factory.yaml can also be JSON
    yaml = None


# ---------------------------------------------------------------- config
def load_config() -> dict:
    p = Path(os.environ.get("FACTORY_CONFIG", HERE / "factory.yaml"))
    text = p.read_text()
    cfg = yaml.safe_load(text) if yaml else json.loads(text)
    cfg["_dir"] = str(p.parent.resolve())
    base = Path(cfg["_dir"])
    for k in ("repo", "state_db", "worktrees", "logs", "reports", "roles_dir"):
        cfg[k] = str((base / cfg[k]).resolve()) if not os.path.isabs(cfg[k]) else cfg[k]
    return cfg


_GLOB_CACHE: dict = {}


def gmatch(path: str, pattern: str) -> bool:
    """Path glob: ** spans folders (including none), * and ? stay within one folder."""
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        out, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out += "(?:.*/)?"; i += 3
            elif pattern.startswith("**", i):
                out += ".*"; i += 2
            elif pattern[i] == "*":
                out += "[^/]*"; i += 1
            elif pattern[i] == "?":
                out += "[^/]"; i += 1
            else:
                out += re.escape(pattern[i]); i += 1
        rx = _GLOB_CACHE[pattern] = re.compile(out + r"\Z")
    return rx.match(path) is not None


# ---------------------------------------------------------------- structure rules (built in; agents cannot weaken them)
_SECRET_RX = re.compile(r"(sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|postgres(?:ql)?://[^:@\s/]+:[^@\s]{6,}@|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[bap]-[A-Za-z0-9-]{10,})")


def structure_violations(cfg: dict, wt: Path, changed: list[str]) -> list[str]:
    rules = cfg.get("structure") or {}
    if not rules:
        return []
    out = []
    skip = rules.get("skip", [])
    caps = rules.get("max_lines", {})
    test_cap = rules.get("max_lines_test", 600)
    max_bytes = int(rules.get("max_file_kb", 512)) * 1024
    banned = re.compile(rules.get("banned_names", r"$^"), re.I)
    for f in changed:
        p = wt / f
        if not p.is_file():
            continue  # deleted
        if any(gmatch(f, g) for g in rules.get("never_commit", [])):
            out.append(f"{f}: must never be committed")
            continue
        if any(gmatch(f, g) for g in skip):
            continue
        name, ext = p.name, p.suffix.lower()
        size = p.stat().st_size
        if size > max_bytes:
            out.append(f"{f}: {size // 1024} KB is over the {max_bytes // 1024} KB file limit (data belongs in R2 or the database)")
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if _SECRET_RX.search(text):
            out.append(f"{f}: looks like it contains a secret; read it from an environment variable")
        stem = name.split(".")[0]
        if banned.search(stem):
            out.append(f"{f}: vague or throwaway file name; name the file after what it does")
        is_test = bool(re.search(r"(\.test\.|\.spec\.|^test_|_test\.py$)", name)) or "/tests/" in "/" + f
        if ext in caps:
            n = text.count("\n") + 1
            cap = test_cap if is_test else int(caps[ext])
            if n > cap:
                out.append(f"{f}: {n} lines is over the {cap}-line limit for {ext} files; split it by responsibility")
        if ext == ".py":
            parts = f.split("/")
            if not re.fullmatch(r"[a-z_][a-z0-9_]*\.py", name):
                out.append(f"{f}: Python files must be snake_case")
            bad = [d for d in parts[:-1] if parts[0] == "services" and not re.fullmatch(r"[a-z_][a-z0-9_]*", d)]
            if bad:
                out.append(f"{f}: Python package folders must be snake_case so they import ({bad[0]})")
        if ext in (".ts", ".tsx") and not name.endswith(".d.ts"):
            if not re.fullmatch(r"[a-z0-9]+(?:[-.][a-z0-9]+)*\.(ts|tsx)", name):
                out.append(f"{f}: TypeScript files must be kebab-case (e.g. site-header.tsx)")
            dirs = [d for d in f.split("/")[:-1] if not re.fullmatch(r"(\[\[?\.{0,3}[a-zA-Z0-9_-]+\]?\]|\([a-z0-9-]+\)|@?[a-z0-9]+(?:-[a-z0-9]+)*|_[a-z0-9-]+)", d)]
            if dirs:
                out.append(f"{f}: folder '{dirs[0]}' must be kebab-case (Next.js [param] and (group) folders are fine)")
    return out


def ref(tid: str) -> str:
    """Safe git branch / folder name for a task id."""
    r = re.sub(r"[^A-Za-z0-9._-]+", "-", tid).strip(".-")
    return re.sub(r"\.{2,}", ".", r) or "task"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- state
SCHEMA = """
create table if not exists tasks(
  id text primary key, title text, role text, phase int, area text, priority int,
  spec text, inputs text, outputs text, deps text, gates text, params text,
  max_attempts int, budget_usd real, timeout_min int, needs_human int default 0,
  status text default 'pending', attempts int default 0, last_error text default '',
  cost real default 0, superseded_by text default '[]', parent text default '',
  created_by text default 'plan', created_at text, updated_at text, started_at text, finished_at text);
create table if not exists events(ts text, task_id text, kind text, detail text);
create table if not exists meta(key text primary key, value text);
create index if not exists ix_status on tasks(status);
"""
DONE = {"merged"}
FOLDED = {"split", "escalated"}           # done when everything in superseded_by is done
TERMINAL_BAD = {"failed", "human"}


class State:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("pragma journal_mode=wal")

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)

    def ev(self, tid, kind, detail=""):
        self.x("insert into events values(?,?,?,?)", (now(), tid, kind, str(detail)[:4000]))

    def meta(self, k, v=None):
        if v is None:
            r = self.q("select value from meta where key=?", (k,))
            return r[0]["value"] if r else None
        self.x("insert or replace into meta values(?,?)", (k, str(v)))

    def add(self, t: dict, created_by="plan"):
        cfg_defaults = {"phase": 1, "area": "", "priority": 50, "spec": [], "inputs": [], "outputs": [],
                        "deps": [], "gates": [], "params": {}, "max_attempts": 3, "budget_usd": 8.0,
                        "timeout_min": 45, "needs_human": False, "parent": ""}
        t = {**cfg_defaults, **t}
        self.x("""insert or ignore into tasks(id,title,role,phase,area,priority,spec,inputs,outputs,deps,gates,params,
                  max_attempts,budget_usd,timeout_min,needs_human,parent,created_by,created_at,updated_at,status)
                  values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (t["id"], t["title"], t["role"], t["phase"], t["area"], t["priority"], json.dumps(t["spec"]),
                json.dumps(t["inputs"]), json.dumps(t["outputs"]), json.dumps(t["deps"]), json.dumps(t["gates"]),
                json.dumps(t["params"]), t["max_attempts"], t["budget_usd"], t["timeout_min"], int(bool(t["needs_human"])),
                t["parent"], created_by, now(), now(), "human" if t["needs_human"] else "pending"))

    def get(self, tid):
        r = self.q("select * from tasks where id=?", (tid,))
        return dict(r[0]) if r else None

    def set(self, tid, **kw):
        kw["updated_at"] = now()
        cols = ",".join(f"{k}=?" for k in kw)
        self.x(f"update tasks set {cols} where id=?", (*kw.values(), tid))

    def all(self):
        return [dict(r) for r in self.q("select * from tasks")]


def satisfied(st: State, tid: str, cache: dict) -> bool:
    if tid in cache:
        return cache[tid]
    t = st.get(tid)
    if t is None:
        cache[tid] = False
    elif t["status"] in DONE:
        cache[tid] = True
    elif t["status"] in FOLDED:
        kids = json.loads(t["superseded_by"])
        cache[tid] = bool(kids) and all(satisfied(st, k, cache) for k in kids)
    else:
        cache[tid] = False
    return cache[tid]


# ---------------------------------------------------------------- shell helpers
def sh(cmd, cwd=None, timeout=None, env=None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, **(env or {})})
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT after {timeout}s\n{(e.stdout or '')[-2000:] if isinstance(e.stdout, str) else ''}"


def tail(s: str, n=6000) -> str:
    return s if len(s) <= n else "...[truncated]...\n" + s[-n:]


# ---------------------------------------------------------------- factory
@dataclass
class Running:
    tid: str
    role: str
    thread: threading.Thread
    started: float


class Factory:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.st = State(cfg["state_db"])
        self.repo = Path(cfg["repo"])
        self.git_lock = threading.Lock()
        self.running: dict[str, Running] = {}
        self.merge_q: list[str] = []
        self.merge_lock = threading.Lock()
        self.stop_flag = False
        self.pending_new: dict[str, list] = {}
        self.merging: str | None = None
        for d in (cfg["worktrees"], cfg["logs"], cfg["reports"]):
            Path(d).mkdir(parents=True, exist_ok=True)

    # ---------------- git
    def git(self, *args, cwd=None):
        with self.git_lock:
            return sh(["git", *args], cwd=cwd or self.repo)

    def main_branch(self):
        return self.cfg.get("main_branch", "main")

    def make_worktree(self, tid) -> Path:
        wt = Path(self.cfg["worktrees"]) / ref(tid)
        if wt.exists():
            self.git("worktree", "remove", "--force", str(wt))
            shutil.rmtree(wt, ignore_errors=True)
        rc, out = self.git("worktree", "add", "-B", f"t/{ref(tid)}", str(wt), self.main_branch())
        if rc:
            raise RuntimeError(out)
        return wt

    def drop_worktree(self, tid):
        wt = Path(self.cfg["worktrees"]) / ref(tid)
        self.git("worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
        self.git("branch", "-D", f"t/{ref(tid)}")

    # ---------------- scheduling
    def ready(self) -> list[dict]:
        cache: dict = {}
        out = []
        for t in self.st.q("select * from tasks where status='pending' order by phase, priority, id"):
            t = dict(t)
            if t["id"] in self.running:
                continue
            if all(satisfied(self.st, d, cache) for d in json.loads(t["deps"])):
                out.append(t)
        return out

    def role_count(self, role):
        return sum(1 for r in list(self.running.values()) if r.role == role)

    def spent(self) -> float:
        return float(self.st.q("select coalesce(sum(cost),0) s from tasks")[0]["s"])

    def tick(self):
        if self.stop_flag or self.st.meta("stop") == "1":
            return
        if self.spent() >= float(self.cfg["budget_usd"]):
            if self.st.meta("budget_hit") != "1":
                self.st.meta("budget_hit", "1"); self.st.ev("", "budget", f"spent {self.spent():.2f}")
            return
        line_stopped = self.st.meta("line_stopped") == "1"
        cap = int(self.cfg["max_concurrency"])
        limits = self.cfg.get("role_limits", {})
        for t in self.ready():
            if len(self.running) >= cap:
                break
            if line_stopped and t["role"] != "fixer":
                continue
            if self.role_count(t["role"]) >= int(limits.get(t["role"], cap)):
                continue
            self.launch(t)

    def launch(self, t):
        self.st.set(t["id"], status="running", attempts=t["attempts"] + 1, started_at=now())
        self.st.ev(t["id"], "start", f"attempt {t['attempts'] + 1}")
        th = threading.Thread(target=self.work, args=(t["id"],), daemon=True)
        self.running[t["id"]] = Running(t["id"], t["role"], th, time.time())
        th.start()

    # ---------------- one task attempt
    def work(self, tid):
        try:
            self._work(tid)
        except Exception as e:  # never let a thread kill the factory
            self.fail(tid, f"factory error: {e!r}")
        finally:
            self.running.pop(tid, None)

    def prompt_for(self, t: dict, wt: Path) -> str:
        roles = Path(self.cfg["roles_dir"])
        parts = [(roles / "_common.md").read_text()]
        rf = roles / f"{t['role']}.md"
        if rf.exists():
            parts.append(rf.read_text())
        task_view = {k: (json.loads(t[k]) if k in ("spec", "inputs", "outputs", "deps", "gates", "params") else t[k])
                     for k in ("id", "title", "role", "area", "spec", "inputs", "outputs", "gates", "params")}
        parts.append("## Your task\n```json\n" + json.dumps(task_view, indent=2) + "\n```")
        gate_lines = [f"- {g}: `{self.gate_cmd(g, t) or 'built-in'}`" for g in json.loads(t["gates"])]
        parts.append("## Gates your branch must pass (run them yourself before finishing)\n" + "\n".join(gate_lines))
        if t["last_error"]:
            parts.append("## Previous attempt failed. Fix this first:\n```\n" + tail(t["last_error"], 5000) + "\n```")
        return "\n\n".join(parts)

    def _work(self, tid):
        t = self.st.get(tid)
        wt = self.make_worktree(tid)
        fx = wt / ".factory"
        fx.mkdir(exist_ok=True)
        (fx / "task.json").write_text(json.dumps({k: t[k] for k in t}, indent=2, default=str))
        prompt = self.prompt_for(t, wt)
        (fx / "prompt.md").write_text(prompt)
        logdir = Path(self.cfg["logs"]) / ref(tid)
        logdir.mkdir(parents=True, exist_ok=True)
        model = self.cfg.get("models", {}).get(t["role"], self.cfg.get("default_model", ""))
        cmd = self.cfg["worker_cmd"].format(model=model, budget=t["budget_usd"], prompt_file=shlex.quote(str(fx / "prompt.md")),
                                            worktree=shlex.quote(str(wt)), task_id=tid)
        rc, out = sh(cmd, cwd=wt, timeout=int(t["timeout_min"]) * 60, env={"FACTORY_TASK": tid})
        (logdir / f"attempt-{t['attempts']}.log").write_text(out)
        cost = self.parse_cost(out)
        self.st.set(tid, cost=(t["cost"] or 0) + cost)
        if rc != 0:
            return self.retry_or_escalate(tid, f"worker exited {rc}\n{tail(out)}")
        result = {}
        if (fx / "result.json").exists():
            try:
                result = json.loads((fx / "result.json").read_text())
            except Exception as e:
                return self.retry_or_escalate(tid, f"result.json unreadable: {e}")
        kind = result.get("status", "done")
        if kind == "blocked":
            self.st.set(tid, status="human", last_error=result.get("notes", "blocked"), finished_at=now())
            self.st.ev(tid, "human", result.get("notes", ""))
            return self.drop_worktree(tid)
        if kind == "split":
            return self.split(tid, result.get("new_tasks", []))
        # commit anything the agent left uncommitted (never .factory)
        sh("git add -A -- . ':!.factory' && git -c user.name=factory -c user.email=factory@local commit -qm "
           + shlex.quote(f"{tid}: {t['title']}") + " || true", cwd=wt)
        ok, why = self.run_gates(t, wt)
        if not ok:
            return self.retry_or_escalate(tid, why)
        self.st.set(tid, status="queued_merge")
        self.st.ev(tid, "gates_pass")
        self.pending_new[tid] = result.get("new_tasks", [])
        with self.merge_lock:
            self.merge_q.append(tid)

    @staticmethod
    def parse_cost(out: str) -> float:
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    j = json.loads(line)
                    return float(j.get("total_cost_usd") or j.get("cost_usd") or 0)
                except Exception:
                    continue
        try:
            j = json.loads(out)
            return float(j.get("total_cost_usd") or 0)
        except Exception:
            return 0.0

    # ---------------- gates
    def gate_cmd(self, g: str, t: dict) -> str | None:
        name, _, arg = g.partition(":")
        tmpl = self.cfg.get("gates", {}).get(name)
        if not tmpl:
            return None
        params = json.loads(t["params"]) if isinstance(t["params"], str) else t["params"]
        return tmpl.format(arg=arg, area=t["area"], id=t["id"], **{k: v for k, v in params.items() if isinstance(v, (str, int, float))})

    def changed_files(self, wt: Path) -> list[str]:
        rc, out = sh(["git", "diff", "--name-only", f"{self.main_branch()}...HEAD"], cwd=wt)
        return [l for l in out.splitlines() if l.strip()]

    def run_gates(self, t: dict, wt: Path) -> tuple[bool, str]:
        changed = self.changed_files(wt)
        if not changed and t["role"] not in self.cfg.get("noop_roles", []):
            return False, "no changes committed on the branch; the task produced nothing"
        # scope guard: only touch declared outputs (+ tests); protected paths only for owner roles
        outs = json.loads(t["outputs"]) + self.cfg.get("always_allowed", [])
        bad = [f for f in changed if not any(gmatch(f, g) for g in outs)]
        if bad and t["role"] not in self.cfg.get("unscoped_roles", []):
            return False, "scope guard: files outside this task's outputs were changed:\n" + "\n".join(bad[:50])
        for prot in self.cfg.get("protected", []):
            hit = [f for f in changed if gmatch(f, prot["glob"])]
            if hit and t["role"] not in prot["roles"]:
                return False, f"protected path {prot['glob']} may only be changed by {prot['roles']}: {hit[:10]}"
        sv = structure_violations(self.cfg, wt, changed)
        if sv:
            return False, "structure rules (see docs/conventions.md):\n" + "\n".join(sv[:40])
        for g in json.loads(t["gates"]):
            name, _, arg = g.partition(":")
            if name == "files":
                missing = [p for p in arg.split(",") if not list(wt.glob(p))]
                if missing:
                    return False, f"gate files: missing {missing}"
                continue
            cmd = self.gate_cmd(g, t)
            if cmd is None:
                return False, f"gate '{g}' is not defined in factory.yaml"
            rc, out = sh(cmd, cwd=wt, timeout=int(self.cfg.get("gate_timeout_min", 20)) * 60)
            self.st.ev(t["id"], f"gate:{g}", f"rc={rc}")
            if rc:
                return False, f"gate {g} failed (`{cmd}`):\n{tail(out)}"
        return True, ""

    # ---------------- outcomes
    def retry_or_escalate(self, tid, why):
        t = self.st.get(tid)
        self.st.ev(tid, "fail", why[:500])
        if t["attempts"] < t["max_attempts"]:
            self.st.set(tid, status="pending", last_error=why)
            self.drop_worktree(tid)
            return
        if t["role"] == "fixer" or self.cfg.get("escalate") is False:
            return self.fail(tid, why)
        fid = f"{tid}~fix"
        self.st.add({"id": fid, "title": f"Fix: {t['title']}", "role": "fixer", "phase": t["phase"], "area": t["area"],
                     "priority": max(0, t["priority"] - 10), "spec": json.loads(t["spec"]), "inputs": json.loads(t["inputs"]),
                     "outputs": json.loads(t["outputs"]), "deps": json.loads(t["deps"]), "gates": json.loads(t["gates"]),
                     "params": {**json.loads(t["params"]), "original_task": tid}, "max_attempts": 2,
                     "budget_usd": t["budget_usd"] * 1.5, "timeout_min": t["timeout_min"]}, created_by=tid)
        self.st.set(fid, last_error=why)
        self.st.set(tid, status="escalated", superseded_by=json.dumps([fid]), last_error=why, finished_at=now())
        self.st.ev(tid, "escalated", fid)
        self.drop_worktree(tid)

    def fail(self, tid, why):
        self.st.set(tid, status="failed", last_error=why, finished_at=now())
        self.st.ev(tid, "failed", why[:500])
        try:
            self.drop_worktree(tid)
        except Exception:
            pass

    def depth(self, t: dict) -> int:
        d = 0
        while t and t.get("parent"):
            d += 1
            t = self.st.get(t["parent"])
        return d

    def split(self, tid, new_tasks):
        t = self.st.get(tid)
        if self.depth(t) >= int(self.cfg.get("max_split_depth", 2)):
            return self.retry_or_escalate(tid, "split refused: this task is already a sub-task at the maximum depth; do the work instead of splitting")
        limit = int(self.cfg.get("max_split", 25))
        if not new_tasks or len(new_tasks) > limit:
            return self.retry_or_escalate(tid, f"split returned {len(new_tasks)} tasks (allowed 1..{limit})")
        ids = self.add_children(t, new_tasks, inherit_deps=True)
        self.st.set(tid, status="split", superseded_by=json.dumps(ids), finished_at=now())
        self.st.ev(tid, "split", ",".join(ids))
        self.drop_worktree(tid)

    def add_children(self, parent: dict, new_tasks: list, inherit_deps: bool) -> list[str]:
        ids, total = [], len(self.st.all())
        if total + len(new_tasks) > int(self.cfg.get("max_total_tasks", 5000)):
            self.st.ev(parent["id"], "discovery_capped", len(new_tasks))
            return []
        local = {}
        for i, nt in enumerate(new_tasks, 1):
            cid = f"{parent['id']}.{i}"
            local[nt.get("id", str(i))] = cid
        for i, nt in enumerate(new_tasks, 1):
            cid = f"{parent['id']}.{i}"
            deps = [local.get(d, d) for d in nt.get("deps", [])]
            if inherit_deps:
                deps = list(dict.fromkeys(json.loads(parent["deps"]) + deps))
            else:
                deps = list(dict.fromkeys([parent["id"]] + deps))
            outs = nt.get("outputs") or json.loads(parent["outputs"])
            self.st.add({"id": cid, "title": nt.get("title", f"part {i} of {parent['title']}"),
                         "role": nt.get("role", parent["role"]), "phase": parent["phase"], "area": parent["area"],
                         "priority": parent["priority"], "spec": nt.get("spec", json.loads(parent["spec"])),
                         "inputs": nt.get("inputs", json.loads(parent["inputs"])), "outputs": outs, "deps": deps,
                         "gates": nt.get("gates", json.loads(parent["gates"])), "params": json.loads(parent["params"]),
                         "max_attempts": parent["max_attempts"], "budget_usd": parent["budget_usd"],
                         "timeout_min": parent["timeout_min"], "parent": parent["id"]}, created_by=parent["id"])
            ids.append(cid)
        return ids

    # ---------------- merge queue (one at a time keeps main linear and green)
    def busy(self) -> bool:
        inflight = self.st.q("select count(*) n from tasks where status in ('running','queued_merge')")[0]["n"]
        return bool(self.running or self.merge_q or self.merging or inflight)

    def merge_loop(self):
        while not (self.stop_flag and not self.merge_q and not self.running):
            tid = None
            with self.merge_lock:
                if self.merge_q:
                    tid = self.merge_q.pop(0)
            if not tid:
                time.sleep(1); continue
            self.merging = tid
            try:
                self.merge_one(tid)
            except Exception as e:
                self.retry_or_escalate(tid, f"merge error: {e!r}")
            finally:
                self.merging = None

    def merge_one(self, tid):
        t = self.st.get(tid)
        wt = Path(self.cfg["worktrees"]) / ref(tid)
        rc, out = sh(["git", "rebase", self.main_branch()], cwd=wt)
        if rc:
            sh(["git", "rebase", "--abort"], cwd=wt)
            return self.retry_or_escalate(tid, f"rebase onto main failed (another task changed the same lines):\n{tail(out)}")
        for g in self.cfg.get("merge_gates", []):
            cmd = g.format(area=t["area"], id=tid)
            rc, out = sh(cmd, cwd=wt, timeout=int(self.cfg.get("gate_timeout_min", 20)) * 60)
            if rc:
                return self.retry_or_escalate(tid, f"merge gate failed after rebase (`{cmd}`):\n{tail(out)}")
        rc, out = self.git("merge", "--ff-only", f"t/{ref(tid)}")
        if rc:
            return self.retry_or_escalate(tid, f"fast-forward failed:\n{tail(out)}")
        self.st.set(tid, status="merged", finished_at=now(), last_error="")
        self.st.ev(tid, "merged")
        self.drop_worktree(tid)
        self.after_merge(self.st.get(tid))
        if tid.startswith("fix-main-"):
            self.check_main()

    def after_merge(self, t):
        new = self.pending_new.pop(t["id"], [])
        cap = int(self.cfg.get("max_discovered_per_task", 10))
        if new and self.depth(t) >= int(self.cfg.get("max_discovery_depth", 3)):
            self.st.ev(t["id"], "discovery_refused", f"{len(new)} tasks at depth {self.depth(t)}: {[n.get('title') for n in new][:5]}")
            new = []
        if new:
            self.add_children(t, new[:cap], inherit_deps=False)
        # follow-on verification work, created as code lands
        fo = self.cfg.get("follow_on", {})
        n = int(self.st.meta(f"merges:{t['area']}") or 0) + 1
        self.st.meta(f"merges:{t['area']}", n)
        for role, every in fo.items():
            if t["role"] in ("tester", "red_team", "fixer") or not t["area"]:
                continue
            if n % int(every) == 0:
                fid = f"{role}:{t['area']}:{n}"
                self.st.add({"id": fid, "title": f"{role.replace('_', ' ')} pass on {t['area']} after {n} merges",
                             "role": role, "phase": t["phase"], "area": t["area"], "priority": 30,
                             "spec": json.loads(t["spec"]), "outputs": self.cfg.get("follow_on_outputs", {}).get(role, ["tests/**", "**/*.test.*", "**/test_*.py", "reports/findings/**"]),
                             "deps": [t["id"]], "gates": self.cfg.get("follow_on_gates", {}).get(role, []),
                             "params": {"recent": t["id"]}, "budget_usd": 6}, created_by="follow_on")

    # ---------------- watchdog: keep main green
    def watchdog(self):
        every = int(self.cfg.get("watchdog_min", 15)) * 60
        last = time.time()
        while not self.stop_flag:
            time.sleep(2)
            if time.time() - last >= every:
                last = time.time()
                self.check_main()

    def check_main(self) -> bool:
        """Run the full check on main. Red main stops the line and creates one fixer at a time."""
        cmd = self.cfg.get("main_check")
        if not cmd:
            return True
        chk = Path(self.cfg["worktrees"]) / "_main_check"
        with self.git_lock:
            if not chk.exists():
                sh(["git", "worktree", "add", "--detach", str(chk), self.main_branch()], cwd=self.repo)
            sh(["git", "checkout", "-q", "--detach", self.main_branch()], cwd=chk)
        rc, out = sh(cmd, cwd=chk, timeout=int(self.cfg.get("gate_timeout_min", 20)) * 60)
        self.st.ev("", "main_check", f"rc={rc}")
        if rc == 0:
            if self.st.meta("line_stopped") == "1":
                self.st.ev("", "line_resumed")
            self.st.meta("line_stopped", "0")
            self.st.meta("main_fix_tries", 0)
            return True
        self.st.meta("line_stopped", "1")
        active = self.st.q("select id from tasks where role='fixer' and id like 'fix-main-%' and status in ('pending','running','queued_merge')")
        tries = int(self.st.meta("main_fix_tries") or 0)
        if not active and tries >= int(self.cfg.get("max_main_fixes", 3)):
            self.st.meta("stop", "1")
            self.st.ev("", "halted", f"main still red after {tries} fixer attempts; stopping so nothing builds on a broken main")
            return False
        if not active:
            self.st.meta("main_fix_tries", tries + 1)
            fid = f"fix-main-{int(time.time() * 1000)}"
            self.st.add({"id": fid, "title": "Main is red: make the full check pass", "role": "fixer", "phase": 0,
                         "area": "main", "priority": 0, "outputs": ["**"], "gates": [], "budget_usd": 15,
                         "params": {"check": cmd}}, created_by="watchdog")
            self.st.set(fid, last_error=tail(out))
            self.st.ev(fid, "line_stopped", "created fixer")
        return False

    # ---------------- main loop
    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop_flag", True))
        self.st.meta("stop", "0")
        self.check_main()
        threading.Thread(target=self.merge_loop, daemon=True).start()
        threading.Thread(target=self.watchdog, daemon=True).start()
        last_status = 0.0
        while True:
            self.tick()
            if time.time() - last_status > 30:
                write_status_html(self); last_status = time.time()
            active = self.st.q("select count(*) n from tasks where status in ('pending','running','queued_merge')")[0]["n"]
            if self.st.meta("stop") == "1" or self.st.meta("budget_hit") == "1":
                if not self.busy():
                    break
            elif active == 0 and not self.busy():
                if self.check_main():
                    break
                continue
            elif not self.busy() and not self.ready():
                # nothing can move: everything left is waiting on failed or human tasks
                stuck = self.st.q("select count(*) n from tasks where status='pending'")[0]["n"]
                if stuck:
                    self.st.ev("", "stalled", f"{stuck} pending tasks wait on failed/human tasks")
                    break
            time.sleep(float(self.cfg.get("tick_sec", 3)))
        self.stop_flag = True
        for p in self.cfg.get("push_on_exit", []):
            sh(p, cwd=self.repo)
        write_status_html(self)
        write_report(self)


# ---------------------------------------------------------------- reporting
def summary(st: State) -> dict:
    rows = st.all()
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"total": len(rows), "by_status": by, "cost": round(sum(r["cost"] or 0 for r in rows), 2)}


def write_status_html(f: Factory):
    s = summary(f.st)
    rows = sorted(f.st.all(), key=lambda r: (r["phase"], r["status"] != "running", r["id"]))
    tr = "".join(f"<tr><td>{r['id']}</td><td>{r['role']}</td><td>{r['area']}</td><td class='{r['status']}'>{r['status']}</td>"
                 f"<td>{r['attempts']}</td><td>{(r['cost'] or 0):.2f}</td><td>{(r['last_error'] or '')[:140]}</td></tr>" for r in rows)
    html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30><title>Factory status</title>
<style>body{{font:14px system-ui;margin:24px}}td,th{{padding:4px 8px;border-bottom:1px solid #ddd;text-align:left}}
.merged{{color:#11603B}}.failed,.human{{color:#A8200F}}.running{{color:#3A22C9;font-weight:600}}</style>
<h1>Factory status</h1><p>{now()} · {s['total']} tasks · {s['by_status']} · ${s['cost']} spent · line {'STOPPED' if f.st.meta('line_stopped')=='1' else 'green'}</p>
<table><tr><th>Task</th><th>Role</th><th>Area</th><th>Status</th><th>Tries</th><th>$</th><th>Last error</th></tr>{tr}</table>"""
    (Path(f.cfg["reports"]) / "status.html").write_text(html)


def write_report(f: Factory):
    st, s = f.st, summary(f.st)
    rows = st.all()
    areas = {}
    for r in rows:
        a = areas.setdefault(r["area"] or "-", {"merged": 0, "total": 0, "open": []})
        a["total"] += 1
        if satisfied(st, r["id"], {}):
            a["merged"] += 1
        elif r["status"] not in FOLDED:
            a["open"].append(f"{r['id']} ({r['status']})")
    lines = [f"# Factory report · {now()}", "",
             f"{s['total']} tasks · ${s['cost']} spent · main is {'RED' if st.meta('line_stopped')=='1' else 'green'}", "",
             "| Status | Count |", "| --- | --- |", *[f"| {k} | {v} |" for k, v in sorted(s["by_status"].items())], "",
             "## By area", "", "| Area | Done | Total | Not done |", "| --- | --- | --- | --- |"]
    for a, v in sorted(areas.items()):
        lines.append(f"| {a} | {v['merged']} | {v['total']} | {', '.join(v['open'][:8])}{' …' if len(v['open'])>8 else ''} |")
    human = [r for r in rows if r["status"] == "human"]
    failed = [r for r in rows if r["status"] == "failed"]
    lines += ["", "## Needs you", ""]
    lines += [f"- **{r['id']}**: {r['title']} — {(r['last_error'] or 'credentials or a decision needed')[:300]}" for r in human] or ["- nothing"]
    lines += ["", "## Failed after retries and a fixer", ""]
    lines += [f"- **{r['id']}**: {r['title']}\n  ```\n  {(r['last_error'] or '')[-800:]}\n  ```" for r in failed] or ["- none"]
    acc = Path(f.cfg["repo"]) / "reports" / "acceptance.json"
    if acc.exists():
        lines += ["", "## Acceptance run on main", "", "```json", acc.read_text()[:6000], "```"]
    Path(f.cfg["reports"], "morning_report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))


# ---------------------------------------------------------------- preflight
def preflight(cfg) -> int:
    bad = 0
    def chk(ok, msg):
        nonlocal bad
        print(("ok   " if ok else "FAIL ") + msg)
        bad += 0 if ok else 1
    for tool in cfg.get("require_tools", []):
        chk(shutil.which(tool) is not None, f"tool on PATH: {tool}")
    for var in cfg.get("require_env", []):
        chk(bool(os.environ.get(var)), f"env var set: {var}")
    for var in cfg.get("optional_env", []):
        print(("ok   " if os.environ.get(var) else "warn ") + f"optional env var: {var} (stub mode if missing)")
    repo = Path(cfg["repo"])
    chk((repo / ".git").exists(), f"git repo at {repo}")
    rc, _ = sh(["git", "rev-parse", "--verify", cfg.get("main_branch", "main")], cwd=repo)
    chk(rc == 0, f"branch {cfg.get('main_branch','main')} exists")
    chk(Path(cfg["roles_dir"], "_common.md").exists(), "roles/_common.md present")
    rc, out = sh(cfg.get("worker_smoke", "true"), cwd=repo, timeout=180)
    chk(rc == 0, "worker smoke test: " + out.strip()[-200:])
    print(f"\n{'READY' if not bad else str(bad) + ' problem(s) — fix before running'}")
    return 1 if bad else 0


# ---------------------------------------------------------------- cli
def main():
    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "preflight":
        sys.exit(preflight(cfg))
    f = Factory(cfg)
    if cmd in ("init", "add"):
        tasks = json.loads(Path(sys.argv[2]).read_text())
        for t in tasks:
            f.st.add(t, created_by="plan" if cmd == "init" else "manual")
        print(f"{len(tasks)} tasks loaded; {summary(f.st)}")
    elif cmd == "run":
        f.run()
    elif cmd == "status":
        print(json.dumps(summary(f.st), indent=2))
        for r in f.st.q("select id, role, status, attempts from tasks where status in ('running','queued_merge','failed','human') order by status"):
            print(f"  {r['status']:13} {r['id']}  ({r['role']}, try {r['attempts']})")
    elif cmd == "report":
        write_report(f)
    elif cmd == "retry":
        target = sys.argv[2]
        ids = [r["id"] for r in f.st.q("select id from tasks where status='failed'")] if target == "failed" else [target]
        for i in ids:
            f.st.set(i, status="pending", attempts=0)
        print(f"re-queued {len(ids)}")
    elif cmd == "stop":
        f.st.meta("stop", "1"); print("stop requested: running tasks will finish, nothing new starts")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
