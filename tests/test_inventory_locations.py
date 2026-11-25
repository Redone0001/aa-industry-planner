from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from esi.models import Scope
from eve_sde.models import BlueprintActivity, NPCStation

from aa_industry_planner.engine import (
    ProductionContext,
    Recipe,
    StockScope,
    allocate_location_stock,
    plan,
    stock_requirement,
)
from aa_industry_planner.models import (
    ActivityStorageLocation,
    Asset,
    Facility,
    FacilityAssignment,
    FacilityService,
    InventorySource,
    ManualStock,
    SavedStorageLocation,
)
from aa_industry_planner.providers import STRUCTURE_NAME_SCOPE
from aa_industry_planner.services.inventory import inventory_by_location, inventory_totals
from aa_industry_planner.services.planning import calculate, create_plan, refresh_plan


def test_shared_storage_capacity_is_reassigned_to_avoid_false_shortages():
    requirements = [
        stock_requirement(5, StockScope((10, 20), 10)),
        stock_requirement(5, StockScope((10,), 10)),
    ]
    assert allocate_location_stock(requirements, {10: 5, 20: 5}) == 10
    assert requirements[0]["allocations"] == {20: 5}
    assert requirements[1]["allocations"] == {10: 5}


def test_stock_already_at_destination_is_used_before_optional_storage():
    requirements = [stock_requirement(5, StockScope((10, 20), 20))]
    assert allocate_location_stock(requirements, {10: 5, 20: 5}) == 5
    assert requirements[0]["allocations"] == {20: 5}


def test_same_material_at_one_factory_cannot_cover_another_activity():
    recipes = {
        1: Recipe(1, 101, "manufacturing", 1, 60, {2: 1, 3: 1}),
        2: Recipe(2, 102, "manufacturing", 1, 60, {4: 5}),
        3: Recipe(3, 103, "reaction", 1, 60, {4: 5}),
    }

    def context(tid, recipe):
        loc = 20 if recipe.activity == "reaction" else 10
        return ProductionContext(structure_id=loc, stock_location_ids=(loc,))

    result = plan(
        1,
        1,
        recipes.get,
        context,
        stock_by_location={4: {10: 100}},
        root_stock_scope=StockScope((10,), 10),
    )
    assert (result.nodes[4].required, result.nodes[4].from_stock, result.nodes[4].missing) == (
        10,
        5,
        5,
    )
    assert {r["consumer_id"]: r["from_stock"] for r in result.nodes[4].inventory_requirements} == {
        2: 5,
        3: 0,
    }
    assert [r["elsewhere"] for r in result.nodes[4].inventory_requirements] == [{}, {10: 5}]


def test_shared_stock_elsewhere_is_reported_at_most_once():
    recipes = {
        1: Recipe(1, 101, "manufacturing", 1, 60, {2: 1, 3: 1}),
        2: Recipe(2, 102, "manufacturing", 1, 60, {4: 5}),
        3: Recipe(3, 103, "manufacturing", 1, 60, {4: 5}),
    }
    result = plan(
        1,
        1,
        recipes.get,
        lambda tid, recipe: ProductionContext(structure_id=10, stock_location_ids=(10,)),
        stock_by_location={4: {10: 2, 20: 6}},
    )
    raw = result.nodes[4]
    assert (raw.from_stock, raw.missing) == (2, 8)
    assert sum(row["elsewhere"].get(20, 0) for row in raw.inventory_requirements) == 6


@pytest.fixture
def sites(user, facility):
    a = facility.facility
    b = Facility.objects.create(
        name="Reaction plant", structure_id=1000000000002, solar_system=a.solar_system
    )
    reactor = FacilityService.objects.create(facility=b, activity="reaction")
    c = Facility.objects.create(
        name="Storage only", structure_id=1000000000003, solar_system=a.solar_system
    )
    outside = Facility.objects.create(
        name="Unconfigured", structure_id=1000000000004, solar_system=a.solar_system
    )
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    FacilityAssignment.objects.create(user=user, activity="reaction", service=reactor)
    return a, b, c, outside


def asset(source, item_id, tid, quantity, location_id, **kwargs):
    return Asset.objects.create(
        source=source,
        item_id=item_id,
        type_id=tid,
        quantity=quantity,
        location_id=location_id,
        **kwargs,
    )


def test_raw_inputs_only_draw_from_the_consuming_activity(user, recipes, source, sites):
    a, b, c, outside = sites
    asset(source, 10, 5, 1000, a.structure_id)
    asset(source, 11, 5, 7, b.structure_id)
    asset(source, 12, 5, 1000, outside.structure_id)
    result, snapshot = calculate(user, 1, 1)
    assert result.nodes[5].from_stock == 7
    assert result.nodes[5].missing == result.nodes[5].required - 7
    assert snapshot["nodes"]["4"]["input_inventory"][0]["allocations"] == [
        {"location_id": b.structure_id, "name": b.name, "quantity": 7, "transfer": False}
    ]
    assert inventory_totals(user)[5] == 1007


def test_inventory_and_plan_show_stock_elsewhere_with_location_and_quantity(
    user, recipes, source, sites, client
):
    a, b, unused, outside = sites
    FacilityService.objects.create(facility=outside, activity="reaction")
    asset(source, 10, 5, 4, a.structure_id)
    asset(source, 11, 5, 2, b.structure_id)
    asset(source, 12, 5, 5, outside.structure_id)
    asset(source, 13, 100, 1, 1000000099999, location_flag="Hangar")
    asset(source, 14, 5, 1, 13, location_flag="Unlocked")
    asset(source, 15, 5, 800, 1000000099998, location_flag="Unlocked")
    asset(source, 16, 5, 25, unused.structure_id)
    asset(source, 17, 5, 10, 60000001)
    asset(source, 18, 60, 1, b.structure_id, location_flag="Hangar")
    asset(source, 19, 100, 1, 18, location_flag="Unlocked")
    asset(source, 20, 5, 25, 19, location_flag="Unlocked")
    asset(source, 21, 5, 20, a.structure_id, location_flag="AssetSafety")

    client.force_login(user)
    inventory = client.get(reverse("aa_industry_planner:inventory"))
    raw = next(row for row in inventory.context["rows"] if row["name"] == recipes[5].name)
    assert raw["quantity"] == 6
    assert raw["elsewhere"] == [(outside.name, 5)]
    assert b"Wrong location" in inventory.content

    production = create_plan(user, 1, 3)
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    shortage = response.context["inputs"][0]
    assert shortage["missing"] == shortage["required"] - 2
    assert sum(pile["quantity"] for pile in shortage["stock_elsewhere"]) == 9
    assert any(pile["name"] == a.name for pile in shortage["stock_elsewhere"])
    assert response.context["multibuy"] == f"Raw input {shortage['missing']}"
    assert b"Stock in wrong location" in response.content

    production.snapshot.pop("stock_elsewhere_scoped")
    production.save(update_fields=["snapshot"])
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    assert not response.context["inputs"][0]["stock_elsewhere"]
    assert b"Refresh this plan to update wrong-location stock" in response.content


def test_intermediate_stock_at_its_production_site_is_not_available_to_a_different_consumer(
    user, recipes, source, sites
):
    a, b, _, _ = sites
    asset(source, 10, 4, 100, b.structure_id)
    asset(source, 11, 4, 2, a.structure_id)
    result, _ = calculate(user, 1, 1)
    assert result.nodes[4].from_stock == 2
    assert result.nodes[4].runs > 0


def test_activity_storage_is_optional_and_cannot_supply_another_activity(
    user, recipes, source, sites
):
    a, b, c, _ = sites
    asset(source, 10, 5, 100, c.structure_id)
    assert calculate(user, 1, 1)[0].nodes[5].from_stock == 0
    ActivityStorageLocation.objects.create(user=user, activity="manufacturing", facility=c)
    assert calculate(user, 1, 1)[0].nodes[5].from_stock == 0
    ActivityStorageLocation.objects.create(user=user, activity="reaction", facility=c)
    result, snapshot = calculate(user, 1, 1)
    assert result.nodes[5].from_stock == result.nodes[5].required
    allocation = snapshot["nodes"]["4"]["input_inventory"][0]["allocations"][0]
    assert allocation["name"] == c.name and allocation["transfer"]


def test_one_storage_pile_is_not_double_counted_across_activities(user, recipes, source, sites):
    a, b, c, _ = sites
    BlueprintActivity.objects.filter(blueprint_item_type_id=103).update(activity="reaction")
    for activity in ["manufacturing", "reaction"]:
        ActivityStorageLocation.objects.create(user=user, activity=activity, facility=c)
    asset(source, 10, 4, 5, c.structure_id)
    result, _ = calculate(user, 1, 1)
    node = result.nodes[4]
    assert node.from_stock == 5
    assert sum(r["allocations"].get(c.structure_id, 0) for r in node.inventory_requirements) == 5
    assert node.missing == node.required - 5


def test_root_output_only_uses_its_assigned_location_and_refresh_changes_the_snapshot(
    user, recipes, source, sites
):
    a, b, _, _ = sites
    row = asset(source, 10, 1, 1, b.structure_id)
    production = create_plan(user, 1, 1)
    assert production.snapshot["nodes"]["1"]["from_stock"] == 0
    row.location_id = a.structure_id
    row.save()
    assert production.snapshot["nodes"]["1"]["from_stock"] == 0
    refreshed, _ = refresh_plan(user, production.pk)
    assert refreshed.snapshot["nodes"]["1"]["from_stock"] == 1
    assert not refreshed.steps.exists()


def test_containers_resolve_to_the_structure_and_cycles_or_missing_parents_are_excluded(
    user, source, sites
):
    a, b, _, _ = sites
    asset(source, 900, 100, 1, a.structure_id)
    asset(source, 901, 100, 1, 900)
    asset(source, 902, 5, 8, 901)
    asset(source, 903, 100, 1, 904)
    asset(source, 904, 100, 1, 903)
    asset(source, 905, 5, 100, 904)
    asset(source, 906, 5, 100, 999)
    stock = inventory_by_location(user)
    assert stock[5] == {a.structure_id: 8}
    # A structure can itself appear in the owner's asset list: stop at its configured ID.
    asset(source, a.structure_id, 999, 1, 30000142)
    assert inventory_by_location(user)[5] == {a.structure_id: 8}


def test_unlocated_manual_stock_is_preserved_but_not_used(user, recipes, sites):
    a, _, _, outside = sites
    source = InventorySource.objects.create(user=user, kind="manual", name="Declared")
    old = ManualStock.objects.create(
        source=source, item_type=recipes[1], quantity=5, location=a.name
    )
    ManualStock.objects.create(
        source=source, item_type=recipes[1], quantity=10, facility=outside, location="Elsewhere"
    )
    assert calculate(user, 1, 1)[0].nodes[1].from_stock == 0
    old.facility = a
    old.save()
    assert calculate(user, 1, 1)[0].nodes[1].from_stock == 1


def test_inactive_or_inaccessible_storage_is_not_used(user, source, sites):
    _, _, c, _ = sites
    ActivityStorageLocation.objects.create(user=user, activity="manufacturing", facility=c)
    asset(source, 10, 5, 10, c.structure_id)
    assert inventory_totals(user)[5] == 10
    c.allowed_groups.add(Group.objects.create(name="Private storage"))
    assert 5 not in inventory_totals(user)
    c.allowed_groups.clear()
    c.active = False
    c.save()
    assert 5 not in inventory_totals(user)


def test_storage_forms_are_user_scoped_allow_storage_without_services_and_are_post_only(
    user, other_user, client, sites
):
    _, _, c, outside = sites
    outside.allowed_groups.add(Group.objects.create(name="Other group"))
    client.force_login(user)
    url = reverse("aa_industry_planner:configuration")
    data = {"form": "storage", "storage-activity": "manufacturing", "storage-facility": c.pk}
    for _ in range(2):
        assert client.post(url, data).status_code == 302
    row = ActivityStorageLocation.objects.get(user=user)
    assert row.facility_id == c.pk and not c.services.exists()
    assert client.post(url, dict(data, **{"storage-facility": outside.pk})).status_code == 200
    remove = reverse("aa_industry_planner:delete_storage_location", args=[row.pk])
    assert client.get(remove).status_code == 405
    client.force_login(other_user)
    assert client.post(remove).status_code == 404
    client.force_login(user)
    assert client.post(remove).status_code == 302
    assert not ActivityStorageLocation.objects.filter(pk=row.pk).exists()


def test_saved_station_is_available_for_selection_before_it_supplies_any_stock(
    user, other_user, client, recipes, source, sites
):
    a, _, _, _ = sites
    station = NPCStation.objects.create(
        pk=60003760, name="Supply station", solar_system=a.solar_system
    )
    asset(source, 10, 5, 10, station.pk)
    client.force_login(user)
    config = reverse("aa_industry_planner:configuration")
    payload = {"form": "save_storage_location", "saved-location-location_id": station.pk}
    assert client.post(config, payload).status_code == 302
    saved = SavedStorageLocation.objects.get(user=user)
    assert saved.name == station.name and not saved.manufacturing and not saved.reaction
    assert 5 not in inventory_by_location(user, include_elsewhere=True)
    assert b"Supply station" in client.get(config).content

    select = reverse("aa_industry_planner:set_activity_storage", args=[station.pk])
    assert client.get(select).status_code == 405
    assert client.post(select, {"manufacturing": "on"}).status_code == 302
    _, snapshot = calculate(user, 1, 1)
    assert snapshot["nodes"]["5"]["stock_elsewhere"][0]["name"] == station.name
    assert snapshot["nodes"]["5"]["from_stock"] == 0
    assert client.post(select, {"reaction": "on"}).status_code == 302
    _, snapshot = calculate(user, 1, 1)
    assert not snapshot["nodes"]["5"]["stock_elsewhere"]
    assert snapshot["nodes"]["5"]["from_stock"] == 10
    assert client.post(select).status_code == 302
    assert 5 not in inventory_by_location(user, include_elsewhere=True)
    assert SavedStorageLocation.objects.filter(pk=saved.pk).exists()

    client.force_login(other_user)
    assert b"Supply station" not in client.get(config).content
    assert client.post(select, {"reaction": "on"}).status_code == 404
    remove = reverse("aa_industry_planner:delete_saved_storage_location", args=[saved.pk])
    assert client.post(remove).status_code == 404


def test_structure_lookup_uses_a_scoped_owned_token_and_allows_manual_fallback(
    user, client, source
):
    scope, _ = Scope.objects.get_or_create(name=STRUCTURE_NAME_SCOPE)
    source.token.scopes.add(scope)
    client.force_login(user)
    config = reverse("aa_industry_planner:configuration")
    payload = {"form": "save_storage_location", "saved-location-location_id": 1000000099999}
    api = Mock()
    api.structure.side_effect = TimeoutError("private response")
    with patch("aa_industry_planner.providers.ESI", return_value=api):
        response = client.post(config, payload)
        assert response.status_code == 200
        assert b"Enter one for this location" in response.content
        assert b"private response" not in response.content
        api.structure.assert_called_once_with(1000000099999, source.token)
        assert not SavedStorageLocation.objects.exists()
        api.reset_mock(side_effect=True)
        api.structure.return_value = {"name": "Personal refinery"}
        assert client.post(config, payload).status_code == 302
        assert SavedStorageLocation.objects.get().name == "Personal refinery"
        api.reset_mock()
        assert (
            client.post(
                config, dict(payload, **{"saved-location-name": "Trade storage"})
            ).status_code
            == 302
        )
        assert SavedStorageLocation.objects.get().name == "Trade storage"
        api.structure.assert_not_called()


def test_private_facility_ids_cannot_be_used_as_personal_storage(user, client, source, sites):
    _, _, _, restricted = sites
    restricted.allowed_groups.add(Group.objects.create(name="Hidden facility"))
    asset(source, 20, 5, 10, restricted.structure_id)
    client.force_login(user)
    payload = {
        "form": "save_storage_location",
        "saved-location-location_id": restricted.structure_id,
        "saved-location-name": "My storage",
    }
    response = client.post(reverse("aa_industry_planner:configuration"), payload)
    assert response.status_code == 200
    assert not SavedStorageLocation.objects.exists()
    assert (
        client.post(
            reverse("aa_industry_planner:set_activity_storage", args=[restricted.structure_id]),
            {"reaction": "on"},
        ).status_code
        == 404
    )
    SavedStorageLocation.objects.create(
        user=user, location_id=restricted.structure_id, name="Old name", reaction=True
    )
    assert 5 not in inventory_by_location(user, include_elsewhere=True)


def test_manual_stock_can_be_assigned_a_structure_without_duplicate_declarations(
    user, recipes, client, sites
):
    a, b, _, _ = sites
    source = InventorySource.objects.create(user=user, kind="manual", name="Declared")
    old = ManualStock.objects.create(
        source=source, item_type=recipes[1], quantity=5, location="Hangar"
    )
    client.force_login(user)
    url = reverse("aa_industry_planner:inventory")
    assert b"No structure assigned" in client.get(url).content
    assert (
        client.post(
            url,
            {
                "stock_id": old.pk,
                "item_type": "1",
                "quantity": 8,
                "facility": a.pk,
                "location": "Hangar",
            },
        ).status_code
        == 302
    )
    assert ManualStock.objects.filter(source=source).count() == 1
    assert inventory_totals(user)[1] == 8
    assert (
        client.post(
            url, {"item_type": "1", "quantity": 9, "facility": b.pk, "location": "Hangar"}
        ).status_code
        == 302
    )
    assert ManualStock.objects.filter(source=source).count() == 2
    assert inventory_totals(user)[1] == 8


def test_unassigned_jobs_use_no_inventory_and_ignoring_inventory_still_produces_everything(
    user, recipes, source, facility
):
    asset(source, 10, 1, 100, facility.facility.structure_id)
    assert calculate(user, 1, 1)[0].nodes[1].from_stock == 0
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    assert calculate(user, 1, 1)[0].nodes[1].from_stock == 1
    assert calculate(user, 1, 1, use_inventory=False)[0].nodes[1].from_stock == 0


def test_bonus_family_location_controls_stock_for_the_job_using_the_material(
    user, recipes, source, sites
):
    from eve_sde.models import ItemCategory, ItemGroup

    a, b, _, _ = sites
    category = ItemCategory.objects.create(id=17, name="Commodity")
    group = ItemGroup.objects.create(id=873, name="Capital Components", category=category)
    recipes[3].group = group
    recipes[3].save()
    service = FacilityService.objects.create(facility=b, activity="manufacturing")
    FacilityAssignment.objects.create(
        user=user, activity="manufacturing", bonus_family=13, service=service
    )
    asset(source, 10, 4, 100, a.structure_id)
    result, _ = calculate(user, 1, 1)
    requirements = {r["consumer_id"]: r for r in result.nodes[4].inventory_requirements}
    assert requirements[2]["from_stock"] == requirements[2]["quantity"]
    assert requirements[3]["from_stock"] == 0
    assert requirements[3]["destination_id"] == b.structure_id


def test_excluded_sources_and_private_manual_rows_do_not_supply_a_plan(
    user, other_user, recipes, source, sites
):
    a, _, _, _ = sites
    asset(source, 10, 1, 100, a.structure_id)
    source.include_in_planning = False
    source.save()
    own = InventorySource.objects.create(user=user, kind="manual", name="Own")
    other = InventorySource.objects.create(user=other_user, kind="manual", name="Other")
    ManualStock.objects.create(source=other, item_type=recipes[1], quantity=100, facility=a)
    assert calculate(user, 1, 1)[0].nodes[1].from_stock == 0
    ManualStock.objects.create(source=own, item_type=recipes[1], quantity=1, facility=a)
    assert calculate(user, 1, 1)[0].nodes[1].from_stock == 1


def test_pending_jobs_at_other_structures_do_not_cover_inventory_targets(
    user, recipes, source, sites
):
    from aa_industry_planner.models import InventoryWatch
    from aa_industry_planner.services.watches import evaluate_watches
    from tests.test_services import make_job

    a, b, _, _ = sites
    watch = InventoryWatch.objects.create(user=user, item_type=recipes[1], minimum=10)
    job = make_job(user, source, runs=7, facility_id=b.structure_id)
    evaluate_watches(user)
    assert watch.task.quantity == 10
    job.facility_id = a.structure_id
    job.save()
    evaluate_watches(user)
    watch.task.refresh_from_db()
    assert watch.task.quantity == 3


def test_saved_plan_renders_input_stock_locations_and_storage_hauling(
    user, recipes, source, sites, client
):
    _, _, c, _ = sites
    ActivityStorageLocation.objects.create(user=user, activity="reaction", facility=c)
    asset(source, 10, 5, 10, c.structure_id)
    production = create_plan(user, 1, 1)
    client.force_login(user)
    content = client.get(reverse("aa_industry_planner:plan", args=[production.pk])).content
    assert (
        b"From allowed stock" in content
        and b"Storage only" in content
        and b"haul to Reaction plant" in content
    )
    assert b"used pooled inventory" not in content


def test_container_resolution_does_not_borrow_parents_from_another_source(user, source, sites):
    a, _, _, _ = sites
    other = InventorySource.objects.create(
        user=user, kind="manual", name="Separate source", owner_id=10
    )
    asset(other, 900, 100, 1, a.structure_id)
    asset(source, 901, 5, 100, 900)
    assert 5 not in inventory_totals(user)


def test_inventory_location_queries_are_batched_instead_of_per_item(user, source, sites):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    a, _, _, _ = sites
    asset(source, 10, 8000, 1, a.structure_id)
    with CaptureQueriesContext(connection) as small:
        inventory_totals(user)
    for tid in range(8001, 8031):
        asset(source, tid, tid, 1, a.structure_id)
    with CaptureQueriesContext(connection) as large:
        totals = inventory_totals(user)
    assert len(totals) == 31
    assert len(large) <= len(small) + 2


@pytest.mark.parametrize("physical_id", [60000001, 1000000000001])
def test_corporate_hangar_divisions_and_nested_containers_resolve_via_office(
    user, source, sites, physical_id
):
    from django.contrib.auth.models import Permission

    user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
    source.kind, source.owner_id = "corporation", 98000001
    source.save()
    a, _, _, outside = sites
    a.structure_id = physical_id
    a.save()
    office_id = 70000001
    asset(source, office_id, 27, 1, physical_id, location_flag="OfficeFolder")
    for division in range(1, 8):
        asset(
            source, 71000000 + division, 5, division, office_id, location_flag=f"CorpSAG{division}"
        )
    asset(source, 72000001, 3293, 1, office_id, location_flag="CorpSAG3")
    asset(source, 72000002, 5, 4, 72000001, location_flag="Unlocked")
    asset(source, 73000001, 27, 1, outside.structure_id, location_flag="OfficeFolder")
    asset(source, 73000002, 5, 100, 73000001, location_flag="CorpSAG1")
    stock = inventory_by_location(user)
    assert stock[5] == {physical_id: 32}
    assert 27 not in stock  # Virtual office records are hierarchy, not usable stock.
