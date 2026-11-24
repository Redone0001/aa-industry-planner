"""Batch assignment choices and human-readable reasons for unlinked jobs."""

from collections import defaultdict

from allianceauth.authentication.models import CharacterOwnership
from django.db.models import Sum

from aa_industry_planner.models import JOB_ACTIVITIES, JobAllocation, PlanStep

from .inventory import authorized_sources
from .jobs import compatible, job_outputs


def annotate_assignments(user, jobs):
    steps = list(
        PlanStep.objects.filter(plan__user=user, plan__archived=False)
        .select_related("plan__item_type", "item_type", "service__facility")
        .order_by("-plan__created_at", "position")
    )
    totals = dict(
        JobAllocation.objects.filter(step__in=steps, job__source__in=authorized_sources(user))
        .exclude(job__status__in=["cancelled", "reverted"])
        .values("step_id")
        .annotate(total=Sum("quantity"))
        .values_list("step_id", "total")
    )
    owned = set(
        CharacterOwnership.objects.filter(user=user).values_list(
            "character__character_id", flat=True
        )
    )
    by_product = defaultdict(list)
    for step in steps:
        by_product[step.item_type_id].append(step)
    outputs = job_outputs(jobs)
    for job in jobs:
        current = getattr(job, "allocation", None)
        matching_product = by_product[job.product_type_id]
        matching = [step for step in matching_product if compatible(job, step)]
        job.assignment_choices = []
        for step in matching:
            credited_here = current.quantity if current and current.step_id == step.pk else 0
            if outputs[job.pk] and (
                step.output_quantity > totals.get(step.pk, 0) - credited_here or credited_here
            ):
                job.assignment_choices.append(step)
        job.assignment_selected = (
            current.step_id
            if current and any(s.pk == current.step_id for s in job.assignment_choices)
            else None
        )
        if current:
            job.match_note = ""
        elif job.activity_id not in JOB_ACTIVITIES:
            job.match_note = (
                "This activity is tracked here but is not a manufacturing or reaction plan step."
            )
        elif job.status in {"cancelled", "reverted"}:
            job.match_note = "Cancelled or reverted jobs cannot cover production steps."
        elif not matching_product:
            job.match_note = "No active plan needs this product."
        elif not any(s.blueprint_type_id == job.blueprint_type_id for s in matching_product):
            job.match_note = "No active step uses this blueprint or reaction formula."
        elif not any(
            s.blueprint_type_id == job.blueprint_type_id
            and s.activity == JOB_ACTIVITIES[job.activity_id]
            for s in matching_product
        ):
            job.match_note = "The job activity does not match the planned activity."
        elif not matching:
            expected = sorted(
                {
                    s.details.get("context", {}).get("structure_id")
                    or (s.service.facility.structure_id if s.service_id else None)
                    for s in matching_product
                    if s.blueprint_type_id == job.blueprint_type_id
                    and s.activity == JOB_ACTIVITIES[job.activity_id]
                }
                - {None}
            )
            job.match_note = (
                f"Structure mismatch: job at #{job.facility_id}; planned at "
                + ", ".join(f"#{sid}" for sid in expected)
                + "."
            )
        elif not outputs[job.pk]:
            job.match_note = "Job output is unavailable from the SDE. Reload the blueprint data."
        elif not job.assignment_choices:
            job.match_note = "Compatible steps are already covered by other jobs."
        elif job.auto_link_disabled:
            job.match_note = (
                "Automatic matching is disabled. Assign a plan here to link this job again."
            )
        elif job.installer_id not in owned:
            job.match_note = "The installer is not one of your AA characters. Assign this corporation job manually."
        elif job.finished and all(
            job.start_date < s.plan.created_at for s in job.assignment_choices
        ):
            job.match_note = "This finished job started before the matching plans. Assign it manually if it belongs here."
        else:
            job.match_note = "Select a compatible plan to assign this job."
