from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from aa_industry_planner.models import Notification, NotificationPreferences, PlannerConfiguration


def emit(user, kind, event_key, title, message):
    preferences, _ = NotificationPreferences.objects.get_or_create(user=user)
    if not getattr(preferences, kind, False):
        return None
    notification, created = Notification.objects.get_or_create(
        user=user, event_key=event_key, defaults={"kind": kind, "title": title, "message": message}
    )
    if created and preferences.discord_dm:
        from aa_industry_planner.tasks import deliver_notification

        transaction.on_commit(lambda: deliver_notification.delay(notification.pk))
    return notification


def discord_embed(notification):
    site_url = getattr(settings, "SITE_URL", "").rstrip("/")
    embed = {
        "title": notification.title[:256],
        "description": notification.message[:4096],
        "color": {
            "job_finished": 0x32D6A0,
            "inventory_low": 0xF4BC61,
            "capacity_low": 0xF4BC61,
            "sync_failed": 0xED6D72,
        }.get(notification.kind, 0x63A8FF),
        "timestamp": notification.created_at.isoformat(),
        "author": {"name": "Industry Planner"},
        "footer": {"text": "Alliance Auth • Industrial operations"},
    }
    if site_url:
        embed["url"] = site_url + reverse("aa_industry_planner:notifications")
    return embed


def send_discord(notification, session=None):
    """Resolve through Multiverse's configured guild, then send an opt-in embed DM."""
    import requests

    preferences, _ = NotificationPreferences.objects.get_or_create(user=notification.user)
    if not preferences.discord_dm or not getattr(preferences, notification.kind, False):
        return
    if not notification.user.is_active or not notification.user.has_perm(
        "aa_industry_planner.access_planner"
    ):
        return
    from aadiscordmultiverse.app_settings import DISCORD_BOT_TOKEN
    from aadiscordmultiverse.models import DiscordManagedServer, MultiDiscordUser

    config = PlannerConfiguration.objects.filter(pk=1).first()
    if not config or not config.discord_guild_id:
        raise ValueError("An administrator must configure the Discord Multiverse guild ID.")
    if not DiscordManagedServer.user_can_access_guild(notification.user, config.discord_guild_id):
        raise ValueError("User no longer has access to the configured Multiverse guild.")
    account = MultiDiscordUser.objects.filter(
        user=notification.user, guild_id=config.discord_guild_id, activated__isnull=False
    ).first()
    if not account:
        raise ValueError("No activated Discord Multiverse account is linked for this guild.")
    http = session or requests.Session()
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": "AllianceAuth Industry Planner/0.1",
    }
    response = http.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers=headers,
        json={"recipient_id": str(account.uid)},
        timeout=20,
    )
    response.raise_for_status()
    channel_id = response.json()["id"]
    # A stable nonce reduces duplicate delivery if a worker retries after a timeout.
    response = http.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers,
        json={
            "embeds": [discord_embed(notification)],
            "allowed_mentions": {"parse": []},
            "nonce": str(notification.pk),
            "enforce_nonce": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    Notification.objects.filter(pk=notification.pk).update(
        discord_sent_at=timezone.now(), discord_error=""
    )
