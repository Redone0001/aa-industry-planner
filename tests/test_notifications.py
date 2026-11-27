import sys
from contextlib import nullcontext
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from eveuniverse.models import EveEntity

from aa_industry_planner.models import (
    Notification,
    NotificationPreferences,
    PlannerConfiguration,
    UserPreferences,
)
from aa_industry_planner.services.jobs import allocate_job, unallocate_job
from aa_industry_planner.services.notifications import (
    discord_embed,
    emit,
    notify_job_finished,
    remind_overdue_jobs,
    send_discord,
)
from aa_industry_planner.services.planning import create_plan
from aa_industry_planner.tasks import evaluate_all_watches

from .test_services import make_job

pytestmark = pytest.mark.django_db


def test_notifications_are_idempotent_and_preferences_are_respected(user):
    emit(user, "job_finished", "job:1:done", "Done", "Job completed")
    emit(user, "job_finished", "job:1:done", "Done", "Job completed")
    assert Notification.objects.count() == 1
    prefs = NotificationPreferences.objects.get(user=user)
    prefs.inventory_low = False
    prefs.save()
    assert emit(user, "inventory_low", "watch:1:low", "Low", "Low") is None


def test_periodic_checks_remind_at_finish_time_without_a_new_import(
    user, source, monkeypatch, django_capture_on_commit_callbacks
):
    finish = timezone.now()
    job = make_job(user, source, end_date=finish)
    NotificationPreferences.objects.create(user=user, discord_dm=True)
    deliver = Mock()
    monkeypatch.setattr("aa_industry_planner.tasks.deliver_notification.delay", deliver)
    monkeypatch.setattr(timezone, "now", lambda: finish - timezone.timedelta(seconds=1))
    evaluate_all_watches.run()
    assert not Notification.objects.exists()
    monkeypatch.setattr(timezone, "now", lambda: finish)

    with django_capture_on_commit_callbacks(execute=True):
        evaluate_all_watches.run()
        evaluate_all_watches.run()
    reminder = Notification.objects.get(kind="job_finished")
    assert reminder.event_key == f"job:{job.job_id}:finished"
    assert "scheduled to finish" in reminder.message
    deliver.assert_called_once_with(reminder.pk)


def test_periodic_watches_deduplicate_overdue_jobs_using_literal_event_keys(user, source):
    finish = timezone.now()
    make_job(user, source, job_id=501, end_date=finish)
    make_job(user, source, job_id=502, end_date=finish)
    emit(user, "job_finished", "job:501:finished", "Done", "Already notified")
    UserPreferences.objects.create(user=user, manufacturing_slots=1)

    with (
        CaptureQueriesContext(connection) as queries,
        patch("aa_industry_planner.services.watches.evaluate_watches") as watches,
    ):
        evaluate_all_watches.run()
    watches.assert_called_once_with(user)
    assert set(Notification.objects.values_list("event_key", flat=True)) == {
        "job:501:finished",
        "job:502:finished",
    }
    assert any(
        '"event_key" IN' in query["sql"] and "job:501:finished" in query["sql"] for query in queries
    )
    assert all(
        "CAST(" not in query["sql"]
        for query in queries
        if '"aa_industry_planner_industryjob"' in query["sql"]
    )


def test_reminder_skips_paused_cancelled_and_already_delivered_jobs(user, source):
    now = timezone.now()
    for tid, status in enumerate(["paused", "cancelled", "reverted", "ready", "delivered"], 1):
        make_job(user, source, job_id=tid, status=status, end_date=now)
    remind_overdue_jobs(now=now)
    assert not Notification.objects.exists()


def test_reminder_honors_completion_preference_and_current_source_access(user, source):
    now = timezone.now()
    make_job(user, source, end_date=now)
    pref = NotificationPreferences.objects.create(user=user, job_finished=False)
    remind_overdue_jobs(now=now)
    assert not Notification.objects.exists()

    pref.job_finished = True
    pref.save(update_fields=["job_finished"])
    user.character_ownerships.all().delete()
    remind_overdue_jobs(now=now)
    assert not Notification.objects.exists()


def test_discord_embed_includes_title_color_time_and_link(user):
    note = emit(user, "inventory_low", "watch:1:low", "Low inventory", "Refill assembly stock")
    embed = discord_embed(note)
    assert embed["title"] == "Low inventory"
    assert embed["color"] == 0xF4BC61
    assert embed["url"].endswith("/industry/notifications/")
    assert timezone.datetime.fromisoformat(embed["timestamp"])


@pytest.mark.parametrize("status", ["active", "ready"])
def test_job_discord_embed_links_current_plan_or_queue_and_bolds_installer(
    user, source, recipes, status, settings
):
    EveEntity.objects.create(pk=source.owner_id, name="Rin_Talos", category="character")
    plan = create_plan(user, 1, 2, use_inventory=False)
    step = plan.steps.get(item_type_id=1)
    job = make_job(user, source, status=status)
    note = notify_job_finished(job)
    queue_url = settings.SITE_URL + reverse("aa_industry_planner:production_queue")

    assert note.message.endswith("\nInstaller: Rin_Talos")
    unassigned = discord_embed(note)
    assert unassigned["description"].endswith("\nInstaller: **Rin\\_Talos**")
    assert unassigned["color"] == 0x32D6A0
    assert unassigned["url"] == queue_url

    allocate_job(user, job.pk, step.pk)
    assert discord_embed(note)["url"] == settings.SITE_URL + reverse(
        "aa_industry_planner:plan", args=[plan.pk]
    )

    unallocate_job(user, job.pk)
    assert discord_embed(note)["url"] == queue_url


@pytest.mark.parametrize("delivery", ["sent", "already_read", "failed"])
def test_dm_uses_multiverse_and_marks_successfully_sent_notifications_read(
    user, monkeypatch, delivery
):
    note = emit(user, "job_finished", "job:1", "Done", "Completed")
    prefs = NotificationPreferences.objects.get(user=user)
    prefs.discord_dm = True
    prefs.save()
    PlannerConfiguration.objects.create(discord_guild_id=1234)
    models = ModuleType("aadiscordmultiverse.models")
    models.DiscordManagedServer = Mock()
    models.DiscordManagedServer.user_can_access_guild.return_value = True
    models.MultiDiscordUser = Mock()
    models.MultiDiscordUser.objects.filter.return_value.first.return_value = Mock(uid=5678)
    app_settings = ModuleType("aadiscordmultiverse.app_settings")
    app_settings.DISCORD_BOT_TOKEN = "test-bot-token"
    monkeypatch.setitem(sys.modules, "aadiscordmultiverse.models", models)
    monkeypatch.setitem(sys.modules, "aadiscordmultiverse.app_settings", app_settings)
    session = Mock()
    session.post.return_value.json.return_value = {"id": "9000"}
    read_at = timezone.now() if delivery == "already_read" else None
    if read_at:
        Notification.objects.filter(pk=note.pk).update(read_at=read_at)
    if delivery == "failed":
        session.post.return_value.raise_for_status.side_effect = [None, RuntimeError("DM failed")]
    with pytest.raises(RuntimeError, match="DM failed") if delivery == "failed" else nullcontext():
        send_discord(note, session)
    models.MultiDiscordUser.objects.filter.assert_called_once_with(
        user=user, guild_id=1234, activated__isnull=False
    )
    first, second = session.post.call_args_list
    assert first.kwargs["json"] == {"recipient_id": "5678"}
    assert second.kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert second.kwargs["json"]["embeds"][0]["title"] == "Done"
    assert second.kwargs["json"]["embeds"][0]["url"].endswith("/industry/production-queue/")
    assert second.kwargs["json"]["enforce_nonce"]
    note.refresh_from_db()
    if delivery == "failed":
        assert note.discord_sent_at is None and note.read_at is None
    else:
        assert note.discord_sent_at
        assert note.read_at == (read_at or note.discord_sent_at)


def test_migration_marks_previously_delivered_notifications_read(user):
    delivered_at = timezone.now()
    read_at = delivered_at - timezone.timedelta(minutes=1)
    delivered = emit(user, "job_finished", "delivered", "Done", "Completed")
    read = emit(user, "job_finished", "read", "Done", "Completed")
    unread = emit(user, "job_finished", "unread", "Done", "Completed")
    Notification.objects.filter(pk__in=[delivered.pk, read.pk]).update(discord_sent_at=delivered_at)
    Notification.objects.filter(pk=read.pk).update(read_at=read_at)
    migration = import_module(
        "aa_industry_planner.migrations.0010_sidebar_slot_badge_and_discord_read"
    )
    migration.mark_delivered_notifications_read(apps, SimpleNamespace(connection=connection))
    assert dict(Notification.objects.values_list("pk", "read_at")) == {
        delivered.pk: delivered_at,
        read.pk: read_at,
        unread.pk: None,
    }


def test_no_dm_when_user_has_not_opted_in(user, monkeypatch):
    note = emit(user, "job_finished", "job:1", "Done", "Completed")
    session = Mock()
    send_discord(note, session)
    session.post.assert_not_called()
