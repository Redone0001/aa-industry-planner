from django.db import transaction
from eve_sde.models import BlueprintActivityProduct

from aa_industry_planner.models import (
    IndustryJob,
    InventoryWatch,
    ItemPolicy,
    ReplenishmentTask,
    UserPreferences,
)

from .inventory import authorized_sources, inventory_totals
from .jobs import job_output
from .notifications import emit
from .planning import create_plan


@transaction.atomic
def evaluate_watches(user):
    UserPreferences.objects.get_or_create(user=user)
    UserPreferences.objects.select_for_update().get(user=user)
    stock = inventory_totals(user)
    policies = {p.item_type_id: p for p in ItemPolicy.objects.filter(user=user)}
    incoming = {}
    for job in IndustryJob.objects.filter(
        user=user, source__in=authorized_sources(user), status__in=["active", "paused", "ready"]
    ):
        incoming[job.product_type_id] = incoming.get(job.product_type_id, 0) + job_output(job)
    for watch in InventoryWatch.objects.filter(user=user).select_related("item_type"):
        shortage = max(0, watch.minimum - stock.get(watch.item_type_id, 0)) if watch.enabled else 0
        policy = policies.get(watch.item_type_id)
        is_input = (
            bool(policy and policy.buy)
            or not BlueprintActivityProduct.objects.filter(
                item_type_id=watch.item_type_id,
                blueprint_activity__activity__in=["manufacturing", "reaction"],
            ).exists()
        )
        existing = ReplenishmentTask.objects.filter(watch=watch).select_related("plan").first()
        covered_by_plan = 0
        if existing and existing.plan and not existing.plan.archived:
            root = existing.plan.steps.filter(item_type=watch.item_type).first()
            if root:
                # Count unallocated planned output; allocated jobs are already in incoming.
                allocated = sum(
                    a.quantity
                    for a in root.allocations.exclude(job__status__in=["cancelled", "reverted"])
                )
                covered_by_plan = max(0, root.output_quantity - allocated)
        quantity = max(0, shortage - incoming.get(watch.item_type_id, 0) - covered_by_plan)
        state = "resolved" if not shortage else "open" if is_input or quantity else "covered"
        task, created = ReplenishmentTask.objects.update_or_create(
            watch=watch,
            defaults={
                "shortage": shortage,
                "quantity": quantity,
                "is_input": is_input,
                "state": state,
            },
        )
        if shortage and (created or existing.state == "resolved"):
            emit(
                user,
                "inventory_low",
                f"watch:{watch.pk}:low:{task.updated_at.isoformat()}",
                "Inventory below target",
                f"{watch.item_type.name}: {stock.get(watch.item_type_id, 0):,} available; target {watch.minimum:,}. {'Buy/import this input.' if is_input else f'{quantity:,} units need production.'}",
            )


@transaction.atomic
def plan_replenishment(user, task_id):
    UserPreferences.objects.get_or_create(user=user)
    UserPreferences.objects.select_for_update().get(user=user)
    evaluate_watches(user)
    task = (
        ReplenishmentTask.objects.select_for_update()
        .select_related("watch")
        .get(pk=task_id, watch__user=user)
    )
    if task.is_input or task.state != "open" or not task.quantity:
        raise ValueError("This item is an input, already covered, or no longer below target.")
    if task.plan and not task.plan.archived:
        raise ValueError(
            "Archive the existing replenishment plan before creating a replacement for the remaining shortage."
        )
    # This is the net shortage: do not subtract current finished goods a second time.
    production = create_plan(
        user, task.watch.item_type_id, task.quantity, "Inventory replenishment", use_inventory=False
    )
    task.plan = production
    task.save(update_fields=["plan"])
    evaluate_watches(user)
    return production
