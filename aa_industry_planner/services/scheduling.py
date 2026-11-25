"""A deterministic earliest-start forecast for one plan's remaining job batches."""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from heapq import heapify, heappop, heappush
from math import ceil, inf, isfinite

from django.utils import timezone

from aa_industry_planner.engine import material_quantity
from aa_industry_planner.models import JOB_ACTIVITIES, PlanStep

from .capacity import ACTIVITIES


def release_after(job, now):
    if job.status in {"ready", "delivered"}:
        return 0  # The forecast explicitly assumes immediate delivery.
    if job.status == "paused" or job.end_date <= now:
        return inf
    return ceil((job.end_date - now).total_seconds())


@dataclass
class StepWork:
    step: PlanStep
    needed: int
    completed: int
    ongoing: int
    output_per_run: int
    seconds_per_run: Decimal
    pending_runs: int
    batches: dict[int, int]
    batch_inputs: dict[int, dict[int, int]]
    legacy_inputs: dict[int, int]
    stock: dict[int, int] = field(default_factory=dict)
    start: int | None = None
    end: int | float = 0

    def scaled_input(self, quantity, runs):
        total_runs = max(1, self.step.runs)
        return (quantity * runs + total_runs - 1) // total_runs

    def next_batch(self, available):
        for size in self.batches:
            inputs = self.batch_inputs[size]
            if self.legacy_inputs:
                # Older snapshots may only have total material requirements.
                # Apportion each total across batches without rounding it up repeatedly.
                inputs = inputs | {
                    tid: self.scaled_input(quantity, self.pending_runs)
                    - self.scaled_input(quantity, self.pending_runs - size)
                    for tid, quantity in self.legacy_inputs.items()
                }
            if all(
                quantity <= self.stock[tid] + available[tid] for tid, quantity in inputs.items()
            ):
                return size, inputs
        return None


def prepare_step(step, needed, consumed, materials):
    recipe = step.details.get("recipe") or {}
    context = step.details.get("context") or {}
    completed = max(0, step.completed_quantity - consumed)
    remaining = max(0, needed - completed)
    ongoing = min(remaining, step.ongoing_quantity)
    output = max(1, recipe.get("output", 1))
    runs = (remaining - ongoing + output - 1) // output
    cap = max(step.details.get("batches") or [max(1, step.runs)])
    full, tail = divmod(runs, cap)
    batches = {}
    if full:
        batches[cap] = full
    if tail:
        batches[tail] = 1
    efficiency = Decimal(str(context.get("material_multiplier", "1")))
    per_run = Decimal(str(recipe.get("seconds", 0))) * Decimal(
        str(context.get("time_multiplier", "1"))
    )
    if step.activity == "manufacturing":
        efficiency *= 1 - Decimal(str(context.get("me", 0))) / 100
        per_run *= 1 - Decimal(str(context.get("te", 0))) / 100
    if per_run <= 0:
        per_run = Decimal(str(step.details.get("estimated_seconds", 0))) / max(1, step.runs)
    base = {int(tid): amount for tid, amount in (recipe.get("materials") or {}).items()}
    return StepWork(
        step=step,
        needed=needed,
        completed=completed,
        ongoing=ongoing,
        output_per_run=output,
        seconds_per_run=per_run,
        pending_runs=runs,
        batches=batches,
        batch_inputs={
            size: {
                tid: material_quantity(base[tid], size, efficiency)
                for tid in materials.keys() & base.keys()
            }
            for size in batches
        },
        legacy_inputs={tid: amount for tid, amount in materials.items() if tid not in base},
    )


def remaining_work(steps, root_id):
    """Calculate future demand starting at the final product and moving toward its inputs."""
    by_type = {step.item_type_id: step for step in steps}
    materials = {}
    consumers = defaultdict(list)
    stock = defaultdict(int)
    for tid, step in by_type.items():
        materials[tid] = {
            int(child): amount
            for child, amount in step.details.get("materials", {}).items()
            if int(child) in by_type
        }
        for child in materials[tid]:
            consumers[child].append(tid)
        for row in step.details.get("inventory_requirements", []):
            stock[tid, row.get("consumer_id")] += row.get("from_stock", 0)

    parents = {tid: len(consumers[tid]) for tid in by_type}
    queue = deque(tid for tid, count in parents.items() if not count)
    demand, consumed = defaultdict(int), defaultdict(int)
    if root_id in by_type:
        demand[root_id] = by_type[root_id].output_quantity
    work = {}
    while queue:
        tid = queue.popleft()
        step = by_type[tid]
        needed = step.output_quantity if root_id is None else min(step.output_quantity, demand[tid])
        row = work[tid] = prepare_step(step, needed, consumed[tid], materials[tid])
        for child, original in materials[tid].items():
            if child in row.legacy_inputs:
                future = row.scaled_input(original, row.pending_runs)
            else:
                future = sum(
                    row.batch_inputs[size][child] * count for size, count in row.batches.items()
                )
            # Installed downstream jobs use their original stock allocation first.
            # Neither that stock nor upstream output already used is available again.
            used = max(0, original - future)
            row.stock[child] = max(0, stock[child, tid] - used)
            consumed[child] += max(0, used - stock[child, tid])
            demand[child] += max(0, future - row.stock[child])
            parents[child] -= 1
            if not parents[child]:
                queue.append(child)
    return work, consumers


def schedule_batches(work, consumers, free_slots, available, arrivals):
    ready = {activity: [] for activity in ACTIVITIES}
    queued = set()

    def enqueue(tid):
        row = work[tid]
        if tid not in queued and row.seconds_per_run > 0 and row.next_batch(available):
            heappush(ready[row.step.activity], (row.step.position, tid))
            queued.add(tid)

    for tid in work:
        enqueue(tid)
    elapsed = 0
    while True:
        updated = set()
        while arrivals and arrivals[0][0] <= elapsed:
            _, tid, quantity = heappop(arrivals)
            available[tid] += quantity
            updated.update(consumers[tid])
        for tid in updated:
            enqueue(tid)

        # Both activity pools can start jobs at the same timestamp. Within that
        # timestamp, use the saved production order to settle shared-input ties.
        active = [
            activity
            for activity in ACTIVITIES
            if ready[activity] and free_slots[activity][0] <= elapsed
        ]
        if not active:
            next_slot = min((free_slots[a][0] for a in ACTIVITIES if ready[a]), default=inf)
            elapsed = min(next_slot, arrivals[0][0] if arrivals else inf)
            if not isfinite(elapsed):
                break
            continue
        activity = min(active, key=lambda a: ready[a][0])
        _, tid = heappop(ready[activity])
        queued.remove(tid)
        row = work[tid]
        batch = row.next_batch(available)
        if batch is None:
            # Another job starting at this time used the shared inputs first.
            continue
        size, inputs = batch
        for child, quantity in inputs.items():
            from_stock = min(row.stock[child], quantity)
            row.stock[child] -= from_stock
            available[child] -= quantity - from_stock
        row.pending_runs -= size
        row.batches[size] -= 1
        if not row.batches[size]:
            del row.batches[size]
        seconds = max(
            1, int((row.seconds_per_run * size).to_integral_value(rounding=ROUND_CEILING))
        )
        end = elapsed + seconds
        heappop(free_slots[activity])
        heappush(free_slots[activity], end)
        if consumers[tid]:
            heappush(arrivals, (end, tid, size * row.output_per_run))
        if row.start is None:
            row.start = elapsed
        row.end = max(row.end, end)
        enqueue(tid)


def estimate_schedule(steps, preferences, running_jobs=(), now=None, root_id=None):
    now = now or timezone.now()
    slots = {activity: getattr(preferences, f"{activity}_slots") for activity in ACTIVITIES}
    job_releases = defaultdict(list)
    for job in running_jobs:
        activity = JOB_ACTIVITIES.get(job.activity_id)
        if activity in slots:
            job_releases[activity].append(release_after(job, now))
    free_slots = {}
    for activity, count in slots.items():
        releases = sorted(job_releases[activity])
        # If more jobs are installed than configured slots, the first few
        # completions reduce the excess before any slot becomes free.
        free_slots[activity] = (
            (releases[-count:] + [0] * max(0, count - len(releases))) if count else [inf]
        )
        heapify(free_slots[activity])

    work, consumers = remaining_work(steps, root_id)
    available = defaultdict(int)
    arrivals = []
    remaining_jobs = 0
    for tid, row in work.items():
        step = row.step
        available[tid] = row.completed
        ongoing = row.ongoing
        running = False
        allocations = (
            a for a in step.allocations.all() if a.quantity and a.job.status in {"active", "paused"}
        )
        for allocation in sorted(allocations, key=lambda a: release_after(a.job, now)):
            if not ongoing:
                break
            running = True
            release = release_after(allocation.job, now)
            quantity = min(ongoing, allocation.quantity)
            ongoing -= quantity
            row.end = max(row.end, release)
            if isfinite(release) and consumers[tid]:
                heappush(arrivals, (release, tid, quantity))
        if ongoing:
            row.end = inf

        reason = ""
        if not isfinite(row.end):
            reason = "A linked job is paused or awaits an ESI status update."
        if row.batches and not slots[step.activity]:
            reason = f"Configure {step.get_activity_display().lower()} slots."
        if row.batches and row.seconds_per_run <= 0:
            reason = "Saved job duration is unavailable. Refresh this plan."
        jobs = sum(row.batches.values())
        remaining_jobs += jobs
        step.timing = dict(
            start_at=None,
            finish_at=None,
            finish_seconds=None,
            running=running,
            remaining_jobs=jobs,
            reason=reason,
            not_needed=row.needed == 0,
        )
    if remaining_jobs > 50000:
        for row in work.values():
            row.step.timing["reason"] = (
                "More than 50,000 remaining jobs. Increase batch sizes to estimate timing."
            )
        return dict(duration=None, finish_at=None, slots=slots, too_large=True)

    schedule_batches(work, consumers, free_slots, available, arrivals)
    for row in work.values():
        timing = row.step.timing
        if row.start is not None:
            timing["start_at"] = now + timedelta(seconds=row.start)
        if not row.batches and isfinite(row.end):
            timing.update(finish_at=now + timedelta(seconds=row.end), finish_seconds=row.end)
        elif not timing["reason"]:
            timing["reason"] = "Waiting for batch inputs or a slot with a known release time."

    if root_id is None:
        duration = max((row.step.timing["finish_seconds"] or 0 for row in work.values()), default=0)
        if any(row.step.timing["finish_seconds"] is None for row in work.values()):
            duration = None
    else:
        duration = work[root_id].step.timing["finish_seconds"] if root_id in work else 0
    return dict(
        duration=duration,
        finish_at=now + timedelta(seconds=duration) if duration is not None else None,
        slots=slots,
        too_large=False,
    )
