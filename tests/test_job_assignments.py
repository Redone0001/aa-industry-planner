import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from eve_sde.models import ItemType

from aa_industry_planner.models import (
    FacilityAssignment,
    FacilityService,
    InventoryWatch,
    UserPreferences,
)
from aa_industry_planner.services.jobs import allocate_job, job_output, reconcile_jobs
from aa_industry_planner.services.planning import create_plan
from aa_industry_planner.services.watches import evaluate_watches
from tests.conftest import add_recipe
from tests.test_services import make_job


@pytest.fixture
def silicon_plan(user, types, facility):
    product = ItemType.objects.create(id=16658, name="Silicon Diborite", group=types[1].group)
    blueprint = ItemType.objects.create(
        id=46179, name="Silicon Diborite Reaction Formula", group=types[1].group
    )
    # The test helper addresses a blueprint by product ID + 100.
    add_recipe(
        {16658: product, 16758: blueprint, 5: types[5]},
        16658,
        {5: 100},
        quantity=200,
        activity="reaction",
        seconds=10800,
    )
    service = FacilityService.objects.create(facility=facility.facility, activity="reaction")
    FacilityAssignment.objects.create(user=user, activity="reaction", service=service)
    UserPreferences.objects.create(user=user, reaction_runs=100)
    return create_plan(user, 16658, 3800, use_inventory=False)


@pytest.mark.parametrize("activity_id", [9, 11])
def test_corporation_silicon_job_matches_19_run_step_with_50_runs(
    user, source, silicon_plan, client, activity_id
):
    user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
    source.kind, source.owner_id = "corporation", 98000001
    source.save()
    job = make_job(
        user,
        source,
        installer_id=90000001,
        activity_id=activity_id,
        blueprint_type_id=46179,
        product_type_id=16658,
        runs=50,
    )
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:jobs"))
    step = silicon_plan.steps.get(item_type_id=16658)
    assert step.runs == 19 and step.output_quantity == 3800
    assert step.ongoing_progress == 100 and step.progress == 0
    assert job_output(job) == 10000 and job.allocation.quantity == 3800
    assert job.allocation.step_id == step.pk
    assert response.context["jobs"][0].assignment_selected == step.pk


def test_reaction_9_output_also_covers_inventory_watch(user, source, silicon_plan):
    make_job(user, source, activity_id=9, blueprint_type_id=46179, product_type_id=16658, runs=50)
    watch = InventoryWatch.objects.create(user=user, item_type_id=16658, minimum=3800)
    evaluate_watches(user)
    assert watch.task.state == "covered" and watch.task.quantity == 0


def test_job_can_move_between_plans_without_counting_twice(user, source, recipes, client):
    first = create_plan(user, 1, 98, use_inventory=False)
    second = create_plan(user, 1, 98, use_inventory=False)
    job = make_job(user, source, runs=100)
    reconcile_jobs(user)
    old = first.steps.get(item_type_id=1)
    target = second.steps.get(item_type_id=1)
    assert job.allocation.step_id == old.pk
    client.force_login(user)
    response = client.post(
        reverse("aa_industry_planner:assign_job", args=[job.pk]),
        {"step": target.pk, "page": "2"},
    )
    assert response.status_code == 302 and response.url.endswith("?page=2#all-industry-jobs")
    job.refresh_from_db()
    old.refresh_from_db()
    target.refresh_from_db()
    assert job.allocation.step_id == target.pk
    assert old.ongoing_quantity == 0 and target.ongoing_quantity == 98
    reconcile_jobs(user)
    assert second.steps.get(item_type_id=1).allocations.get().job_id == job.pk
    assert not first.steps.get(item_type_id=1).allocations.exists()


@pytest.mark.parametrize("failure", ["full", "structure"])
def test_failed_move_preserves_original_link_and_progress(
    user, source, recipes, facility, client, failure
):
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    first = create_plan(user, 1, 10, use_inventory=False)
    second = create_plan(user, 1, 10, use_inventory=False)
    job = make_job(user, source, runs=10)
    old = first.steps.get(item_type_id=1)
    target = second.steps.get(item_type_id=1)
    allocate_job(user, job.pk, old.pk)
    if failure == "full":
        other = make_job(user, source, job_id=456, runs=10)
        allocate_job(user, other.pk, target.pk)
    else:
        target.details["context"]["structure_id"] = 999
        target.save()
    client.force_login(user)
    response = client.post(
        reverse("aa_industry_planner:assign_job", args=[job.pk]), {"step": target.pk}
    )
    assert response.status_code == 302
    job.refresh_from_db()
    old.refresh_from_db()
    assert job.allocation.step_id == old.pk and old.ongoing_quantity == 10
    assert not job.auto_link_disabled


def test_all_jobs_unassign_is_sticky_and_assignment_reenables_matching(
    user, source, recipes, client
):
    production = create_plan(user, 1, 2)
    job = make_job(user, source)
    reconcile_jobs(user)
    step = production.steps.get(item_type_id=1)
    client.force_login(user)
    url = reverse("aa_industry_planner:assign_job", args=[job.pk])
    client.post(url, {"step": "unassigned"})
    client.get(reverse("aa_industry_planner:jobs"))
    job.refresh_from_db()
    assert job.auto_link_disabled and not step.allocations.exists()
    client.post(url, {"step": step.pk})
    job.refresh_from_db()
    assert not job.auto_link_disabled and job.allocation.step_id == step.pk


def test_move_from_archived_plan_updates_both_steps(user, source, recipes, client):
    first = create_plan(user, 1, 2)
    second = create_plan(user, 1, 2)
    job = make_job(user, source, status="ready")
    old = first.steps.get(item_type_id=1)
    target = second.steps.get(item_type_id=1)
    allocate_job(user, job.pk, old.pk)
    first.archived = True
    first.save()
    client.force_login(user)
    client.post(reverse("aa_industry_planner:assign_job", args=[job.pk]), {"step": target.pk})
    old.refresh_from_db()
    target.refresh_from_db()
    assert old.completed_quantity == 0 and target.completed_quantity == 2


def test_assignment_is_post_only_authorized_and_rejects_archived_destinations(
    user, other_user, source, recipes, client
):
    production = create_plan(user, 1, 2)
    foreign = create_plan(other_user, 1, 2)
    job = make_job(user, source)
    url = reverse("aa_industry_planner:assign_job", args=[job.pk])
    step = production.steps.get(item_type_id=1)
    client.force_login(user)
    assert client.get(url).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(url, {"step": step.pk}).status_code == 403
    assert client.post(url, {"step": foreign.steps.get(item_type_id=1).pk}).status_code == 404
    production.archived = True
    production.save()
    assert client.post(url, {"step": step.pk}).status_code == 404
    production.archived = False
    production.save()
    client.force_login(other_user)
    assert client.post(url, {"step": step.pk}).status_code == 404
    client.force_login(user)
    source.enabled = False
    source.save()
    assert client.post(url, {"step": "unassigned"}).status_code == 404


def test_all_jobs_orders_by_newest_start_not_end_or_last_sync(user, source, client):
    now = timezone.now()
    oldest = make_job(
        user,
        source,
        job_id=100,
        start_date=now - timezone.timedelta(days=5),
        end_date=now + timezone.timedelta(days=9),
    )
    middle = make_job(
        user, source, job_id=200, start_date=now, end_date=now + timezone.timedelta(hours=1)
    )
    newest = make_job(
        user, source, job_id=300, start_date=now, end_date=now + timezone.timedelta(days=10)
    )
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:jobs"))
    assert [j.pk for j in response.context["jobs"]] == [newest.pk, middle.pk, oldest.pk]
    assert b"newest started first" in response.content and b"<th>Started</th>" in response.content


def test_structure_mismatch_is_explained_without_offering_wrong_plan(
    user, source, recipes, facility, client
):
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    production = create_plan(user, 1, 2)
    make_job(user, source, facility_id=999)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:jobs"))
    job = response.context["jobs"][0]
    assert "Structure mismatch" in job.match_note
    assert "#999" in job.match_note and f"#{facility.facility.structure_id}" in job.match_note
    assert job.assignment_choices == []
    assert not production.steps.get(item_type_id=1).allocations.exists()


def test_foreign_installer_can_be_manually_assigned_but_not_automatically(
    user, source, recipes, client
):
    production = create_plan(user, 1, 2)
    job = make_job(user, source, installer_id=99999)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:jobs"))
    assert "installer is not one of your AA characters" in response.context["jobs"][0].match_note
    step = production.steps.get(item_type_id=1)
    assert not step.allocations.exists()
    client.post(reverse("aa_industry_planner:assign_job", args=[job.pk]), {"step": step.pk})
    assert job.allocation.step_id == step.pk
