from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from aa_industry_planner.models import (
    AdjustedPrice,
    Asset,
    Facility,
    IndustryJob,
    InventorySource,
    SystemCostIndex,
)
from aa_industry_planner.providers import ESI, missing_scopes, required_scopes

from .inventory import authorized_sources
from .jobs import reconcile_jobs


def validate_source(source, api):
    if not source.user.is_active or not source.user.has_perm("aa_industry_planner.access_planner"):
        raise PermissionDenied("Planner access has been revoked.")
    if not authorized_sources(source.user).filter(pk=source.pk).exists():
        missing = missing_scopes(source.token, source.kind)
        if missing:
            raise PermissionDenied(
                f"Missing ESI permissions: {', '.join(missing)}. Reconnect this source."
            )
        raise PermissionDenied(
            "Character ownership, token or corporation access has changed. Reconnect this source."
        )
    from esi.models import Token

    token = (
        Token.objects.filter(pk=source.token_id, user=source.user)
        .require_scopes(required_scopes(source.kind))
        .require_valid()
        .first()
    )
    if not token:
        raise PermissionDenied("The source requires a valid token with the requested scopes.")
    # require_valid may rotate credentials on a different Token instance.
    source.token = token
    if source.kind == "corporation":
        character = api.character(source.token.character_id)
        if character["corporation_id"] != source.owner_id:
            raise PermissionDenied("The authorized character has left this corporation.")
        if "Director" not in api.roles(source.token).get("roles", []):
            raise PermissionDenied("Corporation asset access requires the Director role.")


def sync_source(source, api=None):
    api = api or ESI()
    source._sync_stage = "authorization"
    validate_source(source, api)
    # Fetch every page of both datasets before replacing any stored asset snapshot.
    source._sync_stage = "fetching assets"
    assets = api.assets(source)
    source._sync_stage = "fetching jobs"
    jobs = api.jobs(source)
    timestamp = timezone.now()
    source._sync_stage = "saving assets and jobs"
    with transaction.atomic():
        InventorySource.objects.select_for_update().get(pk=source.pk)
        Asset.objects.filter(source=source).delete()
        Asset.objects.bulk_create(
            [
                Asset(
                    source=source,
                    item_id=a["item_id"],
                    type_id=a["type_id"],
                    quantity=max(0, a["quantity"]),
                    location_id=a["location_id"],
                    # The SDK represents omitted optional properties as None.
                    location_flag=a.get("location_flag") or "",
                    is_blueprint_copy=bool(a.get("is_blueprint_copy")),
                )
                for a in assets
            ],
            batch_size=1000,
        )
        for data in jobs:
            fields = {
                key: data.get(key)
                for key in [
                    "installer_id",
                    "activity_id",
                    "blueprint_type_id",
                    "product_type_id",
                    "facility_id",
                    "runs",
                    "successful_runs",
                    "status",
                    "cost",
                ]
            }
            fields.update(
                start_date=parse_datetime(data["start_date"]),
                end_date=parse_datetime(data["end_date"]),
            )
            existing = (
                IndustryJob.objects.filter(user=source.user, job_id=data["job_id"])
                .select_related("source")
                .first()
            )
            # A personal endpoint and corporate endpoint can report the same job.
            fields["source"] = (
                existing.source
                if existing and existing.source.kind == "character" and source.kind == "corporation"
                else source
            )
            job, _ = IndustryJob.objects.update_or_create(
                user=source.user, job_id=data["job_id"], defaults=fields
            )
            newly_finished = (existing and not existing.finished) or (
                not existing and source.last_jobs_sync and job.end_date >= source.last_jobs_sync
            )
            if job.finished and newly_finished:
                from .notifications import emit

                emit(
                    source.user,
                    "job_finished",
                    f"job:{job.job_id}:finished",
                    "Industry job finished",
                    f"Job #{job.job_id} has finished ({job.runs:,} runs). Status: {job.status}. Ready jobs must still be delivered in EVE.",
                )
        InventorySource.objects.filter(pk=source.pk).update(
            last_assets_sync=timestamp, last_jobs_sync=timestamp, last_error=""
        )
    source._sync_stage = "updating plan progress"
    reconcile_jobs(source.user)
    from .watches import evaluate_watches

    source._sync_stage = "evaluating inventory targets"
    evaluate_watches(source.user)
    from aa_industry_planner.tasks import resolve_entity_names

    source._sync_stage = "queueing name lookup"
    resolve_entity_names.delay(source.user_id)


def refresh_indices(api=None, now=None):
    api, now = api or ESI(), now or timezone.now()
    systems = set(Facility.objects.filter(active=True).values_list("solar_system_id", flat=True))
    sample = now.replace(minute=0, second=0, microsecond=0)
    rows = api.indices() if systems else []
    metadata = getattr(api, "indices_metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    with transaction.atomic():
        for system in rows:
            if system["solar_system_id"] not in systems:
                continue
            for index in system["cost_indices"]:
                SystemCostIndex.objects.update_or_create(
                    solar_system_id=system["solar_system_id"],
                    activity=index["activity"],
                    recorded_at=sample,
                    defaults={
                        "cost_index": Decimal(str(index["cost_index"])),
                        "retrieved_at": now,
                        **metadata,
                    },
                )
        SystemCostIndex.objects.filter(recorded_at__lt=now - timezone.timedelta(days=90)).delete()


def refresh_prices(api=None):
    api = api or ESI()
    rows, now = api.prices(), timezone.now()
    metadata = getattr(api, "prices_metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    prices = [
        AdjustedPrice(
            type_id=row["type_id"],
            adjusted_price=Decimal(str(row["adjusted_price"]))
            if row.get("adjusted_price") is not None
            else None,
            updated_at=now,
            **metadata,
        )
        for row in rows
    ]
    options = {
        "update_conflicts": True,
        "update_fields": ["adjusted_price", "updated_at", "source_modified_at", "cache_expires_at"],
    }
    if connection.features.supports_update_conflicts_with_target:
        options["unique_fields"] = ["type_id"]
    with transaction.atomic():
        AdjustedPrice.objects.bulk_create(prices, batch_size=1000, **options)
        AdjustedPrice.objects.exclude(type_id__in=[row["type_id"] for row in rows]).delete()
