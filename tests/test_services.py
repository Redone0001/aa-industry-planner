from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from aa_industry_planner.engine import PlanningError
from aa_industry_planner.models import (
    Asset,
    FacilityAssignment,
    IndustryJob,
    InventorySource,
    InventoryWatch,
    ItemPolicy,
    ManualStock,
    Notification,
    ProductionPlan,
    ReplenishmentTask,
    SystemCostIndex,
)
from aa_industry_planner.services.inventory import authorized_sources, inventory_totals
from aa_industry_planner.services.jobs import allocate_job, reconcile_jobs, update_progress
from aa_industry_planner.services.planning import calculate, create_plan
from aa_industry_planner.services.sync import refresh_indices, sync_source, validate_source
from aa_industry_planner.services.watches import evaluate_watches, plan_replenishment

pytestmark = pytest.mark.django_db


def make_job(user, source, **options):
    fields = dict(
        user=user,
        source=source,
        job_id=123,
        installer_id=source.owner_id,
        activity_id=1,
        blueprint_type_id=101,
        product_type_id=1,
        facility_id=1000000000001,
        runs=2,
        status="active",
        start_date=timezone.now(),
        end_date=timezone.now() + timezone.timedelta(hours=1),
    )
    fields.update(options)
    return IndustryJob.objects.create(**fields)


def test_real_sde_plan_and_material_merging(user, recipes):
    result, snapshot = calculate(user, 1, 1)
    assert result.nodes[4].required == 7
    assert result.nodes[4].runs == 2
    production = create_plan(user, 1, 1)
    assert production.steps.count() == 4
    assert production.steps.first().item_type_id == 4
    assert snapshot["nodes"]["1"]["name"] == "Final product"


def test_manual_stock_is_scoped_and_can_be_excluded(user, other_user, types, assigned_facility):
    own = InventorySource.objects.create(user=user, kind="manual", name="Own")
    other = InventorySource.objects.create(user=other_user, kind="manual", name="Other")
    ManualStock.objects.create(
        source=own, item_type=types[1], quantity=7, facility=assigned_facility
    )
    ManualStock.objects.create(source=other, item_type=types[1], quantity=99)
    assert inventory_totals(user) == {1: 7}
    own.include_in_planning = False
    own.save()
    assert inventory_totals(user) == {}


def test_revoked_character_ownership_hides_assets(user, source, assigned_facility):
    Asset.objects.create(
        source=source, item_id=42, type_id=1, quantity=8, location_id=assigned_facility.structure_id
    )
    assert inventory_totals(user) == {1: 8}
    CharacterOwnership.objects.filter(user=user).delete()
    assert inventory_totals(user) == {}
    with pytest.raises(PermissionDenied):
        validate_source(source, Mock())


def test_corporation_permission_and_current_membership_required(user, source):
    source.kind, source.owner_id = "corporation", 98000001
    source.save()
    assert not authorized_sources(user).exists()
    user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
    for key in ["_perm_cache", "_user_perm_cache"]:
        user.__dict__.pop(key, None)
    assert authorized_sources(user).exists()
    character = CharacterOwnership.objects.get(user=user).character
    character.corporation_id = 98000002
    character.save(update_fields=["corporation_id"])
    assert not authorized_sources(user).exists()


def test_job_counts_once_and_completion_requires_reported_status(user, source, recipes):
    first = create_plan(user, 1, 2)
    second = create_plan(user, 1, 2)
    job = make_job(user, source, end_date=timezone.now() - timezone.timedelta(hours=1))
    reconcile_jobs(user)
    step = first.steps.get(item_type_id=1)
    step.refresh_from_db()
    assert step.progress == 0
    assert not second.steps.get(item_type_id=1).allocations.exists()
    job.status = "ready"
    job.save()
    update_progress(user)
    step.refresh_from_db()
    assert step.progress == 100
    with pytest.raises(ValueError, match="already assigned"):
        allocate_job(user, job.pk, second.steps.get(item_type_id=1).pk)


def test_wrong_facility_cannot_complete_step(user, source, recipes, facility):
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    production = create_plan(user, 1, 2)
    job = make_job(user, source, facility_id=999)
    with pytest.raises(ValueError, match="facility"):
        allocate_job(user, job.pk, production.steps.get(item_type_id=1).pk)


def test_cancelled_job_reopens_step(user, source, recipes):
    production = create_plan(user, 1, 2)
    job = make_job(user, source, status="ready")
    step = production.steps.get(item_type_id=1)
    allocate_job(user, job.pk, step.pk)
    job.status = "cancelled"
    job.save()
    update_progress(user)
    step.refresh_from_db()
    assert step.completed_quantity == 0
    assert not step.allocations.exists()


def test_old_finished_history_is_not_automatically_claimed(user, source, recipes):
    job = make_job(
        user, source, status="delivered", start_date=timezone.now() - timezone.timedelta(days=20)
    )
    production = create_plan(user, 1, 2)
    assert not hasattr(job, "allocation")
    allocate_job(user, job.pk, production.steps.get(item_type_id=1).pk)
    assert production.steps.get(item_type_id=1).progress == 100


def test_watch_updates_one_task_and_input_is_not_produced(user, recipes):
    watch = InventoryWatch.objects.create(user=user, item_type=recipes[1], minimum=10)
    evaluate_watches(user)
    evaluate_watches(user)
    assert ReplenishmentTask.objects.filter(watch=watch).count() == 1
    assert watch.task.quantity == 10
    assert Notification.objects.filter(kind="inventory_low").count() == 1
    ItemPolicy.objects.create(user=user, item_type=recipes[1], buy=True)
    evaluate_watches(user)
    watch.task.refresh_from_db()
    assert watch.task.is_input
    with pytest.raises(ValueError):
        plan_replenishment(user, watch.task.pk)
    assert not ProductionPlan.objects.exists()


def test_watch_plan_uses_net_shortage_without_subtracting_stock_twice(
    user, recipes, assigned_facility
):
    source = InventorySource.objects.create(user=user, kind="manual", name="Manual")
    ManualStock.objects.create(
        source=source, item_type=recipes[1], quantity=4, facility=assigned_facility
    )
    watch = InventoryWatch.objects.create(user=user, item_type=recipes[1], minimum=10)
    evaluate_watches(user)
    production = plan_replenishment(user, watch.task.pk)
    assert production.quantity == 6
    assert production.steps.get(item_type_id=1).output_quantity == 6
    evaluate_watches(user)
    watch.task.refresh_from_db()
    assert watch.task.quantity == 0
    assert watch.task.state == "covered"
    with pytest.raises(ValueError):
        plan_replenishment(user, watch.task.pk)


def test_running_jobs_cover_watch_deficit(user, source, recipes, assigned_facility):
    watch = InventoryWatch.objects.create(user=user, item_type=recipes[1], minimum=10)
    make_job(user, source, runs=7)
    evaluate_watches(user)
    assert watch.task.quantity == 3


def test_watch_bulk_plan_combines_net_shortages_and_keeps_per_item_planning(
    user, other_user, source, recipes, assigned_facility, client
):
    stock = InventorySource.objects.create(user=user, kind="manual", name="On hand")
    ManualStock.objects.create(
        source=stock, item_type=recipes[1], quantity=4, facility=assigned_facility
    )
    final = InventoryWatch.objects.create(user=user, item_type=recipes[1], minimum=10)
    component = InventoryWatch.objects.create(user=user, item_type=recipes[2], minimum=5)
    existing = InventoryWatch.objects.create(user=user, item_type=recipes[3], minimum=2)
    raw = InventoryWatch.objects.create(user=user, item_type=recipes[5], minimum=100)
    foreign = InventoryWatch.objects.create(user=other_user, item_type=recipes[4], minimum=10)
    already_running = make_job(user, source, blueprint_type_id=102, product_type_id=2, runs=2)
    evaluate_watches(user)
    evaluate_watches(other_user)
    client.force_login(user)
    watch_url = reverse("aa_industry_planner:watches")
    bulk_url = reverse("aa_industry_planner:replenish_all")
    item_url = reverse("aa_industry_planner:replenish", args=[existing.task.pk])
    page = client.get(watch_url)
    assert b"Plan all production shortages" in page.content
    assert f'action="{item_url}"'.encode() in page.content
    assert client.get(bulk_url).status_code == 405
    assert client.post(item_url).status_code == 302
    existing_plan = ProductionPlan.objects.get(user=user)

    response = client.post(bulk_url)
    combined = ProductionPlan.objects.exclude(pk=existing_plan.pk).get(user=user)
    assert response.url == reverse("aa_industry_planner:plan", args=[combined.pk])
    assert {item.pk: quantity for item, quantity in combined.targets} == {1: 6, 2: 3}
    component_step = combined.steps.get(item_type_id=2)
    assert component_step.output_quantity > 3
    assert not component_step.allocations.filter(job=already_running).exists()
    for watch in (final, component):
        watch.task.refresh_from_db()
        assert watch.task.plan_id == combined.pk
        assert (watch.task.quantity, watch.task.state) == (0, "covered")
    existing.task.refresh_from_db()
    assert existing.task.plan_id == existing_plan.pk
    raw.task.refresh_from_db()
    assert raw.task.is_input and raw.task.plan_id is None
    assert foreign.task.plan_id is None
    page = client.get(watch_url)
    assert page.content.count(f'href="{response.url}"'.encode()) == 2
    assert b"Plan all production shortages" not in page.content
    assert client.post(bulk_url).url == watch_url
    assert ProductionPlan.objects.filter(user=user).count() == 2

    component.minimum = 7
    component.save(update_fields=["minimum"])
    new_job = make_job(
        user,
        source,
        job_id=456,
        blueprint_type_id=102,
        product_type_id=2,
        runs=2,
        start_date=combined.created_at + timezone.timedelta(seconds=1),
    )
    reconcile_jobs(user)
    assert new_job.allocation.step_id == component_step.pk
    evaluate_watches(user)
    component.task.refresh_from_db()
    assert (component.task.quantity, component.task.state) == (2, "open")


def test_indices_hourly_idempotent_and_retained_90_days(facility):
    now = timezone.now().replace(minute=30)
    SystemCostIndex.objects.create(
        solar_system=facility.facility.solar_system,
        activity="manufacturing",
        cost_index=Decimal(".01"),
        recorded_at=now - timezone.timedelta(days=91),
    )
    keep = SystemCostIndex.objects.create(
        solar_system=facility.facility.solar_system,
        activity="manufacturing",
        cost_index=Decimal(".02"),
        recorded_at=now - timezone.timedelta(days=89),
    )
    api = Mock()
    api.indices.return_value = [
        {
            "solar_system_id": facility.facility.solar_system_id,
            "cost_indices": [{"activity": "manufacturing", "cost_index": 0.03}],
        }
    ]
    refresh_indices(api, now)
    refresh_indices(api, now)
    assert SystemCostIndex.objects.count() == 2
    assert SystemCostIndex.objects.filter(pk=keep.pk).exists()


def test_failed_job_fetch_does_not_wipe_previous_assets(user, source):
    Asset.objects.create(source=source, item_id=42, type_id=1, quantity=5, location_id=60000001)
    api = Mock()
    api.assets.return_value = []
    api.jobs.side_effect = RuntimeError("ESI unavailable")
    with patch("aa_industry_planner.services.sync.validate_source"), pytest.raises(RuntimeError):
        sync_source(source, api)
    assert Asset.objects.get(item_id=42).quantity == 5


def test_sde_alternative_recipes_require_explicit_choice(user, recipes):
    from .conftest import add_recipe

    add_recipe(recipes, 1, {5: 5}, activity="reaction")
    with pytest.raises(PlanningError, match="multiple recipes"):
        calculate(user, 1, 1)


@pytest.mark.parametrize("use_finish_time", [False, True])
def test_full_sync_lifecycle_is_idempotent_and_completes_linked_step(
    user, source, recipes, use_finish_time
):
    production = create_plan(user, 1, 2)
    now = timezone.now()
    data = {
        "job_id": 456,
        "installer_id": source.owner_id,
        "activity_id": 1,
        "blueprint_type_id": 101,
        "product_type_id": 1,
        "facility_id": 1000000000001,
        "runs": 2,
        "status": "active",
        "start_date": now.isoformat(),
        "end_date": (now + timezone.timedelta(hours=1)).isoformat(),
    }
    api = Mock()
    api.assets.return_value = [
        {"item_id": 42, "type_id": 5, "quantity": 90, "location_id": 60000001}
    ]
    api.jobs.return_value = [data]
    with (
        patch("aa_industry_planner.services.sync.validate_source"),
        patch("aa_industry_planner.tasks.resolve_entity_names.delay"),
    ):
        sync_source(source, api)
        source.refresh_from_db()
        sync_source(source, api)
        assert IndustryJob.objects.count() == 1
        assert Asset.objects.count() == 1
        assert not Notification.objects.filter(kind="job_finished").exists()
        assert production.steps.get(item_type_id=1).progress == 0
        if use_finish_time:
            with patch.object(timezone, "now", return_value=now + timezone.timedelta(hours=1)):
                sync_source(source, api)
                sync_source(source, api)
            reminder = Notification.objects.get(kind="job_finished")
            assert reminder.title == "Industry job delivery reminder"
            assert "Check Industry in EVE" in reminder.message
            assert IndustryJob.objects.get().status == "active"
            assert production.steps.get(item_type_id=1).progress == 0
        data["status"] = "ready"
        sync_source(source, api)
        assert production.steps.get(item_type_id=1).progress == 100
        data["status"] = "delivered"
        sync_source(source, api)
    assert Notification.objects.filter(kind="job_finished").count() == 1


def test_initial_history_import_does_not_spam_completions(user, source):
    api = Mock()
    api.assets.return_value = []
    now = timezone.now()
    api.jobs.return_value = [
        {
            "job_id": 789,
            "installer_id": source.owner_id,
            "activity_id": 1,
            "blueprint_type_id": 101,
            "product_type_id": 1,
            "facility_id": 1000000000001,
            "runs": 2,
            "status": "delivered",
            "start_date": (now - timezone.timedelta(days=7)).isoformat(),
            "end_date": (now - timezone.timedelta(days=6)).isoformat(),
        }
    ]
    with (
        patch("aa_industry_planner.services.sync.validate_source"),
        patch("aa_industry_planner.tasks.resolve_entity_names.delay"),
    ):
        sync_source(source, api)
    assert IndustryJob.objects.count() == 1
    assert not Notification.objects.filter(kind="job_finished").exists()


def test_corporation_import_deduplicates_personal_jobs(user, source):
    job = make_job(user, source)
    corp = InventorySource.objects.create(
        user=user, kind="corporation", owner_id=98000001, name="Corporation", token=source.token
    )
    api = Mock()
    api.assets.return_value = []
    api.jobs.return_value = [
        {
            "job_id": job.job_id,
            "installer_id": source.owner_id,
            "activity_id": 1,
            "blueprint_type_id": 101,
            "product_type_id": 1,
            "facility_id": 1000000000001,
            "runs": 2,
            "status": "active",
            "start_date": job.start_date.isoformat(),
            "end_date": job.end_date.isoformat(),
        }
    ]
    with (
        patch("aa_industry_planner.services.sync.validate_source"),
        patch("aa_industry_planner.tasks.resolve_entity_names.delay"),
    ):
        sync_source(corp, api)
    assert IndustryJob.objects.count() == 1
    job.refresh_from_db()
    assert job.source_id == source.pk


def test_director_role_and_esi_membership_checked_before_corp_fetch(user, source):
    user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
    source.kind, source.owner_id = "corporation", 98000001
    source.save()
    api = Mock()
    api.character.return_value = {"corporation_id": source.owner_id}
    api.roles.return_value = {"roles": ["Factory_Manager"]}
    with (
        patch("esi.managers.TokenQueryset.require_valid", lambda self: self),
        pytest.raises(PermissionDenied, match="Director"),
    ):
        validate_source(source, api)
    api.character.return_value = {"corporation_id": 98000002}
    with (
        patch("esi.managers.TokenQueryset.require_valid", lambda self: self),
        pytest.raises(PermissionDenied, match="left"),
    ):
        validate_source(source, api)


def test_product_group_assignment_overrides_activity_default(user, recipes, facility):
    from aa_industry_planner.models import Facility, FacilityService

    default_facility = Facility.objects.create(
        name="Default campus",
        structure_id=1000000000002,
        solar_system=facility.facility.solar_system,
    )
    default_service = FacilityService.objects.create(
        facility=default_facility, activity="manufacturing"
    )
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=default_service)
    override = FacilityAssignment.objects.create(
        user=user, activity="manufacturing", item_group=recipes[1].group, service=facility
    )
    result, _ = calculate(user, 1, 1)
    assert result.nodes[1].context.service_id == facility.pk
    assert override.scope_key == recipes[1].group_id
    override.delete()
    result, _ = calculate(user, 1, 1)
    assert result.nodes[1].context.service_id == default_service.pk
