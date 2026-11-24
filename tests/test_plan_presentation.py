from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from aa_industry_planner.models import InventorySource
from aa_industry_planner.services.inventory import source_overview
from aa_industry_planner.services.planning import calculate, create_plan
from aa_industry_planner.services.presentation import plan_views
from aa_industry_planner.services.sync import sync_source


def test_graph_merges_shared_materials_without_losing_connection_quantities(user, recipes):
    _, snapshot = calculate(user, 1, 10, use_inventory=False)
    tree, graph = plan_views(snapshot, {})
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert len(nodes) == len(graph["nodes"]) == 5
    assert nodes[4]["shared"]
    parents = [edge for edge in graph["edges"] if edge["target"] == 4]
    assert {edge["source"] for edge in parents} == {2, 3}
    assert sum(edge["quantity"] for edge in parents) == nodes[4]["required"]
    assert tree["item"]["id"] == 1
    references = []

    def collect(branch):
        if branch["reference"]:
            references.append(branch)
        for child in branch["children"]:
            collect(child)

    collect(tree)
    assert len(references) == 1
    assert references[0]["item"]["id"] == 4
    assert references[0]["edge_quantity"] in {edge["quantity"] for edge in parents}


def test_graph_places_dependencies_to_the_right_and_cards_do_not_overlap(user, recipes):
    _, snapshot = calculate(user, 1, 10, use_inventory=False)
    _, graph = plan_views(snapshot, {1: {"position": 4, "progress": 50}})
    nodes = {node["id"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        parent, child = nodes[edge["source"]], nodes[edge["target"]]
        assert child["x"] > parent["x"] + parent["width"]
    for first in nodes.values():
        for second in nodes.values():
            if first["id"] != second["id"] and first["depth"] == second["depth"]:
                assert abs(first["y"] - second["y"]) >= first["height"]
    assert (nodes[1]["step"], nodes[1]["progress"]) == (4, 50)
    assert nodes[4]["kind"] == "reaction"
    assert nodes[5]["kind"] == "buy"


def test_fully_stocked_product_has_one_stock_node_without_unused_dependencies(
    user, recipes, assigned_facility
):
    from aa_industry_planner.models import ManualStock

    source = InventorySource.objects.create(user=user, kind="manual", name="Stock")
    ManualStock.objects.create(
        source=source, item_type=recipes[1], quantity=10, facility=assigned_facility
    )
    _, snapshot = calculate(user, 1, 10)
    _, graph = plan_views(snapshot, {})
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["kind"] == "stock"
    assert graph["nodes"][0]["stock"] == 10
    assert graph["edges"] == []


def test_overview_renders_safe_graph_payload_and_readable_tree(user, client, recipes):
    recipes[1].name = '</script><script>alert("x")</script>'
    recipes[1].save()
    production = create_plan(user, 1, 10)
    client.force_login(user)
    content = client.get(reverse("aa_industry_planner:plan", args=[production.pk])).content
    assert b'id="industry-dependency-graph"' in content
    assert b"plan_graph.js" in content
    assert b"This branch" in content
    assert b"Plan total" in content
    assert b"data-tree-reference" in content
    assert b'data-plan-view="graph"' in content
    assert b'data-plan-view="tree"' in content
    assert b'<script>alert("x")</script>' not in content


def test_inventory_and_jobs_report_zero_connected_characters_even_with_aa_ownership_and_stock(
    user, client
):
    InventorySource.objects.create(user=user, kind="manual", name="My stock")
    client.force_login(user)
    for name in ["inventory", "jobs"]:
        content = client.get(reverse(f"aa_industry_planner:{name}")).content
        assert b"0 characters connected" in content
        assert b"AA characters are not connected to this planner automatically" in content
        assert b"Connect character" in content
    assert source_overview(user)["connected_characters"] == 0


def test_connection_counts_are_scoped_to_the_current_user(user, other_user, source):
    assert source_overview(user)["connected_characters"] == 1
    assert source_overview(other_user)["connected_characters"] == 0
    source.enabled = False
    source.save()
    summary = source_overview(user)
    assert summary["connected_characters"] == 0
    assert summary["sources"][0].needs_reconnection


@pytest.mark.parametrize(
    ("activity", "label"),
    [(3, "TE research"), (4, "ME research"), (5, "Copying"), (8, "Invention")],
)
def test_science_jobs_import_and_display_blueprint_name_without_product_id(
    user, source, client, recipes, activity, label
):
    recipes[101].name = "Mantis I Blueprint"
    recipes[101].save()
    now = timezone.now()
    api = Mock()
    api.assets.return_value = []
    api.jobs.return_value = [
        {
            "job_id": 54321,
            "installer_id": source.owner_id,
            "activity_id": activity,
            "blueprint_type_id": 101,
            "facility_id": 1000000000001,
            "runs": 9,
            "status": "active",
            "start_date": now.isoformat(),
            "end_date": (now + timezone.timedelta(days=6)).isoformat(),
        }
    ]
    with (
        patch("aa_industry_planner.services.sync.validate_source"),
        patch("aa_industry_planner.tasks.resolve_entity_names.delay"),
    ):
        sync_source(source, api)
    source.include_in_planning = False
    source.save()
    client.force_login(user)
    content = client.get(reverse("aa_industry_planner:jobs")).content
    assert b"1 character connected" in content
    assert b"Mantis I Blueprint" in content
    assert label.encode() in content
    assert b"54321" in content


def test_jobs_source_status_explains_first_import_and_refresh_stays_on_jobs(user, source, client):
    client.force_login(user)
    page = client.get(reverse("aa_industry_planner:jobs"))
    assert b"Never synced" in page.content
    assert b"Waiting for the first import" in page.content
    with patch("aa_industry_planner.tasks.refresh_source.delay") as queued:
        response = client.post(
            reverse("aa_industry_planner:source_action", args=[source.pk]),
            {"action": "refresh", "return_to": "jobs"},
        )
    assert response.status_code == 302
    assert response.url == reverse("aa_industry_planner:jobs")
    queued.assert_called_once_with(source.pk)
