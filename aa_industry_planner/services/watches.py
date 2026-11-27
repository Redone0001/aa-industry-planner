from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from eve_sde.models import BlueprintActivityProduct

from aa_industry_planner.models import (
    IndustryJob,
    InventoryWatch,
    ItemPolicy,
    ReplenishmentTask,
    UserPreferences,
)

from .inventory import authorized_sources, inventory_totals
from .jobs import job_output, reconcile_jobs
from .locations import InventoryLocations
from .notifications import emit
from .planning import create_plan_for_targets


def plannable_tasks(tasks):
    return tasks.filter(
        Q(plan__isnull=True) | Q(plan__archived=True),
        watch__enabled=True,
        state="open",
        is_input=False,
        quantity__gt=0,
    )


@transaction.atomic
def evaluate_watches(user):
    UserPreferences.objects.get_or_create(user=user)
    UserPreferences.objects.select_for_update().get(user=user)
    locations = InventoryLocations(user)
    stock = inventory_totals(user, locations)
    policies = {p.item_type_id: p for p in ItemPolicy.objects.filter(user=user)}
    incoming = defaultdict(dict)
    for job in IndustryJob.objects.filter(
        user=user, source__in=authorized_sources(user), status__in=["active", "paused", "ready"]
    ):
        if (
            not job.product_type_id
            or job.facility_id not in locations.item_scope(job.product_type_id).location_ids
        ):
            continue
        incoming[job.product_type_id][job.pk] = job_output(job)
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
        existing = (
            ReplenishmentTask.objects.filter(watch=watch)
            .select_related("plan")
            .defer("plan__snapshot")
            .first()
        )
        incoming_jobs = incoming[watch.item_type_id]
        coverage = sum(incoming_jobs.values())
        if existing and existing.plan and not existing.plan.archived:
            root = existing.plan.steps.filter(item_type=watch.item_type).first()
            if (
                root
                and root.details.get("context", {}).get("structure_id")
                in locations.item_scope(watch.item_type_id).location_ids
            ):
                allocations = list(
                    root.allocations.exclude(job__status__in=["cancelled", "reverted"])
                    .values_list("job_id", "quantity")
                )
                allocated = sum(amount for _, amount in allocations)
                linked_incoming = sum(
                    amount for job_id, amount in allocations if job_id in incoming_jobs
                )
                unallocated = max(0, root.output_quantity - allocated)
                # Shared output fills the plan's downstream needs before replenishing stock.
                used_in_plan = sum(
                    row["quantity"] - row["from_stock"]
                    for row in root.details.get("inventory_requirements", ())
                    if row["consumer_id"] is not None
                )
                still_needed = max(0, used_in_plan - (allocated - linked_incoming))
                coverage += max(0, unallocated + linked_incoming - still_needed) - linked_incoming
        quantity = max(0, shortage - coverage)
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
def plan_replenishment(user, task_id=None):
    # Evaluation locks the user's preferences for the rest of this transaction.
    evaluate_watches(user)
    tasks = ReplenishmentTask.objects.filter(watch__user=user)
    if task_id is None:
        selected = list(
            plannable_tasks(tasks).order_by("pk").values_list("pk", "watch__item_type_id", "quantity")
        )
        if not selected:
            raise ValueError("There are no production shortages available to plan.")
    else:
        task = tasks.select_related("watch", "plan").get(pk=task_id)
        if task.is_input or task.state != "open" or not task.quantity:
            raise ValueError("This item is an input, already covered, or no longer below target.")
        if task.plan and not task.plan.archived:
            raise ValueError(
                "Archive the existing replenishment plan before creating a replacement for the remaining shortage."
            )
        selected = [(task.pk, task.watch.item_type_id, task.quantity)]
    # The net shortage already accounts for existing stock and incoming jobs.
    production = create_plan_for_targets(
        user,
        {type_id: quantity for _, type_id, quantity in selected},
        "Inventory replenishment",
        use_inventory=False,
        match_jobs=False,
    )
    tasks.filter(pk__in=[pk for pk, _, _ in selected]).update(plan=production)
    reconcile_jobs(user)
    evaluate_watches(user)
    return production
