from dataclasses import asdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from eve_sde.models import BlueprintActivityProduct, ItemType

from aa_industry_planner.engine import PlanningError, ProductionContext, Recipe, plan_targets
from aa_industry_planner.industry import (
    FitError,
    catalog,
)
from aa_industry_planner.models import (
    AdjustedPrice,
    ItemPolicy,
    JobAllocation,
    PlanBlueprintOverride,
    PlanStep,
    PlanTarget,
    ProductionPlan,
    SystemCostIndex,
    UserPreferences,
)

from .costs import refresh_market_prices
from .inventory import inventory_by_location
from .locations import InventoryLocations


class SDERecipes:
    def __init__(self, policies):
        self.policies = policies
        self.cache = {}

    def __call__(self, type_id):
        if type_id in self.cache:
            return self.cache[type_id]
        products = (
            BlueprintActivityProduct.objects.filter(
                item_type_id=type_id,
                blueprint_activity__activity__in=["manufacturing", "reaction"],
            )
            .select_related("blueprint_activity")
            .prefetch_related("blueprint_activity__materials")
        )
        policy = self.policies.get(type_id)
        if policy and policy.blueprint_type_id:
            products = products.filter(
                blueprint_activity__blueprint_item_type_id=policy.blueprint_type_id
            )
        candidates = list(products.order_by("blueprint_activity__blueprint_item_type_id"))
        if len(candidates) > 1:
            raise PlanningError(
                f"Type {type_id} has multiple recipes. Choose a blueprint in item policies."
            )
        recipe = None
        if candidates:
            product = candidates[0]
            activity = product.blueprint_activity
            materials = list(activity.materials.all())
            if not activity.blueprint_item_type_id or any(not m.item_type_id for m in materials):
                raise PlanningError(
                    f"SDE recipe for {type_id} has unresolved types. Reload the SDE."
                )
            recipe = Recipe(
                type_id,
                activity.blueprint_item_type_id,
                activity.activity,
                product.quantity or 0,
                activity.time or 0,
                {m.item_type_id: m.quantity or 0 for m in materials},
                activity.max_production_limit or 100000,
            )
        self.cache[type_id] = recipe
        return recipe


def json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def validate_blueprints(default_me, default_te, overrides):
    if default_me is None or default_te is None:
        raise PlanningError("Set the default blueprint ME and TE for this plan.")
    for me, te in [(default_me, default_te), *overrides.values()]:
        if me is not None and (not isinstance(me, int) or not 0 <= me <= 10):
            raise PlanningError("Blueprint ME must be between 0 and 10.")
        if te is not None and (not isinstance(te, int) or not 0 <= te <= 20):
            raise PlanningError("Blueprint TE must be between 0 and 20.")


def calculate(
    user,
    type_id,
    quantity,
    use_inventory=True,
    **options,
):
    return calculate_targets(user, {type_id: quantity}, use_inventory, **options)


def calculate_targets(
    user,
    targets,
    use_inventory=True,
    *,
    default_me=10,
    default_te=20,
    blueprint_overrides=None,
    buy_item_ids=(),
):
    blueprint_overrides = blueprint_overrides or {}
    validate_blueprints(default_me, default_te, blueprint_overrides)
    policies = {p.item_type_id: p for p in ItemPolicy.objects.filter(user=user)}
    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    locations = InventoryLocations(user, policies)
    contexts = {}

    def context_for(item_id, recipe):
        if item_id in contexts:
            return contexts[item_id]
        me, te = blueprint_overrides.get(item_id, (None, None))
        options = {
            "me": (default_me if me is None else me) if recipe.activity == "manufacturing" else 0,
            "te": (default_te if te is None else te) if recipe.activity == "manufacturing" else 0,
            "reaction_runs": preferences.reaction_runs,
            "reaction_hours": preferences.reaction_hours,
            "alpha_tax": preferences.alpha_clone_tax,
            "time_multiplier": getattr(preferences, f"{recipe.activity}_time_multiplier"),
        }
        service = locations.service_for(item_id, recipe.activity)
        group_id, category_id = locations.groups[item_id]
        options["stock_location_ids"] = locations.scope(item_id, recipe.activity).location_ids
        if service:
            facility = service.facility
            index = SystemCostIndex.objects.filter(
                solar_system=facility.solar_system, activity=recipe.activity
            ).first()
            # Never silently quote an old system index as today's fee.
            if index and index.recorded_at < timezone.now() - timezone.timedelta(hours=6):
                index = None
            material, time, cost = (
                service.material_multiplier,
                service.time_multiplier,
                service.cost_multiplier,
            )
            if facility.structure_type_id:
                try:
                    fit = facility.industry_fit()
                    bonuses = fit.multipliers(recipe.activity, group_id, category_id)
                except FitError as exc:
                    raise PlanningError(f"{facility.name}: {exc}") from exc
                material, time, cost = bonuses.material, bonuses.time, bonuses.cost
                options["bonus_source"] = f"CCP SDE {catalog()['release']}; declared structure fit"
                options["facility_fit"] = {
                    "hull_type_id": fit.hull_id,
                    "modules": [{"type_id": tid, "online": online} for tid, online in fit.modules],
                    "rig_type_ids": list(fit.rigs),
                    "security_band": fit.band,
                    "supercapital_ready": fit.supercapital_ready,
                }
            else:
                options["bonus_source"] = "Manual facility configuration"
                for rig in service.rigs.all():
                    if rig.applies_to_group_id is None or rig.applies_to_group_id == group_id:
                        material *= rig.material_multiplier
                        time *= rig.time_multiplier
            options.update(
                service_id=service.pk,
                facility=facility.name,
                structure_id=facility.structure_id,
                system=str(facility.solar_system.name),
                index=index.cost_index if index else None,
                index_recorded_at=index.recorded_at.isoformat() if index else None,
                index_retrieved_at=index.retrieved_at.isoformat()
                if index and index.retrieved_at
                else None,
                index_source_modified_at=index.source_modified_at.isoformat()
                if index and index.source_modified_at
                else None,
                index_cache_expires_at=index.cache_expires_at.isoformat()
                if index and index.cache_expires_at
                else None,
                material_multiplier=material,
                time_multiplier=time * options["time_multiplier"],
                cost_multiplier=cost,
                facility_tax=service.facility_tax,
                scc_surcharge=service.scc_surcharge,
            )
        contexts[item_id] = ProductionContext(**options)
        return contexts[item_id]

    price_rows = {
        p.type_id: p
        for p in AdjustedPrice.objects.filter(
            updated_at__gte=timezone.now() - timezone.timedelta(days=2)
        )
    }
    prices = {tid: p.adjusted_price for tid, p in price_rows.items()}
    recipes = SDERecipes(policies)
    stock = inventory_by_location(user, locations, include_elsewhere=True) if use_inventory else {}
    locations.prime_items(targets)
    target_scopes = {tid: locations.item_scope(tid) for tid in targets}
    policy_buys = {p.item_type_id for p in policies.values() if p.buy}
    buy_item_ids = set(buy_item_ids)

    def calculate_chain(buy):
        return plan_targets(
            targets,
            recipes,
            context_for,
            buy=buy,
            prices=prices,
            stock_by_location=stock,
            target_scopes=target_scopes,
        )

    result = calculate_chain(policy_buys | buy_item_ids)
    baseline = calculate_chain(policy_buys).nodes if buy_item_ids - policy_buys else {}
    type_ids = result.nodes.keys() | baseline.keys() | buy_item_ids
    names = dict(ItemType.objects.filter(pk__in=type_ids).values_list("id", "name"))
    type_id, quantity = next(iter(targets.items()))
    snapshot = {
        "product_id": type_id,
        "quantity": quantity,
        "targets": [{"type_id": tid, "quantity": amount} for tid, amount in targets.items()],
        "calculated_at": timezone.now().isoformat(),
        "inventory_used": use_inventory,
        "stock_elsewhere_scoped": True,
        "inventory_scope": "Stock counts toward production at each consuming job’s assigned structure and activity storage. Wrong-location stock shows possible transfers. This snapshot is not a stock reservation."
        if use_inventory
        else "Inventory is ignored for this calculation.",
        "blueprint_settings": {
            "default_me": default_me,
            "default_te": default_te,
            "overrides": {
                str(tid): {"me": values[0], "te": values[1]}
                for tid, values in blueprint_overrides.items()
            },
        },
        "order": result.order,
        "nodes": {
            str(t): dict(json_safe(asdict(n)), name=names.get(t, str(t)))
            for t, n in result.nodes.items()
        },
    }
    if buy_item_ids:
        snapshot["purchase_choices"] = {str(t): names.get(t, str(t)) for t in sorted(buy_item_ids)}
        snapshot["before_purchases"] = {
            str(t): dict(
                name=names.get(t, str(t)),
                required=node.required,
                runs=node.runs,
                activity=node.recipe.activity if node.recipe else "",
                materials=json_safe(node.materials),
            )
            for t, node in baseline.items()
            if node.required
        }
    for node in snapshot["nodes"].values():
        node["input_inventory"] = []
        node["stock_locations"] = []
        node["stock_elsewhere"] = []
        if node.get("cost_details"):
            used = node["cost_details"]["materials"]
            times = [price_rows[m["type_id"]].updated_at for m in used]
            node["cost_details"]["prices_retrieved_at"] = min(times).isoformat() if times else None
            for field in ["source_modified_at", "cache_expires_at"]:
                dates = [getattr(price_rows[m["type_id"]], field) for m in used]
                node["cost_details"][f"prices_{field}"] = (
                    min(dates).isoformat() if dates and all(dates) else None
                )
            for material in used:
                material["name"] = names.get(material["type_id"], str(material["type_id"]))
    for node in snapshot["nodes"].values():
        for requirement in node["inventory_requirements"]:
            allocations = [
                {
                    "location_id": int(loc),
                    "name": locations.names.get(int(loc), str(loc)),
                    "quantity": amount,
                    "transfer": bool(
                        requirement["destination_id"] and int(loc) != requirement["destination_id"]
                    ),
                }
                for loc, amount in requirement["allocations"].items()
            ]
            elsewhere = [
                {
                    "location_id": int(loc),
                    "name": locations.transfer_names[int(loc)],
                    "quantity": amount,
                }
                for loc, amount in requirement.get("elsewhere", {}).items()
            ]
            node["stock_locations"].extend(allocations)
            node["stock_elsewhere"].extend(elsewhere)
            parent = snapshot["nodes"].get(str(requirement["consumer_id"]))
            if parent is not None:
                parent["input_inventory"].append(
                    dict(
                        type_id=node["type_id"],
                        name=node["name"],
                        required=requirement["quantity"],
                        from_stock=requirement["from_stock"],
                        missing=requirement["quantity"] - requirement["from_stock"],
                        allocations=allocations,
                        stock_elsewhere=elsewhere,
                    )
                )
    snapshot["inventory_locations"] = [
        dict(location_id=loc, name=name) for loc, name in sorted(locations.names.items())
    ]
    return result, snapshot


def create_plan(
    user,
    type_id,
    quantity,
    name="",
    use_inventory=True,
    **options,
):
    return create_plan_for_targets(user, {type_id: quantity}, name, use_inventory, **options)


@transaction.atomic
def create_plan_for_targets(
    user,
    targets,
    name="",
    use_inventory=True,
    *,
    default_me=10,
    default_te=20,
    blueprint_overrides=None,
    match_jobs=True,
):
    result, snapshot = calculate_targets(
        user,
        targets,
        use_inventory,
        default_me=default_me,
        default_te=default_te,
        blueprint_overrides=blueprint_overrides,
    )
    (type_id, quantity), *additional = targets.items()
    production = ProductionPlan.objects.create(
        user=user,
        item_type_id=type_id,
        quantity=quantity,
        name=name,
        snapshot=snapshot,
        use_inventory=use_inventory,
        default_material_efficiency=default_me,
        default_time_efficiency=default_te,
    )
    PlanTarget.objects.bulk_create(
        PlanTarget(plan=production, item_type_id=tid, quantity=amount) for tid, amount in additional
    )
    save_blueprint_overrides(production, blueprint_overrides or {})
    write_steps(production, result, snapshot)
    if match_jobs:
        from .jobs import reconcile_jobs

        reconcile_jobs(user)
    queue_source_refresh(user)
    return production


def save_blueprint_overrides(production, overrides):
    for tid, (me, te) in overrides.items():
        if me is None and te is None:
            production.blueprint_overrides.filter(item_type_id=tid).delete()
        else:
            PlanBlueprintOverride.objects.update_or_create(
                plan=production,
                item_type_id=tid,
                defaults={"material_efficiency": me, "time_efficiency": te},
            )


def write_steps(production, result, snapshot):
    existing = {s.item_type_id: s for s in production.steps.all()}
    produced = []
    position = 0
    for t in result.order:
        node = result.nodes[t]
        if not node.runs:
            continue
        position += 1
        produced.append(t)
        fields = dict(
            position=position,
            blueprint_type_id=node.recipe.blueprint_id,
            activity=node.recipe.activity,
            runs=node.runs,
            output_quantity=node.output,
            service_id=node.context.service_id,
            details=snapshot["nodes"][str(t)],
        )
        if t in existing:
            step = existing[t]
            for key, value in fields.items():
                setattr(step, key, value)
            step.save(update_fields=list(fields))
        else:
            PlanStep.objects.create(plan=production, item_type_id=t, **fields)
    obsolete = production.steps.exclude(item_type_id__in=produced)
    detached = JobAllocation.objects.filter(step__in=obsolete).count()
    obsolete.delete()
    return detached


@transaction.atomic
def refresh_plan(
    user,
    plan_id,
    *,
    default_me=None,
    default_te=None,
    use_inventory=None,
    blueprint_overrides=None,
    purchase_change=None,
):
    """Recalculate in place, retaining compatible links and all ESI job records."""
    production = ProductionPlan.objects.select_for_update().get(
        pk=plan_id, user=user, archived=False
    )
    default_me = production.default_material_efficiency if default_me is None else default_me
    default_te = production.default_time_efficiency if default_te is None else default_te
    use_inventory = production.use_inventory if use_inventory is None else use_inventory
    overrides = {
        o.item_type_id: (o.material_efficiency, o.time_efficiency)
        for o in production.blueprint_overrides.all()
    }
    overrides.update(blueprint_overrides or {})
    buy_item_ids = set(map(int, production.snapshot.get("purchase_choices", {})))
    if purchase_change is not None:
        item_id, buy = purchase_change
        if item_id not in buy_item_ids and (
            not buy or not production.snapshot.get("nodes", {}).get(str(item_id), {}).get("runs")
        ):
            raise ProductionPlan.DoesNotExist
        if buy:
            buy_item_ids.add(item_id)
        else:
            buy_item_ids.remove(item_id)
    targets = {production.item_type_id: production.quantity}
    targets.update(production.additional_targets.values_list("item_type_id", "quantity"))
    result, snapshot = calculate_targets(
        user,
        targets,
        use_inventory,
        default_me=default_me,
        default_te=default_te,
        blueprint_overrides=overrides,
        buy_item_ids=buy_item_ids,
    )
    if "market_prices" in production.snapshot:
        snapshot["market_prices"] = production.snapshot["market_prices"]
    production.default_material_efficiency = default_me
    production.default_time_efficiency = default_te
    production.use_inventory = use_inventory
    production.snapshot = snapshot
    production.refreshed_at = timezone.now()
    production.save(
        update_fields=[
            "default_material_efficiency",
            "default_time_efficiency",
            "use_inventory",
            "snapshot",
            "refreshed_at",
        ]
    )
    save_blueprint_overrides(production, overrides)
    detached = write_steps(production, result, snapshot)

    from .jobs import compatible, reconcile_jobs

    remaining = {}
    for allocation in (
        JobAllocation.objects.filter(step__plan=production)
        .select_related("job", "step__plan", "step__service__facility")
        .order_by("job__start_date", "job__job_id")
    ):
        step = allocation.step
        available = remaining.setdefault(step.pk, step.output_quantity)
        if not compatible(allocation.job, step) or not available:
            allocation.delete()
            detached += 1
            continue
        quantity = min(allocation.quantity, available)
        if quantity != allocation.quantity:
            allocation.quantity = quantity
            allocation.save(update_fields=["quantity"])
        remaining[step.pk] -= quantity

    reconcile_jobs(user)
    from .watches import evaluate_watches

    evaluate_watches(user)
    queue_source_refresh(user)
    if purchase_change is None:
        transaction.on_commit(lambda: refresh_market_prices(production))
    return production, detached


def queue_source_refresh(user):
    # Recheck authorized accounts after saving a build, including jobs already running.
    from aa_industry_planner.tasks import refresh_source

    from .inventory import authorized_sources

    source_ids = list(authorized_sources(user).exclude(kind="manual").values_list("pk", flat=True))
    transaction.on_commit(lambda: [refresh_source.delay(pk) for pk in source_ids])
