from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from django.db import IntegrityError
from django.urls import reverse

from aa_industry_planner import __version__
from aa_industry_planner.models import Asset, Notification
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
    assert Notification.objects.filter(user=user, kind="sync_failed").count() == 1
    client.force_login(user)
    for page in ["inventory", "jobs"]:
        content = client.get(reverse(f"aa_industry_planner:{page}")).content
        assert b"alert alert-danger aip-sync-error" in content
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
