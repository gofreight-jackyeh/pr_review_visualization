from __future__ import annotations

import json

from pr_explorer.infrastructure.process.runner import RunResult


class RecordingRunner:
    """Fake CommandRunner: records argv and answers from a handler; never spawns a process."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def run(self, args, cwd=None, input=None, timeout=None, env=None):
        self.calls.append({"args": list(args), "cwd": cwd, "input": input, "env": env})
        out = self.handler(list(args), input)
        return out if isinstance(out, RunResult) else RunResult(0, out or "", "")


def claude_envelope(payload: dict) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(payload)})


def prompt_data(prompt: str) -> dict:
    from pr_explorer.infrastructure.semantic.prompt_builder import FENCE_CLOSE, FENCE_OPEN

    return json.loads(prompt[prompt.index(FENCE_OPEN) + len(FENCE_OPEN): prompt.index(FENCE_CLOSE)])


def call_kind(args: list[str]) -> str:
    from pr_explorer.infrastructure.semantic.prompt_builder import system_prompt

    sp = args[args.index("--system-prompt") + 1]
    return next(k for k in ("system_map", "domains", "phases", "pr", "ask") if sp == system_prompt(k))


def ordered(calls: list[str]) -> list[str]:
    """Call kinds in order, with each run of the parallel domain / phase calls sorted (their order is not fixed)."""
    out: list[str] = []
    run: list[str] = []
    for c in calls + [""]:
        if c in ("domains", "phases"):
            run.append(c)
            continue
        out += sorted(run)
        run = []
        if c:
            out.append(c)
    return out


def cut(step_ids: list[str], n: int = 3) -> list[list[str]]:
    """Contiguous, covering split of step ids into n parts (n ≤ len)."""
    k, m = divmod(len(step_ids), n)
    out, i = [], 0
    for j in range(n):
        size = k + (1 if j < m else 0)
        out.append(step_ids[i: i + size])
        i += size
    return out


class SmartClaude:
    """Answers both call kinds from the prompt data: grounded statements plus one invented claim per call."""

    def __init__(self):
        self.calls: list[str] = []
        self.map_digests: list[dict] = []  # the data of every system-map call (which capabilities were asked)
        self.domain_digests: list[dict] = []
        self.phase_digests: list[dict] = []

    def __call__(self, args, prompt):
        kind = call_kind(args)
        self.calls.append(kind)
        data = prompt_data(prompt)
        if kind == "system_map":
            self.map_digests.append(data)
            caps = [{"id": c["id"], "business_name": f"Biz {c['technical_name']}", "description": f"Handles {c['technical_name']}."}
                    for c in data["capabilities"]]
            flows = [{"id": f["id"], "business_name": f"Flow {f['technical_name']}"} for f in data["flows"]]
            steps = [{"id": s["id"], "business_label": f"Do {s['kind']}"} for f in data["flows"] for s in f["steps"]]
            summary = {"text": "An online shop backend.", "cites": [d["evidence_id"] for d in data["docs"][:1]]}
            return claude_envelope({"system_summary": summary, "capabilities": caps + [{"id": "cap:ghost", "business_name": "Ghost"}],
                                    "entities": [{"id": e["id"], "business_name": f"Data {e['technical_name']}"} for e in data["entities"]],
                                    "flows": flows, "steps": steps, "graph": {"nodes": [{"id": "evil"}]}})
        if kind == "domains":  # two domains: orders-related vs everything else
            self.domain_digests.append(data)
            caps = [c["id"] for c in data["business_capabilities"]]
            a = [c for c in caps if c.startswith("cap:orders")]
            b = [c for c in caps if c not in a]
            return claude_envelope({"domains": [
                {"name": "Selling", "description": "Taking and managing orders.", "capability_ids": a},
                {"name": "Supply and money", "description": "Products, stock and payments.", "capability_ids": b,
                 "cites": ["ev:doc:base:README.md"]}]})
        if kind == "phases":
            self.phase_digests.append(data)
            return claude_envelope({"flows": [
                {"flow_id": f["id"], "phases": [{"label": f"Phase {i + 1}", "step_ids": part}
                                                for i, part in enumerate(cut([s["id"] for s in f["steps"]]))]}
                for f in data["flows"]]})
        if not data["flow_changes"]:  # a PR that changes no flow
            return claude_envelope({"purpose": {"text": "A small change.", "cites": ["pr"]}})
        fc = data["flow_changes"][0]
        changed = [s for s in fc["steps"] if s["step_change_status"] != "unchanged"]
        file_ = next((f for f in data["changed_files"] if f["path"] == "orders/services.py"), None) or \
            next(f for f in data["changed_files"] if f["path"].endswith("services.py"))
        return claude_envelope({
            "purpose": {"text": "Let customers apply discount codes at checkout.", "cites": ["pr", file_["id"]]},
            "behavior_changes": [
                {"flow_ids": [fc["flow_id"]], "before": "Members got a fixed discount.", "after": "A discount code reduces the total.",
                 "cites": [changed[0]["id"]]},
                {"flow_ids": ["flow:invented"], "after": "Invented."},
            ],
            "findings": [{"title": "Can a fixed discount exceed the total?", "severity": "warning", "status": "verified",
                          "cites": [file_["evidence_id"]]}],
            "step_labels": [{"step_id": s["id"], "business_label": f"Changed: {s['kind']}"} for s in changed],
            "head_additions": [{"id": x["id"], "business_name": f"New {x['technical_name']}", "description": "Added by this PR."}
                               for x in data.get("head_additions", [])],
            "walkthrough": {"behavior": {"text": "Checkout now accepts discount codes.", "cites": [fc["flow_id"]]}},
            "questions": [{"question": "Who can use a code?", "answer": "Any customer at checkout.", "cites": ["pr"]}],
            "flow_phases": [{"flow_id": x["flow_id"], "phases": [{"label": f"Part {i + 1}", "step_ids": part}
                                                                 for i, part in enumerate(cut([s["id"] for s in x["steps"]]))]}
                            for x in data["flow_changes"] if len(x["steps"]) >= 4],
            "architecture_hypotheses": [{"title": "Should discounts live in their own capability?", "check": "placement",
                                         "capability_ids": ["cap:orders"], "cites": [file_["evidence_id"]]}]
            + [{"title": "Does the mobile app still read the order total?", "check": "interface_change",
                "interface_id": x["interface_id"], "capability_ids": x["capability_ids"], "cites": x["evidence_ids"]}
               for x in data.get("interface_changes", [])[:1] if x["ring"] == "outer"],
            "interface_notes": [{"check_id": x["check_id"], "who_is_affected": f"Clients of {x['name']} notice it.",
                                 "cites": x["evidence_ids"][:1]} for x in data.get("interface_changes", []) if x["ring"] == "outer"],
        })
