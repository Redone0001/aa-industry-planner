import logging
from math import ceil

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException

from . import __version__
from .models import IndustryJob, InventorySource, Notification, UserPreferences

logger = logging.getLogger(__name__)


def sync_failure_message(exc, stage):
    if isinstance(exc, PermissionDenied):
        return str(exc)
    # Exception bodies/headers can include credentials or private response data.
    # Show only a fixed stage, exception class, HTTP status and worker version.
    status = getattr(exc, "status_code", None)
    http = f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
    cause, seen = exc, set()
    timeout = False
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        timeout |= type(cause).__name__ in {
            "ReadTimeout",
            "ConnectTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "TimeoutError",
        }
        cause = cause.__cause__ or cause.__context__
    summary = "ESI network timeout" if timeout else "Import error"
    return (
        f"{summary} during {stage} ({type(exc).__name__}{http}; worker {__version__}). "
        "Check the worker traceback for details."
    )


@shared_task(bind=True, max_retries=None)
def refresh_source(self, source_id):
    from .services.notifications import emit
    from .services.sync import sync_source

    key = f"aip:sync:{source_id}"
    retry_key = f"aip:sync:retry-after:{source_id}"
    if not cache.add(key, True, timeout=1800):
        return
    source = None
    try:
        source = InventorySource.objects.select_related("user", "token").get(
            pk=source_id, enabled=True
        )
        if source.kind == "manual":
            return
        retry_after = cache.get(retry_key)
        if (
            source.last_error
            and isinstance(retry_after, (int, float))
            and retry_after > timezone.now().timestamp()
        ):
            return
        sync_source(source)
        cache.delete(retry_key)
    except InventorySource.DoesNotExist:
        return
    except (ESIBucketLimitException, ESIErrorLimitException) as exc:
        raise self.retry(exc=exc, countdown=max(5, exc.reset)) from exc
    except Exception as exc:
        stage = getattr(source, "_sync_stage", "loading source")
        message = sync_failure_message(exc, stage)
        logger.warning(
            "Industry source %s import error during %s (%s; worker %s)",
            source_id,
            stage,
            type(exc).__name__,
            __version__,
        )
        if source is None:
            raise self.retry(
                exc=exc, countdown=min(900, 60 * 2 ** min(self.request.retries, 4)), max_retries=4
            ) from exc
        now = timezone.now()
        preferences, _ = UserPreferences.objects.get_or_create(user=source.user)
        permanent = isinstance(exc, PermissionDenied)
        with transaction.atomic():
            current = InventorySource.objects.select_for_update().filter(pk=source_id).first()
            if current is None:
                return
            since = current.sync_error_since or now
            grace = preferences.sync_error_grace_minutes * 60
            remaining = grace - (now - since).total_seconds()
            failed = permanent or remaining <= 0
            current.last_error, current.sync_error_since = message[:250], since
            if failed:
                current.sync_failed_at = current.sync_failed_at or now
            else:
                current.sync_failed_at = None
            if permanent:
                current.enabled = False
            current.save(
                update_fields=["last_error", "sync_error_since", "sync_failed_at", "enabled"]
            )
            if failed:
                emit(
                    source.user,
                    "sync_failed",
                    f"sync:{source_id}:{since.isoformat()}",
                    "ESI source needs attention",
                    f"{source.name}: {message}",
                )
        if not permanent:
            delay = (
                min(900, 60 * 2 ** min(self.request.retries, 4), max(1, remaining))
                if not failed
                else 600
            )
            # Periodic/manual refreshes must share the same backoff, rather than
            # starting another unlimited retry chain while this one is waiting.
            cache.set(retry_key, now.timestamp() + delay, timeout=ceil(delay) + 60)
            if not failed:
                raise self.retry(exc=exc, countdown=delay) from exc
            raise
    finally:
        cache.delete(key)


@shared_task
def refresh_all_sources():
    for pk in (
        InventorySource.objects.filter(enabled=True)
        .exclude(kind="manual")
        .values_list("pk", flat=True)
    ):
        refresh_source.delay(pk)


@shared_task
def resolve_entity_names(user_id):
    from eveuniverse.models import EveEntity

    ids = set(IndustryJob.objects.filter(user_id=user_id).values_list("installer_id", flat=True))
    ids.update(
        InventorySource.objects.filter(user_id=user_id)
        .exclude(kind="manual")
        .values_list("owner_id", flat=True)
    )
    if ids:
        EveEntity.objects.bulk_resolve_ids(ids)
    for source in InventorySource.objects.filter(user_id=user_id, kind="corporation"):
        entity = EveEntity.objects.filter(pk=source.owner_id).first()
        if entity and entity.name:
            InventorySource.objects.filter(pk=source.pk).update(name=entity.name)


@shared_task(bind=True, max_retries=4)
def refresh_public_data(self):
    from .services.sync import refresh_indices, refresh_prices

    try:
        refresh_indices()
        refresh_prices()
    except (ESIBucketLimitException, ESIErrorLimitException) as exc:
        raise self.retry(exc=exc, countdown=max(5, exc.reset)) from exc
    except Exception as exc:
        raise self.retry(exc=exc, countdown=300) from exc


@shared_task
def evaluate_all_watches():
    from django.db.models import Q

    from .services.capacity import evaluate_slot_warnings
    from .services.notifications import remind_overdue_jobs
    from .services.watches import evaluate_watches

    remind_overdue_jobs()
    for user in (
        get_user_model()
        .objects.filter(
            Q(industry_watches__enabled=True)
            | Q(industry_preferences__manufacturing_slots__gt=0)
            | Q(industry_preferences__reaction_slots__gt=0),
            is_active=True,
        )
        .distinct()
    ):
        if user.has_perm("aa_industry_planner.access_planner"):
            evaluate_watches(user)
            evaluate_slot_warnings(user)


@shared_task(bind=True, max_retries=4)
def deliver_notification(self, notification_id):
    from .services.notifications import send_discord

    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update().select_related("user").get(pk=notification_id)
        )
        if notification.discord_sent_at or notification.discord_attempts >= 5:
            return
        notification.discord_attempts += 1
        notification.save(update_fields=["discord_attempts"])
        try:
            send_discord(notification)
        except Exception as exc:
            notification.discord_error = f"DM delivery failed ({type(exc).__name__}). Check your Multiverse link and allow direct messages."
            notification.save(update_fields=["discord_error"])
            # Persist failure before retrying, so the panel can explain what happened.
            retry_error = exc
        else:
            return
    response = getattr(retry_error, "response", None)
    if isinstance(retry_error, (ValueError, ImportError)) or (
        response is not None and response.status_code in {401, 403, 404}
    ):
        return
    delay = 60 * 2**self.request.retries
    if response is not None and response.status_code == 429:
        try:
            delay = max(1, min(86400, float(response.json().get("retry_after", delay))))
        except (ValueError, TypeError):
            pass
    raise self.retry(exc=retry_error, countdown=delay)
