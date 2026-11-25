from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from aa_industry_planner import __version__
from aa_industry_planner.models import Asset, Notification, UserPreferences
from aa_industry_planner.services.sync import sync_source
from aa_industry_planner.tasks import refresh_source, sync_failure_message


def test_error_summary_excludes_private_exception_content():
    error = RuntimeError("Authorization: Bearer do-not-display; private response body")
    error.status_code = 403
    message = sync_failure_message(error, "fetching assets")
    assert "fetching assets" in message
    assert "HTTP 403" in message
    assert "RuntimeError" in message
    assert f"worker {__version__}" in message
    assert "do-not-display" not in message
    assert "private response" not in message


def test_storage_failure_keeps_stock_and_reports_stage_without_disabling_auth(user, source, client):
    Asset.objects.create(source=source, item_id=42, type_id=34, quantity=5, location_id=60000001)
    api = Mock()
    api.assets.return_value = []
    api.jobs.return_value = []
    with (
        patch("aa_industry_planner.services.sync.ESI", return_value=api),
        patch(
            "aa_industry_planner.services.sync.Asset.objects.bulk_create",
            side_effect=IntegrityError("private-sql-secret"),
        ),
        patch.object(refresh_source, "retry", side_effect=Retry("retry requested")),
        pytest.raises(Retry),
    ):
        refresh_source.run(source.pk)
    source.refresh_from_db()
    assert source.enabled
    assert source.last_assets_sync is None and source.last_jobs_sync is None
    assert Asset.objects.get(source=source, item_id=42).quantity == 5
    assert "saving assets and jobs" in source.last_error
    assert "IntegrityError" in source.last_error
    assert f"worker {__version__}" in source.last_error
    assert "private-sql-secret" not in source.last_error
    assert source.sync_error_since and source.sync_failed_at is None
    assert Notification.objects.filter(user=user, kind="sync_failed").count() == 0
    client.force_login(user)
    for page in ["configuration", "jobs"]:
        content = client.get(reverse(f"aa_industry_planner:{page}")).content
        assert b"alert alert-warning aip-sync-error" in content
        assert b"saving assets and jobs" in content
        assert b"private-sql-secret" not in content


def test_source_load_failure_retries_without_an_unbound_source_error(user):
    with (
        patch("aa_industry_planner.tasks.InventorySource.objects") as manager,
        patch.object(refresh_source, "retry", side_effect=Retry("retry requested")) as retry,
    ):
        manager.select_related.return_value.get.side_effect = RuntimeError(
            "database temporarily unavailable"
        )
        with pytest.raises(Retry):
            refresh_source.run(999)
    retry.assert_called_once()
    assert isinstance(retry.call_args.kwargs["exc"], RuntimeError)


def test_wrapped_network_timeout_has_a_safe_explanation():
    error = RuntimeError("private request details")
    error.__cause__ = TimeoutError("secret URL and token")
    message = sync_failure_message(error, "fetching assets")
    assert "ESI network timeout" in message
    assert "secret" not in message and "private" not in message


def test_timeout_retries_until_grace_expires_then_reports_one_failure_episode(user, source):
    first = timezone.now()
    UserPreferences.objects.create(user=user, sync_error_grace_minutes=30)
    assert (
        refresh_source.max_retries is None
    )  # A short retry count must not end a longer grace period.
    with patch("aa_industry_planner.services.sync.sync_source", side_effect=TimeoutError("secret")):
        for minutes in [0, 10, 29]:
            with (
                patch(
                    "aa_industry_planner.tasks.timezone.now",
                    return_value=first + timedelta(minutes=minutes),
                ),
                patch.object(refresh_source, "retry", side_effect=Retry("retry")) as retry,
                pytest.raises(Retry),
            ):
                refresh_source.run(source.pk)
            source.refresh_from_db()
            assert source.sync_error_since == first
            assert source.sync_failed_at is None
            assert source.enabled
            assert not Notification.objects.exists()
            assert 0 < retry.call_args.kwargs["countdown"] <= (30 - minutes) * 60
        for minutes in [30, 40]:
            with (
                patch(
                    "aa_industry_planner.tasks.timezone.now",
                    return_value=first + timedelta(minutes=minutes),
                ),
                pytest.raises(TimeoutError),
            ):
                refresh_source.run(source.pk)
        source.refresh_from_db()
        assert source.sync_failed_at == first + timedelta(minutes=30)
        assert Notification.objects.filter(kind="sync_failed").count() == 1


def test_periodic_refresh_during_backoff_does_not_start_another_retry_chain(user, source):
    with (
        patch(
            "aa_industry_planner.services.sync.sync_source", side_effect=TimeoutError("network")
        ) as sync,
        patch.object(refresh_source, "retry", side_effect=Retry("retry")),
    ):
        with pytest.raises(Retry):
            refresh_source.run(source.pk)
        refresh_source.run(source.pk)
    sync.assert_called_once()
    source.refresh_from_db()
    assert source.sync_failed_at is None


def test_revoked_access_is_reported_immediately_despite_grace_period(user, source):
    with patch(
        "aa_industry_planner.services.sync.sync_source",
        side_effect=PermissionDenied("Reconnect this source."),
    ):
        refresh_source.run(source.pk)
    source.refresh_from_db()
    assert not source.enabled
    assert source.sync_failed_at
    assert Notification.objects.get().kind == "sync_failed"


def test_successful_sync_clears_the_error_streak_and_failure_marker(user, source):
    source.last_error = "Previous timeout"
    source.sync_error_since = timezone.now() - timedelta(hours=1)
    source.sync_failed_at = timezone.now()
    source.save()
    api = Mock()
    api.assets.return_value, api.jobs.return_value = [], []
    with (
        patch("aa_industry_planner.services.sync.validate_source"),
        patch("aa_industry_planner.tasks.resolve_entity_names.delay"),
    ):
        sync_source(source, api)
    source.refresh_from_db()
    assert source.last_error == ""
    assert source.sync_error_since is None and source.sync_failed_at is None
    assert source.last_jobs_sync and source.last_assets_sync
