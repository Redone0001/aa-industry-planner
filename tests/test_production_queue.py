import pytest
from django.urls import reverse

from aa_industry_planner.models import (
    Facility,
    FacilityAssignment,
    FacilityService,
    InventorySource,
    ManualStock,
    ProductionPlan,
    UserPreferences,
)
from aa_industry_planner.services.planning import create_plan

from .conftest import add_recipe
from .test_services import make_job

pytestmark = pytest.mark.django_db


def target_form(*rows):
    form = {
        "name": "Joint build",
        "default_material_efficiency": 10,
        "default_time_efficiency": 20,
        "targets-TOTAL_FORMS": len(rows),
        "targets-INITIAL_FORMS": 0,
    }
    for index, (tid, quantity) in enumerate(rows):
        form[f"targets-{index}-item_type"] = tid
        form[f"targets-{index}-quantity"] = quantity
    return form


def test_create_one_plan_for_staged_targets_and_refresh_it(user, client, recipes):
    client.force_login(user)
    url = reverse("aa_industry_planner:plans")
    assert b"Add another item" in client.get(url).content
    staged = target_form((1, 1), (2, 2), (3, 999), (2, 1))
    staged["targets-2-DELETE"] = "on"
    response = client.post(url, staged)
    assert response.status_code == 302
    production = ProductionPlan.objects.get()
    assert [(item.pk, quantity) for item, quantity in production.targets] == [(1, 1), (2, 3)]
    assert production.snapshot["nodes"]["2"]["required"] == 5
    assert production.steps.get(item_type_id=4).runs == 3
    detail = client.get(response.url)
    assert detail.context["dependency_graph"]["roots"] == [1, 2]
    assert detail.context["tree"]["additional_targets"][0]["reference"]
    assert b"Component A \xc3\x97 3" in detail.content
    step_ids = list(production.steps.values_list("pk", flat=True))
    refresh = reverse("aa_industry_planner:refresh_plan", args=[production.pk])
    assert client.post(refresh).status_code == 302
    assert list(production.steps.values_list("pk", flat=True)) == step_ids
    production.refresh_from_db()
    assert production.snapshot["nodes"]["2"]["required"] == 5


def test_invalid_target_keeps_draft_and_creates_no_plan(user, client, recipes):
    client.force_login(user)
    response = client.post(reverse("aa_industry_planner:plans"), target_form((1, 3), (2, 0)))
    assert response.status_code == 200
    assert b"greater than or equal to 1" in response.content
    assert response.context["targets"][0]["quantity"].value() == "3"
    assert not ProductionPlan.objects.exists()


def test_default_blueprint_settings_expand_when_a_value_is_invalid(user, client, recipes):
    client.force_login(user)
    url = reverse("aa_industry_planner:plans")
    page = client.get(url)
    assert b"data-blueprint-defaults >" in page.content
    assert b"Default blueprint ME/TE" in page.content
    assert b'name="default_material_efficiency" value="10"' in page.content
    form = target_form((1, 1)) | {"default_material_efficiency": 11}
    invalid = client.post(url, form)
    assert b"data-blueprint-defaults open>" in invalid.content
    assert invalid.context["form"]["default_material_efficiency"].errors
    assert not ProductionPlan.objects.exists()


def test_forecast_accounts_for_independent_outputs(user, client, types):
    add_recipe(types, 2, {5: 1}, seconds=60)
    add_recipe(types, 3, {5: 1}, seconds=150)
    UserPreferences.objects.create(user=user, manufacturing_slots=2)
    client.force_login(user)
    saved = client.post(reverse("aa_industry_planner:plans"), target_form((2, 1), (3, 1)))
    response = client.get(saved.url)
    assert response.context["timing"]["duration"] == 120


def test_queue_keeps_stock_that_fulfils_a_requested_output(user, client, types, assigned_facility):
    add_recipe(types, 2, {5: 1})
    add_recipe(types, 3, {2: 1})
    source = InventorySource.objects.create(user=user, kind="manual", name="Local stock")
    ManualStock.objects.create(
        source=source, item_type=types[2], facility=assigned_facility, quantity=1
    )
    client.force_login(user)
    staged = target_form((2, 1), (3, 1))
    staged["use_inventory"] = "on"
    assert client.post(reverse("aa_industry_planner:plans"), staged).status_code == 302
    response = client.get(reverse("aa_industry_planner:production_queue"))
    assert not response.context["ready"]
    assert response.context["multibuy"] == "Raw input 1"


def test_combined_queue_spends_stock_once_and_separates_hauling_from_buying(
    user,
    other_user,
    client,
    recipes,
    assigned_facility,
):
    reactor = FacilityService.objects.create(facility=assigned_facility, activity="reaction")
    FacilityAssignment.objects.create(user=user, activity="reaction", service=reactor)
    first = create_plan(user, 4, 10, "First reaction", use_inventory=False)
    second = create_plan(user, 4, 5, "Second reaction", use_inventory=False)
    archived = create_plan(user, 4, 500, "Archived reaction", use_inventory=False)
    archived.archived = True
    archived.save(update_fields=["archived"])
    create_plan(other_user, 4, 500, "Someone else's reaction", use_inventory=False)
    source = InventorySource.objects.create(user=user, kind="manual", name="Local stock")
    warehouse = Facility.objects.create(
        name="Warehouse",
        structure_id=1000000000002,
        solar_system=assigned_facility.solar_system,
    )
    FacilityService.objects.create(facility=warehouse, activity="manufacturing")
    ManualStock.objects.create(
        source=source, item_type=recipes[5], facility=assigned_facility, quantity=10
    )
    ManualStock.objects.create(source=source, item_type=recipes[5], facility=warehouse, quantity=3)

    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:production_queue"))
    assert response.status_code == 200
    assert [plan.pk for plan in response.context["plans"]] == [first.pk, second.pk]
    assert [(row["plan"].pk, row["runs"]) for row in response.context["ready"]] == [(first.pk, 1)]
    assert [row["plan"].pk for row in response.context["waiting"]] == [second.pk]
    (shortage,) = response.context["shortages"]
    assert (shortage["missing"], shortage["move"]) == (17, 3)
    assert shortage["transfers"] == [("Warehouse", 3)]
    assert response.context["multibuy"] == "Raw input 17"
    assert b'data-buy-line="Raw input 17"' in response.content
    assert b"Archived reaction" not in response.content
    assert b"Someone else" not in response.content


def test_combined_queue_omits_materials_for_installed_jobs_and_shows_delivery(
    user,
    client,
    source,
    recipes,
    assigned_facility,
):
    production = create_plan(user, 1, 2, use_inventory=False)
    job = make_job(user, source, runs=1)
    client.force_login(user)
    url = reverse("aa_industry_planner:production_queue")
    running = client.get(url)
    assert running.context["running"][0]["jobs"] == [job]
    assert running.context["multibuy"] == "Raw input 20"
    job.status = "ready"
    job.save(update_fields=["status"])
    ready = client.get(url)
    assert ready.context["delivery"][0]["jobs"] == [job]
    assert not ready.context["running"]
    assert ready.context["multibuy"] == running.context["multibuy"]
    assert f"/plans/{production.pk}/#step-".encode() in ready.content
