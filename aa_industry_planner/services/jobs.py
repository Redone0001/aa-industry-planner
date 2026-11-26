from collections import defaultdict

from django.db import transaction
from django.db.models import Q, Sum

from aa_industry_planner.models import JOB_ACTIVITIES, IndustryJob, JobAllocation, PlanStep

from .inventory import authorized_sources


def active_past_finish(now):
    return Q(status="active", end_date__lte=now)


def installer_names(ids):
    from allianceauth.eveonline.models import EveCharacter
    from eveuniverse.models import EveEntity

    ids = set(ids)
    names = dict(EveEntity.objects.filter(pk__in=ids).exclude(name="").values_list("pk", "name"))
    names.update(
        EveCharacter.objects.filter(character_id__in=ids - names.keys())
        .exclude(character_name="")
        .values_list("character_id", "character_name")
    )
    return {installer_id: names.get(installer_id, str(installer_id)) for installer_id in ids}


def job_output(job, products=None):
    from eve_sde.models import BlueprintActivityProduct

    activity = JOB_ACTIVITIES.get(job.activity_id)
    if not activity or not job.product_type_id:
        return 0
    if products is None:
        quantity = (
            BlueprintActivityProduct.objects.filter(
                blueprint_activity__blueprint_item_type_id=job.blueprint_type_id,
                blueprint_activity__activity=activity,
                item_type_id=job.product_type_id,
            )
            .values_list("quantity", flat=True)
            .first()
        )
    else:
        quantity = products.get((job.blueprint_type_id, activity, job.product_type_id))
    # Manufacturing and reactions are deterministic. successful_runs describes invention,
    # and can be absent or zero while a deterministic job is still active.
    return job.runs * (quantity or 0)


def job_outputs(jobs):
    from eve_sde.models import BlueprintActivityProduct

    products = {
        (bp, activity, item): quantity
        for bp, activity, item, quantity in BlueprintActivityProduct.objects.filter(
            blueprint_activity__blueprint_item_type_id__in={j.blueprint_type_id for j in jobs}
        ).values_list(
            "blueprint_activity__blueprint_item_type_id",
            "blueprint_activity__activity",
            "item_type_id",
            "quantity",
        )
    }
    return {job.pk: job_output(job, products) for job in jobs}


def compatible(job, step):
    expected_facility = step.details.get("context", {}).get("structure_id")
    if expected_facility is None and step.service_id:
        expected_facility = step.service.facility.structure_id
    return (
        job.user_id == step.plan.user_id
        and job.product_type_id == step.item_type_id
        and job.blueprint_type_id == step.blueprint_type_id
        and JOB_ACTIVITIES.get(job.activity_id) == step.activity
        and (expected_facility is None or job.facility_id == expected_facility)
        and job.status not in {"cancelled", "reverted"}
    )


@transaction.atomic
def allocate_job(user, job_id, step_id, *, refresh_progress=True, reassign=False):
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
    previous_step_id = (
        JobAllocation.objects.filter(job=job).values_list("step_id", flat=True).first()
    )
    if previous_step_id and previous_step_id != step.pk and not reassign:
        raise ValueError("This job is already assigned to another step.")
    assigned = (
        step.allocations.exclude(job=job)
        .filter(job__source__in=authorized_sources(user))
        .exclude(job__status__in=["cancelled", "reverted"])
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    quantity = min(job_output(job), max(0, step.output_quantity - assigned))
    if not quantity:
        raise ValueError("This step is already covered or the job has no known output.")
    JobAllocation.objects.update_or_create(job=job, defaults={"step": step, "quantity": quantity})
    if job.auto_link_disabled:
        IndustryJob.objects.filter(pk=job.pk).update(auto_link_disabled=False)
    if refresh_progress:
        update_progress(user, extra_step_ids=[previous_step_id] if previous_step_id else ())


@transaction.atomic
def unallocate_job(user, job_id):
    job = IndustryJob.objects.select_for_update().get(pk=job_id, user=user)
    previous = list(JobAllocation.objects.filter(job=job).values_list("step_id", flat=True))
    JobAllocation.objects.filter(job=job).delete()
    IndustryJob.objects.filter(pk=job.pk).update(auto_link_disabled=True)
    update_progress(user, extra_step_ids=previous)


def update_progress(user, *, extra_step_ids=()):
    sources = authorized_sources(user)
    steps = list(
        PlanStep.objects.filter(Q(plan__archived=False) | Q(pk__in=extra_step_ids), plan__user=user)
    )
    allocations = list(
        JobAllocation.objects.filter(step__in=steps)
        .select_related("job")
        .order_by("job__start_date", "job__job_id")
    )
    outputs = job_outputs([a.job for a in allocations])
    allowed = set(sources.values_list("pk", flat=True))
    grouped = defaultdict(list)
    cancelled, changed = [], []
    for allocation in allocations:
        grouped[allocation.step_id].append(allocation)
    for step in steps:
        completed = ongoing = 0
        # Credit finished output first, so a finishing oversize job contributes its full output.
        for allocation in sorted(grouped[step.pk], key=lambda a: not a.job.finished):
            job = allocation.job
            if job.status in {"cancelled", "reverted"}:
                cancelled.append(allocation.pk)
                continue
            quantity = 0
            if job.source_id in allowed and job.status in {
                "active",
                "paused",
                "ready",
                "delivered",
            }:
                quantity = min(outputs[job.pk], max(0, step.output_quantity - completed - ongoing))
                if job.finished:
                    completed += quantity
                else:
                    ongoing += quantity
            if allocation.quantity != quantity:
                allocation.quantity = quantity
                changed.append(allocation)
        step.completed_quantity, step.ongoing_quantity = completed, ongoing
    JobAllocation.objects.filter(pk__in=cancelled).delete()
    JobAllocation.objects.bulk_update(changed, ["quantity"])
    PlanStep.objects.bulk_update(steps, ["completed_quantity", "ongoing_quantity"])


def reconcile_jobs(user):
    from allianceauth.authentication.models import CharacterOwnership

    update_progress(user)
    owned = CharacterOwnership.objects.filter(user=user).values_list(
        "character__character_id", flat=True
    )
    candidates = IndustryJob.objects.filter(
        user=user,
        source__in=authorized_sources(user),
        installer_id__in=owned,
        status__in=["active", "paused", "ready", "delivered"],
        allocation__isnull=True,
        auto_link_disabled=False,
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
                    allocate_job(user, job.pk, step.pk, refresh_progress=False)
                except ValueError:
                    continue
                break
    update_progress(user)
