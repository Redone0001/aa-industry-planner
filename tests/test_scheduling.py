from datetime import timedelta
from types import SimpleNamespace as Obj

from django.utils import timezone

from aa_industry_planner.services.scheduling import estimate_schedule


def step(
    tid,
    activity="reaction",
    runs=1,
    seconds=10,
    batches=None,
    deps=(),
    completed=0,
    ongoing=0,
    linked=(),
    position=None,
    output=1,
):
    return Obj(
        item_type_id=tid,
        position=position or tid,
        activity=activity,
        runs=runs,
        output_quantity=runs * output,
        completed_quantity=completed,
        ongoing_quantity=ongoing,
        allocations=Obj(
            all=lambda: [
                Obj(quantity=max(1, (ongoing + len(linked) - 1) // len(linked)), job=job)
                for job in linked
            ]
        ),
        details={
            "recipe": {"seconds": seconds, "output": output},
            "context": {"time_multiplier": "1", "te": 0},
            "materials": {str(t): 1 for t in deps},
            "batches": batches or [runs],
        },
        get_activity_display=lambda: "Reactions" if activity == "reaction" else "Manufacturing",
    )


def prefs(manufacturing=1, reaction=1):
    return Obj(manufacturing_slots=manufacturing, reaction_slots=reaction)


def job(now, seconds, activity=11, status="active"):
    return Obj(end_date=now + timedelta(seconds=seconds), activity_id=activity, status=status)


def test_batches_use_parallel_slots_and_keep_the_smaller_final_job():
    rows = [step(1, runs=5, batches=[2, 2, 1])]
    parallel = estimate_schedule(rows, prefs(reaction=2))
    assert parallel["duration"] == 30
    assert rows[0].timing["remaining_jobs"] == 3
    serial = estimate_schedule(rows, prefs(reaction=1))
    assert serial["duration"] == 50


def test_independent_work_fills_slots_before_future_dependencies_are_ready():
    now = timezone.now()
    rows = [
        step(3, "manufacturing", seconds=100, position=1),
        step(1, deps=[3], position=2),
        step(2, position=3),
        step(4, "manufacturing", seconds=5, deps=[1, 2]),
    ]
    result = estimate_schedule(rows, prefs(), now=now)
    assert rows[2].timing["start_at"] == now
    assert rows[1].timing["start_at"] == now + timedelta(seconds=100)
    assert result["duration"] == 115


def test_jobs_outside_the_plan_reserve_capacity_and_excess_jobs_release_first():
    now = timezone.now()
    rows = [step(1)]
    result = estimate_schedule(
        rows, prefs(reaction=2), [job(now, 10), job(now, 20), job(now, 30), job(now, 40)], now
    )
    assert rows[0].timing["start_at"] == now + timedelta(seconds=30)
    assert result["duration"] == 40


def test_two_oversize_linked_jobs_cover_output_without_reinstalling_it():
    now = timezone.now()
    linked = [job(now, 40, 9), job(now, 60, 11)]
    rows = [step(1, runs=98, ongoing=98, linked=linked), step(2, "manufacturing", deps=[1])]
    result = estimate_schedule(rows, prefs(reaction=0), linked, now)
    assert rows[0].timing["remaining_jobs"] == 0
    assert rows[0].timing["finish_seconds"] == 60
    assert result["duration"] == 70


def test_partial_completed_and_ongoing_output_only_schedules_the_remainder():
    now = timezone.now()
    linked = [job(now, 100)]
    rows = [step(1, runs=98, seconds=1, batches=[50, 48], completed=20, ongoing=50, linked=linked)]
    result = estimate_schedule(rows, prefs(reaction=2), linked, now)
    assert rows[0].timing["remaining_jobs"] == 1
    assert rows[0].timing["start_at"] == now
    assert result["duration"] == 100  # 28 new units finish first; the existing job ends later.


def test_paused_or_unconfirmed_jobs_do_not_invent_an_end_time():
    now = timezone.now()
    for blocked in [job(now, 100, status="paused"), job(now, -10)]:
        rows = [
            step(1, ongoing=1, linked=[blocked]),
            step(2, "manufacturing", deps=[1]),
            step(3, "manufacturing"),
        ]
        result = estimate_schedule(rows, prefs(), [blocked], now)
        assert result["duration"] is None
        assert rows[0].timing["finish_at"] is None
        assert rows[1].timing["finish_at"] is None
        assert rows[2].timing["finish_seconds"] == 10


def test_missing_slots_blocks_only_new_work_and_completed_steps_need_no_capacity():
    rows = [step(1, completed=1), step(2, "manufacturing", deps=[1])]
    assert estimate_schedule(rows, prefs(reaction=0))["duration"] == 10
    assert estimate_schedule(rows, prefs(manufacturing=0))["duration"] is None
    assert "Configure manufacturing slots" in rows[1].timing["reason"]


def test_time_bonuses_and_whole_second_rounding_apply_per_job():
    row = step(1, "manufacturing", runs=5, seconds=3, batches=[2, 2, 1])
    row.details["context"] = {"time_multiplier": ".5", "te": 20}
    # Three jobs: ceil(2.4), ceil(2.4), ceil(1.2), not ceil(6 total seconds).
    assert estimate_schedule([row], prefs())["duration"] == 8


def test_missing_duration_does_not_turn_into_a_one_second_job():
    row = step(1, seconds=0)
    assert estimate_schedule([row], prefs())["duration"] is None
    assert "duration is unavailable" in row.timing["reason"]


def test_fully_ongoing_final_output_does_not_schedule_its_consumed_inputs_again():
    now = timezone.now()
    active = job(now, 50, activity=1)
    rows = [
        step(1, runs=100, seconds=1000),
        step(2, "manufacturing", deps=[1], ongoing=1, linked=[active]),
    ]
    result = estimate_schedule(rows, prefs(), [active], now, root_id=2)
    assert result["duration"] == 50
    assert rows[0].timing["remaining_jobs"] == 0
    assert rows[0].timing["not_needed"]


def test_partial_downstream_coverage_reduces_upstream_work_without_reusing_consumed_stock():
    now = timezone.now()
    active = job(now, 100, activity=1)
    child = step(1, runs=3, seconds=10)
    child.details["inventory_requirements"] = [{"consumer_id": 2, "from_stock": 1}]
    parent = step(2, "manufacturing", runs=2, seconds=10, deps=[1], ongoing=1, linked=[active])
    parent.details["materials"] = {"1": 4}
    parent.details["recipe"]["materials"] = {"1": 2}
    result = estimate_schedule([child, parent], prefs(manufacturing=2), [active], now, root_id=2)
    # One parent run consumed two inputs, including its one original stock unit.
    # Two new inputs are required; the consumed stock unit is not available again.
    assert child.timing["finish_seconds"] == 20
    assert result["duration"] == 100


def test_consumed_completed_upstream_output_is_not_credited_to_the_remaining_parent_twice():
    now = timezone.now()
    active = job(now, 100, activity=1)
    child = step(1, runs=4, seconds=10, completed=2)
    parent = step(2, "manufacturing", runs=2, seconds=10, deps=[1], ongoing=1, linked=[active])
    parent.details["materials"] = {"1": 4}
    parent.details["recipe"]["materials"] = {"1": 2}
    estimate_schedule([child, parent], prefs(manufacturing=2), [active], now, root_id=2)
    assert child.timing["finish_seconds"] == 20


def test_ready_job_slots_can_be_reused_under_the_immediate_delivery_assumption():
    now = timezone.now()
    ready = job(now, -100, status="ready")
    assert estimate_schedule([step(1)], prefs(), [ready], now)["duration"] == 10
