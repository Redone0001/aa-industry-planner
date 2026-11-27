from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj

import pytest
from django.template.loader import render_to_string
from django.utils import timezone

from aa_industry_planner.services.blueprints import FormulaSupply
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


def test_formula_pools_are_shared_at_one_structure_and_release_independently():
    now = timezone.now()
    rows = [step(1, runs=2, batches=[1, 1]), step(2), step(3), step(4)]
    for row, location in zip(rows, [1001, 1001, 1002, 1003], strict=True):
        row.blueprint_type_id = 501
        row.details["context"]["structure_id"] = location
    formulas = {
        (501, 1001): FormulaSupply(available=1),
        (501, 1002): FormulaSupply(reserved=[job(now, 30, status="paused")]),
        (501, 1003): FormulaSupply(reserved=[job(now, 30, status="ready")]),
    }
    result = estimate_schedule(rows, prefs(reaction=20), now=now, reaction_formulas=formulas)
    assert result["duration"] is None
    assert rows[0].timing["finish_seconds"] == 20
    assert rows[1].timing["start_at"] == now + timedelta(seconds=20)
    assert rows[1].timing["finish_seconds"] == 30
    assert "without a known release time" in rows[2].timing["reason"]
    assert rows[3].timing["finish_seconds"] == 10


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


def test_twenty_manufacturing_slots_are_shared_by_independent_steps():
    now = timezone.now()
    branches = [step(tid, "manufacturing") for tid in range(1, 25)]
    final = step(25, "manufacturing", deps=range(1, 25))
    result = estimate_schedule([*branches, final], prefs(manufacturing=20), now=now, root_id=25)
    assert [row.timing["start_at"] for row in branches[:20]] == [now] * 20
    assert [row.timing["start_at"] for row in branches[20:]] == [now + timedelta(seconds=10)] * 4
    assert final.timing["start_at"] == now + timedelta(seconds=20)
    assert result["duration"] == 30


def test_downstream_batches_start_while_later_upstream_batches_are_still_building():
    now = timezone.now()
    upstream = step(1, "manufacturing", runs=5, batches=[2, 2, 1])
    downstream = step(2, "manufacturing", runs=5, batches=[1] * 5)
    downstream.details["materials"] = {"1": 5}
    downstream.details["recipe"]["materials"] = {"1": 1}
    result = estimate_schedule([upstream, downstream], prefs(manufacturing=2), now=now, root_id=2)
    # Two source jobs finish at t=20, releasing four units and both slots.
    # The tail uses one slot at t=20 while the first downstream job uses the other.
    assert upstream.timing["finish_seconds"] == 30
    assert downstream.timing["start_at"] == now + timedelta(seconds=20)
    assert result["duration"] == 50


def test_two_downstream_jobs_can_start_as_soon_as_one_upstream_batch_finishes():
    now = timezone.now()
    upstream = step(1, runs=4, batches=[2, 2])
    downstream = step(2, "manufacturing", runs=4, seconds=30, batches=[1] * 4)
    downstream.details["materials"] = {"1": 4}
    downstream.details["recipe"]["materials"] = {"1": 1}
    result = estimate_schedule([upstream, downstream], prefs(manufacturing=2), now=now, root_id=2)
    assert upstream.timing["finish_seconds"] == 40
    assert downstream.timing["start_at"] == now + timedelta(seconds=20)
    # Both initial jobs must start at t=20 to free both slots again at t=50.
    assert result["duration"] == 80


def test_shared_input_is_consumed_once_across_ready_branches():
    now = timezone.now()
    upstream = step(1, runs=2, batches=[1, 1])
    left = step(2, "manufacturing", deps=[1])
    right = step(3, "manufacturing", deps=[1])
    final = step(4, "manufacturing", deps=[2, 3])
    result = estimate_schedule(
        [upstream, left, right, final], prefs(manufacturing=2), now=now, root_id=4
    )
    assert left.timing["start_at"] == now + timedelta(seconds=10)
    assert right.timing["start_at"] == now + timedelta(seconds=20)
    assert result["duration"] == 40


def test_a_smaller_job_can_use_finished_output_before_a_full_batch_is_ready():
    now = timezone.now()
    upstream = step(1, runs=6, batches=[3, 3], completed=1)
    upstream.details["inventory_requirements"] = [{"consumer_id": 2, "from_stock": 2}]
    downstream = step(2, "manufacturing", runs=3, batches=[2, 1])
    downstream.details["materials"] = {"1": 8}
    downstream.details["recipe"]["materials"] = {"1": 3}
    downstream.details["context"]["material_multiplier"] = "0.7"
    result = estimate_schedule([upstream, downstream], prefs(), now=now, root_id=2)
    # The small job uses 3 units, covered by stock and one completed unit.
    # The 2-run job needs 5 fresh units, delivered by two more upstream batches.
    assert downstream.timing["start_at"] == now
    assert result["duration"] == 70


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
    rows[1].details["materials"] = {"1": 98}
    result = estimate_schedule(rows, prefs(reaction=0), linked, now)
    assert rows[0].timing["remaining_jobs"] == 0
    assert rows[0].timing["finish_seconds"] == 60
    assert result["duration"] == 70


def test_a_paused_upstream_job_allows_batches_with_other_inputs_to_start():
    now = timezone.now()
    linked = [job(now, 15), job(now, 25, status="paused")]
    upstream = step(1, runs=2, ongoing=2, linked=linked)
    downstream = step(2, "manufacturing", runs=2, batches=[1, 1])
    downstream.details["materials"] = {"1": 2}
    downstream.details["recipe"]["materials"] = {"1": 1}
    result = estimate_schedule([upstream, downstream], prefs(), linked, now, root_id=2)
    assert downstream.timing["start_at"] == now + timedelta(seconds=15)
    assert downstream.timing["finish_at"] is None
    assert result["duration"] is None


def test_partial_completed_and_ongoing_output_only_schedules_the_remainder():
    now = timezone.now()
    linked = [job(now, 100)]
    rows = [step(1, runs=98, seconds=1, batches=[50, 48], completed=20, ongoing=50, linked=linked)]
    result = estimate_schedule(rows, prefs(reaction=2), linked, now)
    assert rows[0].timing["remaining_jobs"] == 1
    assert rows[0].timing["start_at"] == now
    assert result["duration"] == 100  # 28 new units finish first; the existing job ends later.


def test_paused_jobs_do_not_invent_an_end_time():
    now = timezone.now()
    paused = job(now, -10, status="paused")
    rows = [
        step(1, ongoing=1, linked=[paused]),
        step(2, "manufacturing", deps=[1]),
        step(3, "manufacturing"),
    ]
    result = estimate_schedule(rows, prefs(), [paused], now)
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


@pytest.mark.parametrize(
    ("seconds", "remaining", "finish"),
    [
        (1, "1m", "27 Nov 2026 17:43 IST"),
        (60, "1m", "27 Nov 2026 17:44 IST"),
        (86_399, "1d", "28 Nov 2026 17:43 IST"),
        (86_400, "1d", "28 Nov 2026 17:43 IST"),
        (86_401, "1d 1h", "28 Nov 2026 18:00 IST"),
        (864_000, "10d", "07 Dec 2026 18:00 IST"),
        (864_001, "11d", "07 Dec 2026"),
    ],
)
def test_production_timing_rounds_estimate_for_display(seconds, remaining, finish):
    now = datetime(2026, 11, 27, 12, 12, 34, tzinfo=UTC)
    with timezone.override("Asia/Kolkata"):
        page = render_to_string(
            "aa_industry_planner/production_timing.html",
            {
                "progress": {"total": 1, "completed": 0, "in_jobs": 0, "waiting": 1},
                "timing": {"duration": seconds, "finish_at": now + timedelta(seconds=seconds)},
                "capacity": {"fresh": True},
            },
        )
    assert f"About {remaining} remaining</strong> · Estimated finish {finish}</p>" in page
