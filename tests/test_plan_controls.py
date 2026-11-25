from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.urls import reverse
from eve_sde.models import ItemCategory, ItemGroup, ItemType

from aa_industry_planner.engine import PlanningError
from aa_industry_planner.forms import AssignmentForm, PolicyForm, blueprint_rows
from aa_industry_planner.industry import BONUS_SCOPE_OFFSET
from aa_industry_planner.models import (
    Facility,
    FacilityAssignment,
    FacilityService,
    IndustryJob,
    ItemPolicy,
    PlanBlueprintOverride,
    ProductionPlan,
)
from aa_industry_planner.services.jobs import allocate_job
from aa_industry_planner.services.planning import calculate, create_plan, refresh_plan

from .conftest import add_recipe
from .test_services import make_job

pytestmark = pytest.mark.django_db


def blueprint_payload(production, *, me=10, te=20, overrides=None, inventory=False):
    rows = blueprint_rows(production)
    data = {
        "form": "blueprints",
        "settings-default_material_efficiency": me,
        "settings-default_time_efficiency": te,
        "blueprints-TOTAL_FORMS": len(rows),
        "blueprints-INITIAL_FORMS": len(rows),
        "blueprints-MIN_NUM_FORMS": 0,
        "blueprints-MAX_NUM_FORMS": 1501,
    }
    if inventory:
        data["settings-use_inventory"] = "on"
    for index, row in enumerate(rows):
        values = (overrides or {}).get(row["item_type_id"], (None, None))
        data[f"blueprints-{index}-item_type_id"] = row["item_type_id"]
        data[f"blueprints-{index}-material_efficiency"] = "" if values[0] is None else values[0]
        data[f"blueprints-{index}-time_efficiency"] = "" if values[1] is None else values[1]
    return data


def another_service(service, number=2, activity="manufacturing"):
    facility = Facility.objects.create(
        name=f"Campus {number}",
        structure_id=1000000000000 + number,
        solar_system=service.facility.solar_system,
    )
    return FacilityService.objects.create(facility=facility, activity=activity)


def test_planned_purchase_removes_only_its_share_of_shared_production(user, client, recipes):
    production = create_plan(user, 1, 10, use_inventory=False)
    client.force_login(user)
    purchase_url = reverse("aa_industry_planner:set_plan_purchase", args=[production.pk, 2])
    plan_url = reverse("aa_industry_planner:plan", args=[production.pk])
    original = production.snapshot["nodes"]
    assert client.post(purchase_url, {"action": "buy"}).url == plan_url + "#buy-list"
    page = client.get(plan_url)
    inputs = {row["type_id"]: row for row in page.context["inputs"]}
    steps = {step.item_type_id: step for step in page.context["steps"]}
    edges = {(e["source"], e["target"]): e for e in page.context["dependency_graph"]["edges"]}

    assert inputs[2]["missing"] == original["2"]["missing"] and inputs[2]["planned_purchase"]
    assert inputs[5]["missing"] < original["5"]["missing"]
    assert set(steps) == {1, 3, 4}
    assert steps[4].details["required"] == original["3"]["materials"]["4"]
    assert 0 < steps[4].removed_runs < original["4"]["runs"]
    assert edges[2, 4]["covered_quantity"] == original["2"]["materials"]["4"]
    assert edges[2, 4]["quantity"] == 0
    assert edges[3, 4]["quantity"] == original["3"]["materials"]["4"]
    assert "covered_quantity" not in edges[3, 4]
    assert b"Make instead" in page.content

    refreshed, _ = refresh_plan(user, production.pk)
    assert refreshed.snapshot["purchase_choices"] == {"2": "Component A"}
    assert client.post(purchase_url, {"action": "make"}).status_code == 302
    production.refresh_from_db()
    for tid, row in production.snapshot["nodes"].items():
        for field in ("required", "missing", "runs", "output", "materials"):
            assert row[field] == original[tid][field]
    assert "purchase_choices" not in production.snapshot


def test_root_purchase_keeps_only_final_output_on_the_buy_list_and_can_be_undone(
    user, client, recipes
):
    production = create_plan(user, 1, 10, use_inventory=False)
    client.force_login(user)
    root_url = reverse("aa_industry_planner:set_plan_purchase", args=[production.pk, 1])
    client.post(root_url, {"action": "buy"})
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    assert response.context["multibuy"] == "Final product 10"
    assert not response.context["steps"]
    assert {row["id"] for row in response.context["covered_steps"]} == {2, 3, 4}
    assert client.post(root_url, {"action": "make"}).status_code == 302
    assert production.steps.count() == 4


def test_purchase_choices_require_an_active_owned_plan_and_production_step(
    user, other_user, client, recipes
):
    plan = create_plan(user, 1, 1)
    client.force_login(other_user)
    url = reverse("aa_industry_planner:set_plan_purchase", args=[plan.pk, 2])
    assert client.post(url, {"action": "buy"}).status_code == 404
    client.force_login(user)
    assert client.get(url).status_code == 405
    assert client.post(url, {"action": "invalid"}).status_code == 400
    raw_url = reverse("aa_industry_planner:set_plan_purchase", args=[plan.pk, 5])
    assert client.post(raw_url, {"action": "buy"}).status_code == 404
    plan.archived = True
    plan.save(update_fields=["archived"])
    assert client.post(url, {"action": "buy"}).status_code == 404


def test_buy_list_classifies_inputs_using_sde_groups(user, client, types):
    material = ItemCategory.objects.create(id=4, name="Material")
    planetary = ItemCategory.objects.create(id=43, name="Planetary commodities")
    for tid, group_id, name, category in [
        (34, 18, "Tritanium", material),
        (16634, 427, "Cadmium", material),
        (9846, 1040, "Robotics", planetary),
    ]:
        group = ItemGroup.objects.create(id=group_id, name=str(group_id), category=category)
        types[tid] = ItemType.objects.create(id=tid, name=name, group=group, published=True)
    add_recipe(types, 1, {34: 10, 16634: 5, 9846: 1, 5: 2})
    production = create_plan(user, 1, 1, use_inventory=False)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    assert {row["name"]: row["category"] for row in response.context["inputs"]} == {
        "Tritanium": "minerals",
        "Cadmium": "moon-goo",
        "Robotics": "pi",
        "Raw input": "",
    }
    assert all(available for _, _, available in response.context["copy_categories"])
    assert b'data-buy-line="Cadmium 5"' in response.content


def test_blueprints_default_to_me10_te20_without_affecting_reactions(user, recipes):
    result, _ = calculate(user, 1, 100, use_inventory=False)
    assert result.nodes[1].materials == {2: 180, 3: 270}
    assert result.nodes[1].estimated_seconds == 4800
    for tid in [1, 2, 3]:
        assert (result.nodes[tid].context.me, result.nodes[tid].context.te) == (10, 20)
    assert (result.nodes[4].context.me, result.nodes[4].context.te) == (0, 0)


def test_plan_overrides_are_independent_of_other_plans_and_item_policies(user, recipes):
    first = create_plan(user, 1, 100, use_inventory=False, blueprint_overrides={2: (0, 0)})
    second = create_plan(user, 1, 100, use_inventory=False)
    assert first.snapshot["nodes"]["2"]["materials"] == {"4": 360}
    assert second.snapshot["nodes"]["2"]["materials"] == {"4": 324}
    assert not ItemPolicy.objects.exists()
    assert "material_efficiency" not in PolicyForm().fields
    assert "time_efficiency" not in PolicyForm().fields


def test_defaults_can_be_overridden_in_the_new_production_form(user, client, recipes):
    client.force_login(user)
    response = client.post(
        reverse("aa_industry_planner:plans"),
        {
            "item_type": "1 · Final product",
            "quantity": 100,
            "default_material_efficiency": 0,
            "default_time_efficiency": 0,
        },
    )
    assert response.status_code == 302
    production = ProductionPlan.objects.get()
    assert not production.use_inventory
    assert production.snapshot["nodes"]["1"]["materials"] == {"2": 200, "3": 300}
    assert production.snapshot["nodes"]["1"]["estimated_seconds"] == 6000


def test_refresh_applies_new_facility_in_place_and_keeps_blueprint_overrides(
    user, recipes, facility
):
    assignment = FacilityAssignment.objects.create(
        user=user, activity="manufacturing", service=facility
    )
    production = create_plan(user, 1, 100, use_inventory=False, blueprint_overrides={2: (2, 4)})
    before_time = production.created_at
    step = production.steps.get(item_type_id=1)
    new_service = another_service(facility)
    assignment.service = new_service
    assignment.save()
    refreshed, detached = refresh_plan(user, production.pk)
    assert refreshed.pk == production.pk
    assert refreshed.created_at == before_time
    assert refreshed.refreshed_at is not None
    assert detached == 0
    assert refreshed.steps.get(item_type_id=1).pk == step.pk
    assert (
        refreshed.snapshot["nodes"]["1"]["context"]["structure_id"]
        == new_service.facility.structure_id
    )
    assert refreshed.snapshot["nodes"]["2"]["context"]["me"] == 2
    assert refreshed.snapshot["nodes"]["2"]["context"]["te"] == 4
    assert not refreshed.snapshot["inventory_used"]


def test_refresh_keeps_compatible_job_links_and_progress(user, source, recipes, facility):
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    production = create_plan(user, 1, 2, use_inventory=False)
    step = production.steps.get(item_type_id=1)
    job = make_job(user, source, status="ready")
    allocate_job(user, job.pk, step.pk)
    refresh_plan(user, production.pk, default_me=0, default_te=0)
    step.refresh_from_db()
    assert step.completed_quantity == 2
    assert step.progress == 100
    assert job.allocation.step_id == step.pk


def test_refresh_releases_incompatible_links_but_never_deletes_jobs(
    user, source, recipes, facility
):
    assignment = FacilityAssignment.objects.create(
        user=user, activity="manufacturing", service=facility
    )
    production = create_plan(user, 1, 2, use_inventory=False)
    job = make_job(user, source)
    allocate_job(user, job.pk, production.steps.get(item_type_id=1).pk)
    assignment.service = another_service(facility)
    assignment.save()
    _, detached = refresh_plan(user, production.pk)
    assert detached == 1
    assert IndustryJob.objects.filter(pk=job.pk, status="active").exists()
    job.refresh_from_db()
    assert not hasattr(job, "allocation")


def test_failed_refresh_preserves_the_previous_snapshot_and_settings(user, recipes, facility):
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    production = create_plan(user, 1, 10, use_inventory=False, blueprint_overrides={2: (2, 4)})
    old_snapshot = production.snapshot
    structure = facility.facility
    structure.structure_type = recipes[1]  # An invalid hull in current configuration.
    structure.save()
    with pytest.raises(PlanningError):
        refresh_plan(user, production.pk, default_me=0, blueprint_overrides={2: (0, 0)})
    production.refresh_from_db()
    assert production.snapshot == old_snapshot
    assert production.default_material_efficiency == 10
    assert production.refreshed_at is None
    assert production.blueprint_overrides.get().material_efficiency == 2


def test_apply_blueprint_settings_accepts_zero_and_blank_inheritance(user, client, recipes):
    production = create_plan(user, 1, 100, use_inventory=False)
    client.force_login(user)
    response = client.post(
        reverse("aa_industry_planner:refresh_plan", args=[production.pk]),
        blueprint_payload(production, me=8, te=16, overrides={2: (0, 0), 3: (None, 4)}),
    )
    assert response.status_code == 302
    production.refresh_from_db()
    root = production.snapshot["nodes"]["1"]["context"]
    assert (root["me"], root["te"]) == (8, 16)
    assert production.snapshot["nodes"]["2"]["context"]["me"] == 0
    assert production.snapshot["nodes"]["3"]["context"]["me"] == 8
    assert production.snapshot["nodes"]["3"]["context"]["te"] == 4
    assert set(production.blueprint_overrides.values_list("item_type_id", flat=True)) == {2, 3}
    response = client.post(
        reverse("aa_industry_planner:refresh_plan", args=[production.pk]),
        blueprint_payload(production, me=6, te=12),
    )
    assert response.status_code == 302
    production.refresh_from_db()
    assert not production.blueprint_overrides.exists()
    assert production.snapshot["nodes"]["2"]["context"]["me"] == 6


@pytest.mark.parametrize(
    "problem", ["out_of_range", "foreign_item", "missing_row", "duplicate_row"]
)
def test_invalid_blueprint_edits_cannot_change_a_plan(user, client, recipes, problem):
    production = create_plan(user, 1, 10, use_inventory=False)
    snapshot = production.snapshot
    data = blueprint_payload(production)
    if problem == "out_of_range":
        data["blueprints-0-material_efficiency"] = 11
    elif problem == "foreign_item":
        data["blueprints-0-item_type_id"] = 5
    elif problem == "missing_row":
        data["blueprints-TOTAL_FORMS"] = 0
    else:
        data["blueprints-0-item_type_id"] = data["blueprints-1-item_type_id"]
    client.force_login(user)
    response = client.post(reverse("aa_industry_planner:refresh_plan", args=[production.pk]), data)
    assert response.status_code == 200
    production.refresh_from_db()
    assert production.snapshot == snapshot
    assert production.refreshed_at is None


def test_refresh_is_post_only_owner_scoped_and_unavailable_for_archived_plans(
    user, other_user, client, recipes
):
    own = create_plan(user, 1, 1)
    other = create_plan(other_user, 1, 1)
    client.force_login(user)
    own_url = reverse("aa_industry_planner:refresh_plan", args=[own.pk])
    assert client.get(own_url).status_code == 405
    assert (
        client.post(reverse("aa_industry_planner:refresh_plan", args=[other.pk])).status_code == 404
    )
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(own_url).status_code == 403
    own.archived = True
    own.save()
    assert client.post(own_url).status_code == 404


def test_remove_one_archived_plan_preserves_jobs_and_other_plans(user, source, client, recipes):
    active = create_plan(user, 1, 1)
    archived = create_plan(user, 1, 2)
    job = make_job(user, source)
    allocate_job(user, job.pk, archived.steps.get(item_type_id=1).pk)
    archived.archived = True
    archived.save()
    client.force_login(user)
    url = reverse("aa_industry_planner:delete_archived_plan", args=[archived.pk])
    assert client.get(url).status_code == 200
    assert ProductionPlan.objects.filter(pk=archived.pk).exists()
    assert client.post(url).status_code == 302
    assert not ProductionPlan.objects.filter(pk=archived.pk).exists()
    assert ProductionPlan.objects.filter(pk=active.pk).exists()
    assert IndustryJob.objects.filter(pk=job.pk).exists()
    assert not PlanBlueprintOverride.objects.filter(plan_id=archived.pk).exists()


def test_remove_all_archived_plans_is_owner_scoped_and_keeps_active_plans(
    user, other_user, client, recipes
):
    active = create_plan(user, 1, 1)
    for owner in [user, user, other_user]:
        p = create_plan(owner, 1, 1)
        p.archived = True
        p.save()
    client.force_login(user)
    assert client.post(reverse("aa_industry_planner:delete_archived_plans")).status_code == 302
    assert list(ProductionPlan.objects.filter(user=user).values_list("pk", flat=True)) == [
        active.pk
    ]
    assert ProductionPlan.objects.filter(user=other_user, archived=True).count() == 1
    assert (
        client.post(
            reverse("aa_industry_planner:delete_archived_plan", args=[active.pk])
        ).status_code
        == 404
    )
    other = ProductionPlan.objects.get(user=other_user)
    assert (
        client.post(
            reverse("aa_industry_planner:delete_archived_plan", args=[other.pk])
        ).status_code
        == 404
    )
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(reverse("aa_industry_planner:delete_archived_plans")).status_code == 403


def test_family_assignments_override_broad_ships_then_catch_all(user, recipes, facility):
    ships = another_service(facility, 2)
    small = another_service(facility, 3)
    components = another_service(facility, 4)
    category, _ = ItemCategory.objects.get_or_create(id=17, defaults={"name": "Material"})
    group, _ = ItemGroup.objects.get_or_create(
        id=873, defaults={"name": "Capital Components", "category": category}
    )
    recipes[3].group = group
    recipes[3].save()
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    broad = FacilityAssignment.objects.create(
        user=user, activity="manufacturing", bonus_family=3, service=ships
    )
    specific = FacilityAssignment.objects.create(
        user=user, activity="manufacturing", bonus_family=5, service=small
    )
    FacilityAssignment.objects.create(
        user=user, activity="manufacturing", bonus_family=13, service=components
    )
    assert specific.scope_key == BONUS_SCOPE_OFFSET + 5
    result, _ = calculate(user, 1, 10)
    assert result.nodes[1].context.service_id == small.pk
    assert result.nodes[3].context.service_id == components.pk
    specific.delete()
    result, _ = calculate(user, 1, 10)
    assert result.nodes[1].context.service_id == ships.pk
    broad.delete()
    result, _ = calculate(user, 1, 10)
    assert result.nodes[1].context.service_id == facility.pk


def test_assignment_form_uses_bonus_families_and_preserves_an_empty_catch_all(
    user, client, facility
):
    client.force_login(user)
    form = AssignmentForm(user=user)
    assert "item_group" not in form.fields
    assert "bonus_family" in form.fields
    for family in [5, ""]:
        response = client.post(
            reverse("aa_industry_planner:configuration"),
            {
                "form": "assignment",
                "activity": "manufacturing",
                "bonus_family": family,
                "service": facility.pk,
            },
        )
        assert response.status_code == 302
    assert FacilityAssignment.objects.filter(user=user, scope_key=0).exists()
    assert FacilityAssignment.objects.filter(user=user, bonus_family=5).exists()
    invalid = FacilityAssignment(
        user=user, activity="manufacturing", bonus_family=18, service=facility
    )
    with pytest.raises(ValidationError):
        invalid.full_clean()


def test_reaction_family_selects_the_matching_reactor_location(user, recipes, facility):
    category, _ = ItemCategory.objects.get_or_create(id=4, defaults={"name": "Material"})
    group, _ = ItemGroup.objects.get_or_create(
        id=428, defaults={"name": "Intermediate Materials", "category": category}
    )
    recipes[4].group = group
    recipes[4].save()
    service = another_service(facility, activity="reaction")
    FacilityAssignment.objects.create(
        user=user, activity="reaction", bonus_family=18, service=service
    )
    result, _ = calculate(user, 1, 10)
    assert result.nodes[4].context.service_id == service.pk
    assert (result.nodes[4].context.me, result.nodes[4].context.te) == (0, 0)


def test_migration_preserves_saved_plan_efficiencies_without_changing_snapshots(user, recipes):
    snapshot = {
        "inventory_used": False,
        "nodes": {
            "1": {"recipe": {"activity": "manufacturing"}, "context": {"me": 2, "te": 4}},
            "4": {"recipe": {"activity": "reaction"}, "context": {"me": 0, "te": 0}},
        },
    }
    production = ProductionPlan.objects.create(
        user=user, item_type=recipes[1], quantity=1, snapshot=snapshot
    )
    migration = import_module(
        "aa_industry_planner.migrations.0005_remove_itempolicy_material_efficiency_and_more"
    )
    migration.preserve_saved_blueprints(apps, SimpleNamespace(connection=connection))
    production.refresh_from_db()
    assert production.snapshot == snapshot
    assert not production.use_inventory
    row = production.blueprint_overrides.get()
    assert (row.item_type_id, row.material_efficiency, row.time_efficiency) == (1, 2, 4)
