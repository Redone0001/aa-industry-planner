from dataclasses import asdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from eve_sde.models import BlueprintActivityProduct, ItemType

from aa_industry_planner.engine import PlanningError, ProductionContext, Recipe, plan
from aa_industry_planner.models import (
    AdjustedPrice,
    Facility,
    FacilityAssignment,
    ItemPolicy,
    PlanStep,
    ProductionPlan,
    SystemCostIndex,
    UserPreferences,
)

from .inventory import inventory_totals


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


def calculate(user, type_id, quantity, use_inventory=True):
    policies = {p.item_type_id: p for p in ItemPolicy.objects.filter(user=user)}
    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    assignments = {
        (a.activity, a.scope_key): a.service
        for a in FacilityAssignment.objects.filter(
            user=user, service__online=True, service__facility__in=Facility.visible_to(user)
        )
        .select_related("service__facility__solar_system")
        .prefetch_related("service__rigs")
    }
    group_cache = {}

    def context_for(item_id, recipe):
        policy = policies.get(item_id)
        options = {
            "me": policy.material_efficiency if policy else 0,
            "te": policy.time_efficiency if policy else 0,
            "reaction_runs": preferences.reaction_runs,
            "reaction_hours": preferences.reaction_hours,
            "alpha_tax": preferences.alpha_clone_tax,
            "time_multiplier": getattr(preferences, f"{recipe.activity}_time_multiplier"),
        }
        if item_id not in group_cache:
            group_cache[item_id] = ItemType.objects.values_list("group_id", flat=True).get(
                pk=item_id
            )
        service = assignments.get((recipe.activity, group_cache[item_id])) or assignments.get(
            (recipe.activity, 0)
        )
        if service:
            facility = service.facility
            index = SystemCostIndex.objects.filter(
                solar_system=facility.solar_system, activity=recipe.activity
            ).first()
            # Never silently quote an old system index as today's fee.
            if index and index.recorded_at < timezone.now() - timezone.timedelta(hours=6):
                index = None
            material, time = service.material_multiplier, service.time_multiplier
            for rig in service.rigs.all():
                if (
                    rig.applies_to_group_id is None
                    or rig.applies_to_group_id == group_cache[item_id]
                ):
                    material *= rig.material_multiplier
                    time *= rig.time_multiplier
            options.update(
                service_id=service.pk,
                facility=facility.name,
                structure_id=facility.structure_id,
                system=str(facility.solar_system.name),
                index=index.cost_index if index else None,
                index_recorded_at=index.recorded_at.isoformat() if index else None,
                material_multiplier=material,
                time_multiplier=time * options["time_multiplier"],
                cost_multiplier=service.cost_multiplier,
                facility_tax=service.facility_tax,
                scc_surcharge=service.scc_surcharge,
            )
        return ProductionContext(**options)

    prices = {
        p.type_id: p.adjusted_price
        for p in AdjustedPrice.objects.filter(
            updated_at__gte=timezone.now() - timezone.timedelta(days=2)
        )
    }
    result = plan(
        type_id,
        quantity,
        SDERecipes(policies),
        context_for,
        inventory_totals(user) if use_inventory else {},
        [p.item_type_id for p in policies.values() if p.buy],
        prices,
    )
    type_ids = list(result.nodes)
    names = {t.pk: str(t.name) for t in ItemType.objects.filter(pk__in=type_ids)}
    snapshot = {
        "product_id": type_id,
        "quantity": quantity,
        "calculated_at": timezone.now().isoformat(),
        "inventory_used": use_inventory,
        "inventory_scope": "All enabled included sources; this is a plan snapshot, not a stock reservation.",
        "order": result.order,
        "nodes": {
            str(t): dict(json_safe(asdict(n)), name=names.get(t, str(t)))
            for t, n in result.nodes.items()
        },
    }
    return result, snapshot


@transaction.atomic
def create_plan(user, type_id, quantity, name="", use_inventory=True):
    result, snapshot = calculate(user, type_id, quantity, use_inventory)
    production = ProductionPlan.objects.create(
        user=user, item_type_id=type_id, quantity=quantity, name=name, snapshot=snapshot
    )
    position = 0
    for t in result.order:
        node = result.nodes[t]
        if not node.runs:
            continue
        position += 1
        PlanStep.objects.create(
            plan=production,
            position=position,
            item_type_id=t,
            blueprint_type_id=node.recipe.blueprint_id,
            activity=node.recipe.activity,
            runs=node.runs,
            output_quantity=node.output,
            service_id=node.context.service_id,
            details=snapshot["nodes"][str(t)],
        )
    from .jobs import reconcile_jobs

    reconcile_jobs(user)
    # Recheck authorized accounts after saving a build, including jobs already running.
    from aa_industry_planner.tasks import refresh_source

    from .inventory import authorized_sources

    source_ids = list(authorized_sources(user).exclude(kind="manual").values_list("pk", flat=True))
    transaction.on_commit(lambda: [refresh_source.delay(pk) for pk in source_ids])
    return production
