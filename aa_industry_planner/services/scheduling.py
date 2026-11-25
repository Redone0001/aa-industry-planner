"""A deterministic earliest-start forecast for one plan's remaining job batches."""

from collections import defaultdict, deque
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from heapq import heapify, heappop, heappush
from math import ceil, inf, isfinite

from django.utils import timezone

from aa_industry_planner.engine import material_quantity
from aa_industry_planner.models import JOB_ACTIVITIES


def release_after(job, now):
    if job.status in {"ready", "delivered"}:
        return 0  # The forecast explicitly assumes immediate delivery.
    if job.status == "paused" or job.end_date <= now:
        return inf
    return ceil((job.end_date - now).total_seconds())


def remaining_targets(steps, root_id):
    """Do not reinstall inputs already consumed by a linked downstream job.

    Original stock is consumed first. Completed upstream output already consumed
    downstream cannot also supply that downstream step's remaining runs.
    """
    by_type = {step.item_type_id: step for step in steps}
    if root_id is None:
        return {
            tid: (step.output_quantity, step.completed_quantity) for tid, step in by_type.items()
        }
    demand, consumed, targets = defaultdict(int), defaultdict(int), {}
    if root_id in by_type:
        demand[root_id] = by_type[root_id].output_quantity
    children = {
        tid: {int(t) for t in step.details.get("materials", {}) if int(t) in by_type}
        for tid, step in by_type.items()
    }
    parents = {tid: sum(tid in child_ids for child_ids in children.values()) for tid in by_type}
    queue = deque(tid for tid, count in parents.items() if not count)
    while queue:
        tid = queue.popleft()
        step = by_type[tid]
        needed = min(step.output_quantity, demand[tid])
        completed = max(0, step.completed_quantity - consumed[tid])
        targets[tid] = needed, completed
        missing = max(0, needed - completed - step.ongoing_quantity)
        recipe, context = step.details.get("recipe") or {}, step.details.get("context") or {}
        output = max(1, recipe.get("output", 1))
        runs = (missing + output - 1) // output
        cap = max(step.details.get("batches") or [max(1, step.runs)])
        batches = [cap] * (runs // cap) + ([runs % cap] if runs % cap else [])
        efficiency = Decimal(str(context.get("material_multiplier", "1")))
        if step.activity == "manufacturing":
            efficiency *= 1 - Decimal(str(context.get("me", 0))) / 100
        for child in children[tid]:
            original = step.details["materials"].get(str(child), 0)
            base = recipe.get("materials", {}).get(str(child))
            future = (
                sum(material_quantity(base, batch, efficiency) for batch in batches)
                if base is not None
                else ceil(Decimal(original) * runs / max(1, step.runs))
            )
            stock = sum(
                row.get("from_stock", 0)
                for row in by_type[child].details.get("inventory_requirements", [])
                if row.get("consumer_id") == tid
            )
            used = max(0, original - future)
            consumed[child] += max(0, used - stock)
            demand[child] += max(0, future - max(0, stock - used))
            parents[child] -= 1
            if not parents[child]:
                queue.append(child)
    return targets


def estimate_schedule(steps, preferences, running_jobs=(), now=None, root_id=None):
    now = now or timezone.now()
    slots = {
        activity: getattr(preferences, f"{activity}_slots")
        for activity in ["manufacturing", "reaction"]
    }
    free = {}
    for activity, count in slots.items():
        releases = sorted(
            release_after(job, now)
            for job in running_jobs
            if JOB_ACTIVITIES.get(job.activity_id) == activity
        )
        # If more jobs are already installed than configured slots, the first
        # completions reduce that excess before any configured slot becomes free.
        free[activity] = (
            (releases[-count:] + [0] * max(0, count - len(releases))) if count else [inf]
        )
        heapify(free[activity])
    by_type = {step.item_type_id: step for step in steps}
    targets = remaining_targets(steps, root_id)
    work, finished = {}, {}
    remaining_jobs = 0
    for step in steps:
        details = step.details
        recipe, context = details.get("recipe") or {}, details.get("context") or {}
        needed, completed = targets.get(
            step.item_type_id, (step.output_quantity, step.completed_quantity)
        )
        remaining = max(0, needed - completed)
        ongoing = min(remaining, step.ongoing_quantity)
        missing = remaining - ongoing
        output = max(1, recipe.get("output", 1))
        runs = (missing + output - 1) // output
        cap = max(details.get("batches") or [max(1, step.runs)])
        batches = deque([cap] * (runs // cap) + ([runs % cap] if runs % cap else []))
        per_run = Decimal(str(recipe.get("seconds", 0))) * Decimal(
            str(context.get("time_multiplier", "1"))
        )
        if step.activity == "manufacturing":
            per_run *= 1 - Decimal(str(context.get("te", 0))) / 100
        if per_run <= 0:
            per_run = Decimal(str(details.get("estimated_seconds", 0))) / max(1, step.runs)
        end, linked = 0, []
        allocations = (
            a for a in step.allocations.all() if a.quantity and a.job.status in {"active", "paused"}
        )
        for allocation in sorted(allocations, key=lambda a: release_after(a.job, now)):
            if ongoing <= 0:
                break
            end = max(end, release_after(allocation.job, now))
            ongoing -= allocation.quantity
            linked.append(allocation.job)
        if ongoing > 0:
            end = inf
        reason = (
            "A linked job is paused or awaits an ESI status update." if not isfinite(end) else ""
        )
        if batches and not slots[step.activity]:
            reason = f"Configure {step.get_activity_display().lower()} slots."
        if batches and per_run <= 0:
            reason = "Saved job duration is unavailable. Refresh this plan."
        step.timing = dict(
            start_at=None,
            finish_at=None,
            finish_seconds=None,
            running=bool(linked),
            remaining_jobs=len(batches),
            reason=reason,
            not_needed=needed == 0,
        )
        work[step.item_type_id] = dict(
            step=step,
            batches=batches,
            per_run=per_run,
            end=end,
            start=None,
            dependencies={int(t) for t in details.get("materials", {}) if int(t) in by_type},
        )
        remaining_jobs += len(batches)
        if not batches:
            finished[step.item_type_id] = end
    if remaining_jobs > 50000:
        for row in work.values():
            row["step"].timing["reason"] = (
                "More than 50,000 remaining jobs. Increase batch sizes to estimate timing."
            )
        return dict(duration=None, finish_at=None, slots=slots, too_large=True)
    while True:
        candidates = []
        for tid, row in work.items():
            if (
                not row["batches"]
                or row["per_run"] <= 0
                or not row["dependencies"].issubset(finished)
            ):
                continue
            ready = max((finished[dep] for dep in row["dependencies"]), default=0)
            start = max(ready, free[row["step"].activity][0])
            if isfinite(start):
                candidates.append((start, row["step"].position, tid))
        if not candidates:
            break
        start, _, tid = min(candidates)
        row = work[tid]
        size = row["batches"].popleft()
        seconds = max(1, int((row["per_run"] * size).to_integral_value(rounding=ROUND_CEILING)))
        end = start + seconds
        heappop(free[row["step"].activity])
        heappush(free[row["step"].activity], end)
        row["start"] = start if row["start"] is None else min(row["start"], start)
        row["end"] = max(row["end"], end)
        if not row["batches"]:
            finished[tid] = row["end"]
    for tid, row in work.items():
        timing = row["step"].timing
        end = finished.get(tid, inf)
        if row["start"] is not None:
            timing["start_at"] = now + timedelta(seconds=row["start"])
        if isfinite(end):
            timing.update(finish_at=now + timedelta(seconds=end), finish_seconds=end)
        elif not timing["reason"]:
            timing["reason"] = (
                "Waiting for an upstream step or a slot whose availability is unknown."
            )
    if root_id is not None:
        duration = finished.get(root_id, inf) if root_id in work else 0
    else:
        duration = max(finished.values(), default=0) if len(finished) == len(work) else inf
    return dict(
        duration=duration if isfinite(duration) else None,
        finish_at=now + timedelta(seconds=duration) if isfinite(duration) else None,
        slots=slots,
        too_large=False,
    )
