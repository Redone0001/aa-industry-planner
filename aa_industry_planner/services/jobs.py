from django.db import transaction
from django.db.models import Sum

from aa_industry_planner.models import ACTIVITY_IDS, IndustryJob, JobAllocation, PlanStep

from .inventory import authorized_sources


def job_output(job):
    from eve_sde.models import BlueprintActivityProduct

    activity = {v: k for k, v in ACTIVITY_IDS.items()}.get(job.activity_id)
    if not activity or not job.product_type_id:
        return 0
    product = BlueprintActivityProduct.objects.filter(
        blueprint_activity__blueprint_item_type_id=job.blueprint_type_id,
        blueprint_activity__activity=activity,
        item_type_id=job.product_type_id,
    ).first()
    runs = job.successful_runs if job.successful_runs is not None else job.runs
    return runs * (product.quantity or 0) if product else 0


def compatible(job, step):
    expected_facility = step.details.get("context", {}).get("structure_id")
    if expected_facility is None and step.service_id:
        expected_facility = step.service.facility.structure_id
    return (
        job.user_id == step.plan.user_id
        and job.product_type_id == step.item_type_id
        and job.blueprint_type_id == step.blueprint_type_id
        and job.activity_id == ACTIVITY_IDS[step.activity]
        and (expected_facility is None or job.facility_id == expected_facility)
        and job.status not in {"cancelled", "reverted"}
    )


@transaction.atomic
def allocate_job(user, job_id, step_id):
    job = IndustryJob.objects.select_for_update().get(
        pk=job_id, user=user, source__in=authorized_sources(user)
    )
    step = (
        PlanStep.objects.select_for_update()
        .select_related("plan", "service__facility")
        .get(pk=step_id, plan__user=user, plan__archived=False)
    )
    if not compatible(job, step):
        raise ValueError("Job product, blueprint, activity and facility must match the plan step.")
    if JobAllocation.objects.filter(job=job).exclude(step=step).exists():
        raise ValueError("This job is already assigned to another step.")
    assigned = (
        step.allocations.exclude(job=job)
        .exclude(job__status__in=["cancelled", "reverted"])
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    quantity = min(job_output(job), max(0, step.output_quantity - assigned))
    if not quantity:
        raise ValueError("This step is already covered or the job has no known output.")
    JobAllocation.objects.update_or_create(job=job, defaults={"step": step, "quantity": quantity})
    update_progress(user)


def update_progress(user):
    sources = authorized_sources(user)
    for step in PlanStep.objects.filter(plan__user=user, plan__archived=False):
        quantity = (
            step.allocations.filter(
                job__source__in=sources, job__status__in=["ready", "delivered"]
            ).aggregate(total=Sum("quantity"))["total"]
            or 0
        )
        # cancelled/reverted jobs free their allocation for a replacement.
        step.allocations.filter(job__status__in=["cancelled", "reverted"]).delete()
        PlanStep.objects.filter(pk=step.pk).update(
            completed_quantity=min(quantity, step.output_quantity)
        )


def reconcile_jobs(user):
    from allianceauth.authentication.models import CharacterOwnership

    owned = CharacterOwnership.objects.filter(user=user).values_list(
        "character__character_id", flat=True
    )
    candidates = IndustryJob.objects.filter(
        user=user,
        source__in=authorized_sources(user),
        installer_id__in=owned,
        status__in=["active", "paused", "ready", "delivered"],
        allocation__isnull=True,
    ).order_by("start_date", "job_id")
    steps = list(
        PlanStep.objects.filter(plan__user=user, plan__archived=False)
        .select_related("plan", "service__facility")
        .order_by("plan__created_at", "position")
    )
    for job in candidates:
        for step in steps:
            # Existing running jobs may cover new plans; old finished history may not.
            if job.start_date < step.plan.created_at and job.status in {"ready", "delivered"}:
                continue
            if compatible(job, step):
                try:
                    allocate_job(user, job.pk, step.pk)
                except ValueError:
                    continue
                break
    update_progress(user)
