from __future__ import annotations

from .assurance import dependency_key
from .files import digest
from .models import CheckSpec, Verdict
from .sandbox import Scope, Seatbelt, runtime_roots


class Verifier:
    def __init__(self, store=None):
        self.store = store

    async def check(
        self, root, specs: list[CheckSpec], version: int, ports=None, reuse: bool = False
    ) -> Verdict:
        before = digest(root)
        runner = Seatbelt(Scope(root, writable=False, runtimes=runtime_roots(), ports=ports or []))
        run_id = self.store.find_workspace(root) if self.store else None
        state = self.store.get(run_id) if run_id else None
        if run_id:
            from .processes import track

            track(self.store, run_id, runner)
        results, pending_cache = [], {}
        try:
            for spec in specs:
                key = dependency_key(root, spec, state, ports)
                trusted_closed = state and any(p.closed and p.argv == spec.argv for p in state.check_policies)
                cached = state.check_cache.get(key) if trusted_closed and reuse else None
                if cached and cached.get("passed"):
                    result = {**cached, "name": spec.name, "reused": True}
                else:
                    raw = await runner.run(spec.argv, spec.timeout)
                    result = {
                        "name": spec.name,
                        "argv": spec.argv,
                        **raw,
                        "passed": raw["exit_code"] == 0 and not raw["timed_out"],
                        "dependency_key": key,
                        "reused": False,
                    }
                results.append(result)
                if result["passed"] and trusted_closed:
                    pending_cache[key] = result
                if run_id:
                    self.store.event(
                        run_id,
                        "check_reused" if result["reused"] else "check_executed",
                        {"argv": spec.argv, "key": key, "passed": result["passed"]},
                    )
        finally:
            await runner.stop()
        after = digest(root)
        passed = bool(specs) and before == after and all(r["passed"] for r in results)
        if state and reuse and before == after:
            current = self.store.get(run_id)
            if current.goal.version == version:
                current.check_cache.update(pending_cache)
                # Bounded per-attempt cache. Evidence keeps its own key after eviction.
                current.check_cache = dict(list(current.check_cache.items())[-128:])
                self.store.commit(current, "check_cache_saved", {"added": len(pending_cache)})
        return Verdict(passed=passed, goal_version=version, snapshot=after, checks=results)
