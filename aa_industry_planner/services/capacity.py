"""User-wide job slots; corporate colleagues do not consume the user's capacity."""

from collections import Counter
from datetime import timedelta

from allianceauth.authentication.models import CharacterOwnership
from django.db import transaction
from django.utils import timezone

from aa_industry_planner.models import (
    JOB_ACTIVITIES,
    IndustryJob,
    InventorySource,
    SlotMonitorState,
    UserPreferences,
)

from .inventory import authorized_sources

ACTIVITIES = ("manufacturing", "reaction")


def capacity_status(user, preferences=None, now=None):
    now = now or timezone.now()
    preferences = preferences or UserPreferences.objects.get_or_create(user=user)[0]
    sources = list(authorized_sources(user).exclude(kind="manual"))
    owned = CharacterOwnership.objects.filter(user=user).values_list(
        "character__character_id", flat=True
    )
    jobs = list(
        IndustryJob.objects.filter(
            user=user,
            source__in=sources,
            installer_id__in=owned,
            activity_id__in=[1, 9, 11],
            status__in=["active", "paused", "ready"],
        )
    )
    counts = Counter((JOB_ACTIVITIES[job.activity_id], job.status) for job in jobs)
    configured_sources = InventorySource.objects.filter(user=user).exclude(kind="manual").count()
    fresh = (
        bool(sources)
        and len(sources) == configured_sources
        and all(
            source.last_jobs_sync
            and source.last_jobs_sync >= now - timedelta(minutes=30)
            and not source.last_error
            for source in sources
        )
    )
    rows = []
    for activity in ACTIVITIES:
        slots = getattr(preferences, f"{activity}_slots")
        active, paused = counts[activity, "active"], counts[activity, "paused"]
        rows.append(
            dict(
                activity=activity,
                label="Reactions" if activity == "reaction" else "Manufacturing",
                slots=slots,
                active=active,
                paused=paused,
                ready=counts[activity, "ready"],
                idle=max(0, slots - active - paused),
                min_active=getattr(preferences, f"{activity}_min_active"),
                max_idle=getattr(preferences, f"{activity}_max_idle"),
            )
        )
    return dict(rows=rows, jobs=jobs, fresh=fresh, sources=sources)


@transaction.atomic
def evaluate_slot_warnings(user, now=None):
    from .notifications import emit

    status = capacity_status(user, now=now)
    # Never turn a missing, failed or stale import into an "idle slots" alert.
    if not status["fresh"]:
        return
    for row in status["rows"]:
        state, _ = SlotMonitorState.objects.get_or_create(user=user, activity=row["activity"])
        state = SlotMonitorState.objects.select_for_update().get(pk=state.pk)
        reasons = []
        if row["slots"]:
            if row["min_active"] is not None and row["active"] < row["min_active"]:
                reasons.append(f"active jobs below {row['min_active']}")
            if row["max_idle"] is not None and row["idle"] > row["max_idle"]:
                reasons.append(f"idle slots above {row['max_idle']}")
        if reasons and not state.warning_active:
            state.episode += 1
            note = emit(
                user,
                "capacity_low",
                f"slots:{state.pk}:{state.episode}",
                f"{row['label']} slot warning",
                f"{row['active']} active, {row['paused']} paused, {row['idle']} idle / {row['slots']} configured slots: {', '.join(reasons)}. Ready for delivery: {row['ready']} (included in idle). Counts cover your owned characters in the latest connected ESI imports.",
            )
            state.warning_active = note is not None
        elif not reasons:
            state.warning_active = False
        state.save(update_fields=["episode", "warning_active"])
