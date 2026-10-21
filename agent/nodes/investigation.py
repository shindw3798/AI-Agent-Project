import asyncio
import time
import httpx
from ..schemas import Observation, TaskResult, RetrievedEvidence


async def run_task(system, kind, call, semaphore, timeout=15):
    started = time.perf_counter()
    error = None
    retries = 0
    async with semaphore:
        await asyncio.sleep(.35)
        for attempt in range(3):
            try:
                data = await asyncio.wait_for(call(), timeout)
                if kind == "monitoring":
                    if data["system_name"] != system or data["status"] == "unknown":
                        raise ValueError("INVALID_MONITORING_TARGET")
                    obs = Observation(
                        observation_id=f"obs:{system}", system_name=system, status=data["status"],
                        cpu_percent=data["cpu_percent"], memory_percent=data["memory_percent"],
                        alerts=data["alerts"], observed_at=data["last_checked"])
                    data = obs.model_dump()
                    status = "success"
                else:
                    if data.get("status") not in ("success", "no_match"):
                        raise ValueError(data.get("error_code") or "SEARCH_FAILED")
                    data["evidence"] = [RetrievedEvidence.model_validate(e).model_dump() for e in data["evidence"]]
                    if any(e["system_name"] != system for e in data["evidence"]):
                        raise ValueError("INVALID_SEARCH_TARGET")
                    status = data["status"]
                return TaskResult(task_id=f"{kind}:{system}", system_name=system, kind=kind,
                                  status=status, data=data, retry_count=retries,
                                  elapsed_ms=1000*(time.perf_counter()-started)).model_dump()
            except (TimeoutError, ConnectionError, httpx.TransportError):
                error = "TIMEOUT_OR_CONNECTION"
                if attempt < 2:
                    retries += 1
                    await asyncio.sleep([.3, .9][attempt])
            except Exception:
                error = "MONITORING_FAILED" if kind == "monitoring" else "SEARCH_FAILED"
                break
    return TaskResult(task_id=f"{kind}:{system}", system_name=system, kind=kind, status="failed",
                      error_code=error, retry_count=retries,
                      elapsed_ms=1000*(time.perf_counter()-started)).model_dump()
