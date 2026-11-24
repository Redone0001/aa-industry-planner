from decimal import Decimal as D

import pytest

from aa_industry_planner.engine import (
    PlanningError,
    ProductionContext,
    Recipe,
    material_quantity,
    plan,
)


def context(*args):
    return ProductionContext()


def test_shared_demand_aggregates_before_runs_and_stock():
    recipes = {
        1: Recipe(1, 101, "manufacturing", 1, 60, {2: 1, 3: 1}),
        2: Recipe(2, 102, "manufacturing", 1, 60, {4: 3}),
        3: Recipe(3, 103, "manufacturing", 1, 60, {4: 3}),
        4: Recipe(4, 104, "manufacturing", 5, 60, {5: 10}),
    }
    result = plan(1, 1, recipes.get, context, stock={4: 2})
    assert result.nodes[4].required == 6
    assert result.nodes[4].from_stock == 2
    assert result.nodes[4].runs == 1
    assert result.nodes[5].missing == 10
    assert result.order.index(4) < result.order.index(2) < result.order.index(1)


def test_buy_policy_stops_expansion():
    recipes = {
        1: Recipe(1, 101, "manufacturing", 1, 60, {2: 2}),
        2: Recipe(2, 102, "reaction", 1, 60, {3: 5}),
    }
    result = plan(1, 5, recipes.get, context, buy={2})
    assert result.nodes[2].missing == 10
    assert result.nodes[2].recipe is None
    assert 3 not in result.nodes


def test_cycle_rejected_and_buy_breaks_cycle():
    recipes = {
        1: Recipe(1, 101, "manufacturing", 1, 60, {2: 1}),
        2: Recipe(2, 102, "manufacturing", 1, 60, {1: 1}),
    }
    with pytest.raises(PlanningError, match="cycle"):
        plan(1, 1, recipes.get, context)
    assert plan(1, 1, recipes.get, context, buy={2}).nodes[2].missing == 1


def test_material_rounding_and_minimum_per_run():
    assert material_quantity(1, 10, D("0.9")) == 10
    assert material_quantity(10, 1, D("0.90001")) == 9
    assert material_quantity(10, 1, D("0.901")) == 10


def test_reaction_duration_controls_batches_and_does_not_use_blueprint_me():
    recipe = Recipe(1, 101, "reaction", 10, 3600, {2: 100})
    ctx = ProductionContext(reaction_runs=100, reaction_hours=4, me=10, te=20)
    node = plan(1, 50, lambda t: recipe if t == 1 else None, lambda *args: ctx).nodes[1]
    assert node.batches == [4, 4]
    assert node.output == 80
    assert node.materials[2] == 800
    assert node.estimated_seconds == 8 * 3600


def test_batch_rounding_happens_per_job():
    recipe = Recipe(1, 101, "manufacturing", 1, 60, {2: 3}, max_runs=1)
    ctx = ProductionContext(material_multiplier=D("0.9"))
    node = plan(1, 2, lambda t: recipe if t == 1 else None, lambda *args: ctx).nodes[1]
    assert node.batches == [1, 1]
    assert node.materials[2] == 6


def test_installation_fee_uses_unmodified_materials_and_eiv_taxes():
    recipe = Recipe(1, 101, "manufacturing", 1, 60, {2: 100})
    ctx = ProductionContext(
        service_id=1,
        index=D("0.05"),
        cost_multiplier=D("0.95"),
        material_multiplier=D("0.9"),
        me=10,
        facility_tax=D("0.01"),
        scc_surcharge=D("0.04"),
    )
    node = plan(
        1, 2, lambda t: recipe if t == 1 else None, lambda *args: ctx, prices={2: D(10)}
    ).nodes[1]
    assert node.materials[2] == 162
    assert node.estimated_job_cost == D(195)  # EIV=2000, not 1620.


def test_missing_prices_or_index_do_not_produce_zero_cost():
    recipe = Recipe(1, 101, "manufacturing", 1, 60, {2: 10})
    node = plan(1, 1, lambda t: recipe if t == 1 else None, context).nodes[1]
    assert node.estimated_job_cost is None
    assert len(node.warnings) == 3


@pytest.mark.parametrize("quantity", [0, -1, 10**12 + 1])
def test_invalid_quantities_rejected(quantity):
    with pytest.raises(PlanningError):
        plan(1, quantity, lambda t: None, context)


def test_existing_output_suppresses_all_downstream_demand():
    recipe = Recipe(1, 101, "manufacturing", 1, 60, {2: 50})
    result = plan(1, 1, lambda t: recipe if t == 1 else None, context, stock={1: 1})
    assert result.nodes[1].runs == 0
    assert result.nodes[2].required == 0
