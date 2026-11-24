from types import SimpleNamespace

import pytest
from django.urls import reverse

from aa_industry_planner.models import ManualStock
from aa_industry_planner.services.jobs import reconcile_jobs, update_progress
from aa_industry_planner.services.planning import create_plan, refresh_plan
from aa_industry_planner.services.readiness import annotate_steps, multibuy_text
from tests.test_services import make_job


def test_two_oversize_jobs_automatically_cover_98_units_without_completing_them(
    user, source, recipes, client
):
    production = create_plan(user, 1, 98, use_inventory=False)
    first = make_job(user, source, job_id=201, runs=50)
    second = make_job(user, source, job_id=202, runs=50)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    step = production.steps.get(item_type_id=1)
    assert step.allocations.count() == 2
    assert sum(step.allocations.values_list("quantity", flat=True)) == 98
    assert step.ongoing_quantity == 98 and step.ongoing_progress == 100
    assert step.completed_quantity == 0 and step.progress == 0
    assert b"100% ongoing" in response.content
    assert b"50 output units" in response.content
    assert b"2 linked jobs" in response.content
    graph = {n["id"]: n for n in response.context["dependency_graph"]["nodes"]}
    assert graph[1]["state"] == "active" and graph[1]["ongoing"] == 100
    second.status = "ready"
    second.save()
    update_progress(user)
    step.refresh_from_db()
    assert step.completed_quantity == 50 and step.ongoing_quantity == 48
    assert step.progress + step.ongoing_progress == 100
    first.status = "delivered"
    first.save()
    update_progress(user)
    step.refresh_from_db()
    assert step.progress == 100 and step.ongoing_progress == 0


def test_cancelled_or_unauthorized_jobs_do_not_count_as_ongoing(user, source, recipes):
    production = create_plan(user, 1, 98, use_inventory=False)
    job = make_job(user, source, runs=100)
    reconcile_jobs(user)
    step = production.steps.get(item_type_id=1)
    assert step.ongoing_progress == 100
    source.enabled = False
    source.save()
    update_progress(user)
    step.refresh_from_db()
    assert step.ongoing_quantity == step.completed_quantity == 0
    source.enabled = True
    source.save()
    job.status = "cancelled"
    job.save()
    reconcile_jobs(user)
    assert not step.allocations.exists()
    replacement = make_job(user, source, job_id=456, runs=100)
    reconcile_jobs(user)
    assert replacement.allocation.step_id == step.pk


def test_explicit_unlink_stays_unlinked_and_manual_relink_reenables_matching(
    user, source, recipes, client
):
    production = create_plan(user, 1, 2)
    job = make_job(user, source)
    reconcile_jobs(user)
    step = production.steps.get(item_type_id=1)
    client.force_login(user)
    url = reverse("aa_industry_planner:plan", args=[production.pk])
    client.post(reverse("aa_industry_planner:unlink_job", args=[job.allocation.pk]))
    client.get(url)
    assert not step.allocations.exists()
    job.refresh_from_db()
    assert job.auto_link_disabled
    client.post(
        reverse("aa_industry_planner:link_job", args=[production.pk]),
        {"step": step.pk, "job": job.pk},
    )
    job.refresh_from_db()
    assert not job.auto_link_disabled and job.allocation.step_id == step.pk


def test_checklist_is_private_persistent_and_does_not_affect_production(
    user, other_user, recipes, client
):
    production = create_plan(user, 1, 10, use_inventory=False)
    client.force_login(user)
    url = reverse("aa_industry_planner:check_plan_item", args=[production.pk, 1])
    before = [(s.item_type_id, s.runs, s.output_quantity) for s in production.steps.all()]
    assert client.get(url).status_code == 405
    assert client.post(url, {"checked": "on"}, HTTP_ACCEPT="application/json").json()["checked"]
    refresh_plan(user, production.pk)
    assert production.checklist.get(item_type_id=1).checked
    assert before == [(s.item_type_id, s.runs, s.output_quantity) for s in production.steps.all()]
    page = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    assert all(s.completed_quantity == 0 for s in page.context["steps"])
    assert next(n for n in page.context["dependency_graph"]["nodes"] if n["id"] == 1)["checked"]
    assert (
        client.post(
            reverse("aa_industry_planner:check_plan_item", args=[production.pk, 999]),
            {"checked": "on"},
        ).status_code
        == 404
    )
    client.force_login(other_user)
    assert client.post(url, {"checked": "on"}).status_code == 404
    client.force_login(user)
    assert not client.post(url, {}, HTTP_ACCEPT="application/json").json()["checked"]
    production.archived = True
    production.save()
    assert client.post(url, {"checked": "on"}).status_code == 404


def step(item, materials, *, locations=(10,), destination=10, completed=0, ongoing=0):
    return SimpleNamespace(
        item_type_id=item,
        details={
            "materials": materials,
            "context": {"stock_location_ids": locations, "structure_id": destination},
        },
        progress=completed,
        ongoing_quantity=ongoing,
    )


def test_readiness_requires_all_inputs_and_reserves_shared_stock_once():
    first, second, blocked = step(1, {"5": 8}), step(2, {"5": 8}), step(3, {"5": 1, "6": 1})
    annotate_steps([blocked, first, second], {5: {10: 8, 99: 100}}, checked={1})
    assert blocked.state == "blocked"
    assert first.state == "ready" and first.checked
    assert second.state == "blocked"
    assert not first.needs_haul


def test_ready_storage_is_marked_for_hauling_and_running_output_is_not_stock():
    active = step(1, {"5": 8}, ongoing=8)
    consumer = step(2, {"1": 8})
    storage = step(3, {"5": 8}, locations=(10, 20))
    annotate_steps([active, consumer, storage], {5: {20: 8}})
    assert active.state == "active" and consumer.state == "blocked"
    assert storage.state == "ready" and storage.needs_haul
    active.progress, active.ongoing_quantity = 100, 0
    annotate_steps([active, consumer], {})
    assert active.state == "complete" and consumer.state == "blocked"


def test_readiness_updates_from_latest_stock_without_recalculating_plan(
    user, recipes, assigned_facility, client
):
    from aa_industry_planner.models import InventorySource

    production = create_plan(user, 1, 10, use_inventory=False)
    source = InventorySource.objects.create(user=user, kind="manual", name="Stock")
    client.force_login(user)
    url = reverse("aa_industry_planner:plan", args=[production.pk])
    assert all(s.state == "blocked" for s in client.get(url).context["steps"])
    # Both manufacturing components need type 4. Supply the first one only.
    first = production.steps.filter(activity="manufacturing").exclude(item_type_id=1).first()
    stock = ManualStock.objects.create(
        source=source,
        item_type=recipes[4],
        quantity=first.details["materials"]["4"],
        facility=assigned_facility,
    )
    states = {s.item_type_id: s.state for s in client.get(url).context["steps"]}
    assert states[first.item_type_id] == "ready"
    assert sorted(states.values()) == ["blocked", "blocked", "blocked", "ready"]
    source.include_in_planning = False
    source.save()
    assert all(s.state == "blocked" for s in client.get(url).context["steps"])
    assert stock.quantity > 0


def test_multibuy_uses_net_shortages_and_plain_integer_quantities(user, recipes, client):
    production = create_plan(user, 1, 1000, use_inventory=False)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    inputs = response.context["inputs"]
    assert response.context["multibuy"] == f"Raw input {inputs[0]['missing']}"
    assert b"Copy buy all" in response.content and b"Copy unchecked" in response.content
    assert (
        multibuy_text(
            [
                {"name": "Zydrine", "missing": 1250000},
                {"name": "Isogen", "missing": 10},
                {"name": "Nocxium", "missing": 0},
            ]
        )
        == "Isogen 10\nZydrine 1250000"
    )


def test_partly_finished_step_needs_only_materials_for_remaining_runs(
    user, recipes, assigned_facility
):
    production = create_plan(user, 1, 98, use_inventory=False)
    step = production.steps.get(item_type_id=1)
    step.completed_quantity = 50
    annotate_steps(
        [step], {2: {assigned_facility.structure_id: 87}, 3: {assigned_facility.structure_id: 130}}
    )
    assert step.state == "ready"
    annotate_steps(
        [step], {2: {assigned_facility.structure_id: 86}, 3: {assigned_facility.structure_id: 130}}
    )
    assert step.state == "blocked"
    annotate_steps(
        [step],
        {2: {assigned_facility.structure_id: 1000}, 3: {assigned_facility.structure_id: 1000}},
        available_services=set(),
    )
    assert step.state == "blocked"


@pytest.mark.parametrize("status", ["active", "ready", "delivered"])
def test_deterministic_job_output_uses_runs_even_when_successful_runs_is_zero(
    user, source, recipes, status
):
    production = create_plan(user, 1, 98, use_inventory=False)
    make_job(user, source, runs=100, successful_runs=0, status=status)
    reconcile_jobs(user)
    step = production.steps.get(item_type_id=1)
    assert step.allocations.count() == 1
    assert (step.ongoing_quantity, step.completed_quantity) == (
        (98, 0) if status == "active" else (0, 98)
    )
