import sys
from types import ModuleType
from unittest.mock import Mock

import pytest
from django.utils import timezone

from aa_industry_planner.models import Notification, NotificationPreferences, PlannerConfiguration
from aa_industry_planner.services.notifications import discord_embed, emit, send_discord

pytestmark = pytest.mark.django_db


def test_notifications_are_idempotent_and_preferences_are_respected(user):
    emit(user, "job_finished", "job:1:done", "Done", "Job completed")
    emit(user, "job_finished", "job:1:done", "Done", "Job completed")
    assert Notification.objects.count() == 1
    prefs = NotificationPreferences.objects.get(user=user)
    prefs.inventory_low = False
    prefs.save()
    assert emit(user, "inventory_low", "watch:1:low", "Low", "Low") is None


def test_discord_embed_includes_title_color_time_and_link(user):
    note = emit(user, "job_finished", "job:1", "Assembly complete", "Ready for delivery")
    embed = discord_embed(note)
    assert embed["title"] == "Assembly complete"
    assert embed["color"] == 0x32D6A0
    assert embed["url"].endswith("/industry/notifications/")
    assert timezone.datetime.fromisoformat(embed["timestamp"])


def test_dm_uses_multiverse_user_in_configured_guild_and_disables_mentions(user, monkeypatch):
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
    send_discord(note, session)
    models.MultiDiscordUser.objects.filter.assert_called_once_with(
        user=user, guild_id=1234, activated__isnull=False
    )
    first, second = session.post.call_args_list
    assert first.kwargs["json"] == {"recipient_id": "5678"}
    assert second.kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert second.kwargs["json"]["embeds"][0]["title"] == "Done"
    assert second.kwargs["json"]["enforce_nonce"]
    note.refresh_from_db()
    assert note.discord_sent_at


def test_no_dm_when_user_has_not_opted_in(user, monkeypatch):
    note = emit(user, "job_finished", "job:1", "Done", "Completed")
    session = Mock()
    send_discord(note, session)
    session.post.assert_not_called()
