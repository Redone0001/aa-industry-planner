from datetime import timedelta
from math import ceil

from django.db.models import Sum
from django.db.models.functions import Coalesce
from eve_sde.models import ItemType

from aa_industry_planner.models import JOB_ACTIVITIES

from .capacity import ACTIVITIES, capacity_status


def job_dashboard(user, jobs, now):
    capacity = capacity_status(user, now=now)
    rows = capacity["rows"]
    has_import = bool(capacity["jobs"]) or any(s.last_jobs_sync for s in capacity["sources"])
    finishes = {activity: [] for activity in ACTIVITIES}
    for job in capacity["jobs"]:
        if job.status == "active" and job.end_date > now:
            finishes[JOB_ACTIVITIES[job.activity_id]].append(ceil(job.end_date.timestamp() * 1000))

    recent_jobs = jobs.filter(start_date__range=(now - timedelta(days=30), now)).exclude(
        status__in=("cancelled", "reverted")
    )
    for row in rows:
        activity = row["activity"]
        reported_active = row["active"]
        row["active"] = len(finishes[activity])
        row["past_finish"] = reported_active - row["active"]
        row["occupied"] = reported_active + row["paused"]
        row["used_percent"] = round(100 * row["occupied"] / row["slots"]) if row["slots"] else None
        activity_ids = [pk for pk, name in JOB_ACTIVITIES.items() if name == activity]
        row["ranking"] = list(
            recent_jobs.filter(activity_id__in=activity_ids)
            .values(type_id=Coalesce("product_type_id", "blueprint_type_id"))
            .annotate(runs=Sum("runs"))
            .order_by("-runs", "type_id")[:3]
        )

    names = dict(
        ItemType.objects.filter(
            pk__in={item["type_id"] for row in rows for item in row["ranking"]}
        ).values_list("pk", "name")
    )
    for row in rows:
        for item in row["ranking"]:
            item["name"] = names.get(item["type_id"], str(item["type_id"]))

    return {
        "rows": rows,
        "has_import": has_import,
        "burndown": (
            {"now": int(now.timestamp() * 1000), **finishes} if any(finishes.values()) else None
        ),
    }
