import asyncio
import copy
import json
from pathlib import Path
HERE = Path(__file__).resolve().parent / "fixtures"
BASE = Path(__file__).resolve().parents[1]
def read(path):
    return json.loads(path.read_text(encoding="utf-8"))
class FixtureMCP:
    """Deterministic implementation of the original MCP boundary, faults included."""
    def __init__(self, case):
        self.case = case
        self.fixture = read(HERE / "monitoring_fixture.json")
        for name, values in case["overrides"].items():
            self.fixture[name].update(values)
        self.calls = []
        self.counts = {}

    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, name, arguments, server="ops"):
        # Exact original prompt function body, without importing FastMCP.
        import ast
        source = ast.parse((BASE / "mcp_server.py").read_text(encoding="utf-8"))
        fn = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == name)
        fn.decorator_list = []
        scope = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "<original-prompt>", "exec"), scope)
        return scope[name](**arguments)

    async def find_resource_template(self, name):
        return "ops", "runbook://incident-response/{incident_type}"

    async def call_tool(self, name, arguments, server="ops"):
        target = arguments["system_name"]
        self.counts[target] = self.counts.get(target, 0) + 1
        fault = self.case["failure"].get(target, self.case["failure"].get("*"))
        record = {"kind": "monitoring", "target": target, "attempt": self.counts[target]}
        self.calls.append(record)
        if fault == "timeout" or (fault == "transient_once" and self.counts[target] == 1):
            record["status"] = "timeout"
            raise asyncio.TimeoutError("evaluation injected timeout")
        if fault == "permanent":
            record["status"] = "failed"
            raise ValueError("evaluation injected permanent failure")
        record["status"] = "success"
        result = copy.deepcopy(self.fixture.get(target, {
            "system_name": target, "status": "unknown", "cpu_percent": 0.0,
            "memory_percent": 0.0, "last_checked": "2026-10-20T09:00:00+09:00",
            "alerts": [f"System '{target}' not found in monitoring"]}))
        record["data"] = result
        return result

    async def read_resource(self, uri, server="ops"):
        target = uri.rsplit("/", 1)[-1]
        record = {"kind": "runbook", "target": target, "status": "success"}
        self.calls.append(record)
        if self.case["failure"].get("runbook"):
            record["status"] = "failed"
            raise ValueError("evaluation injected runbook failure")
        path = BASE / "data/runbooks" / f"{target}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else f"# Runbook Not Found\n\nNo runbook for incident type: {target}"
        record["data"] = text
        return text
