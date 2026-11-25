from datetime import timedelta
from unittest.mock import patch

import pytest
from allianceauth.menu.templatetags.menu_menu_items import render_menu
from django.urls import reverse
from django.utils import timezone

from aa_industry_planner.forms import OperationsForm
from aa_industry_planner.models import (
    IndustryJob,
    InventorySource,
    Notification,
    NotificationPreferences,
    SlotMonitorState,
    UserPreferences,
)
from aa_industry_planner.services.capacity import capacity_status, evaluate_slot_warnings
from aa_industry_planner.tasks import evaluate_all_watches

pytestmark = pytest.mark.django_db


@pytest.fixture
def fresh(source):
    source.last_jobs_sync = timezone.now()
    source.save()
    return source


def add_job(user, source, activity=1, status="active", installer=None):
    now = timezone.now()
    return IndustryJob.objects.create(
        user=user,
        source=source,
        job_id=IndustryJob.objects.count() + 1,
        installer_id=installer or source.owner_id,
        activity_id=activity,
        blueprint_type_id=101,
        product_type_id=1,
        facility_id=1234,
        runs=1,
        status=status,
        start_date=now - timedelta(minutes=1),
        end_date=now + timedelta(hours=1),
    )


def settings(user, **kwargs):
    return UserPreferences.objects.create(
        user=user, manufacturing_slots=4, reaction_slots=4, **kwargs
    )


def test_counts_use_owned_installers_both_reaction_ids_and_paused_occupancy(user, fresh):
    preferences = settings(user)
    add_job(user, fresh)
    add_job(user, fresh, status="paused")
    add_job(user, fresh, activity=9)
    add_job(user, fresh, activity=11)
    add_job(user, fresh, installer=99999999)  # A corporation colleague is not our slot.
    add_job(user, fresh, activity=3)
    status = capacity_status(user, preferences)
    manufacture, reaction = status["rows"]
    assert status["fresh"]
    assert (manufacture["active"], manufacture["paused"], manufacture["idle"]) == (1, 1, 2)
    assert (reaction["active"], reaction["idle"]) == (2, 2)


def test_both_thresholds_alert_once_and_rearm_after_recovery(user, fresh):
    settings(user, manufacturing_min_active=2, manufacturing_max_idle=2)
    evaluate_slot_warnings(user)
    evaluate_slot_warnings(user)
    note = Notification.objects.get(kind="capacity_low")
    assert "active jobs below 2" in note.message
    assert "idle slots above 2" in note.message
    add_job(user, fresh)
    second = add_job(user, fresh)
    evaluate_slot_warnings(user)
    assert not SlotMonitorState.objects.get(user=user, activity="manufacturing").warning_active
    second.delete()
    evaluate_slot_warnings(user)
    assert Notification.objects.filter(kind="capacity_low").count() == 2


def test_idle_threshold_is_strict_and_paused_jobs_do_not_count_as_idle(user, fresh):
    settings(user, manufacturing_max_idle=1)
    add_job(user, fresh)
    add_job(user, fresh, status="paused")
    paused = add_job(user, fresh, status="paused")
    evaluate_slot_warnings(user)
    assert not Notification.objects.exists()  # Exactly one idle slot is allowed.
    paused.delete()
    evaluate_slot_warnings(user)
    assert "2 idle / 4 configured slots" in Notification.objects.get().message


def test_ready_jobs_are_nonproducing_and_warning_explains_delivery_is_needed(user, fresh):
    settings(user, reaction_max_idle=1)
    add_job(user, fresh, activity=11, status="ready")
    row = capacity_status(user)["rows"][1]
    assert row["active"] == 0 and row["ready"] == 1 and row["idle"] == 4
    evaluate_slot_warnings(user)
    assert "Ready for delivery: 1" in Notification.objects.get().message


@pytest.mark.parametrize("unavailable", ["never", "stale", "error", "revoked"])
def test_missing_or_unreliable_imports_never_create_idle_slot_alerts(user, fresh, unavailable):
    settings(user, manufacturing_min_active=4)
    if unavailable == "never":
        fresh.last_jobs_sync = None
    elif unavailable == "stale":
        fresh.last_jobs_sync = timezone.now() - timedelta(minutes=31)
    elif unavailable == "error":
        fresh.last_error = "ESI network timeout"
    else:
        fresh.enabled = False
    fresh.save()
    evaluate_slot_warnings(user)
    assert not Notification.objects.exists()


def test_disconnected_second_source_prevents_a_false_warning_from_other_fresh_sources(user, fresh):
    settings(user, manufacturing_min_active=4)
    InventorySource.objects.create(
        user=user, kind="corporation", owner_id=98000002, name="Reconnect me", enabled=False
    )
    evaluate_slot_warnings(user)
    assert not Notification.objects.exists()


def test_notification_preference_can_disable_capacity_alerts_then_enable_them(user, fresh):
    settings(user, reaction_min_active=4)
    preferences = NotificationPreferences.objects.create(user=user, capacity_low=False)
    evaluate_slot_warnings(user)
    assert not Notification.objects.exists()
    preferences.capacity_low = True
    preferences.save()
    evaluate_slot_warnings(user)
    assert Notification.objects.get().title == "Reactions slot warning"


def test_existing_periodic_watch_task_also_checks_users_with_slots_and_no_inventory_targets(user):
    settings(user)
    with patch("aa_industry_planner.services.capacity.evaluate_slot_warnings") as evaluate:
        evaluate_all_watches.run()
    evaluate.assert_called_once_with(user)


def test_configuration_validates_thresholds_and_keeps_other_defaults(user, client):
    preferences = UserPreferences.objects.create(user=user, reaction_runs=50)
    values = dict(
        manufacturing_slots=4,
        reaction_slots=5,
        manufacturing_min_active=2,
        reaction_max_idle=0,
        sync_error_grace_minutes=45,
    )
    invalid = OperationsForm(dict(values, reaction_max_idle=6), instance=preferences)
    assert not invalid.is_valid()
    assert "reaction_max_idle" in invalid.errors
    client.force_login(user)
    response = client.post(
        reverse("aa_industry_planner:configuration"), dict(values, form="operations")
    )
    assert response.status_code == 302
    preferences.refresh_from_db()
    assert (
        preferences.manufacturing_slots,
        preferences.reaction_max_idle,
        preferences.sync_error_grace_minutes,
    ) == (4, 0, 45)
    assert preferences.reaction_runs == 50


@pytest.mark.parametrize(
    ("badge", "count"), [("both", 5), ("manufacturing", 2), ("reaction", 3), ("off", None)]
)
def test_configuration_controls_industry_sidebar_badge(
    user, other_user, fresh, client, badge, count
):
    assert settings(user).sidebar_slot_badge == "both"
    other_settings = settings(other_user, sidebar_slot_badge="reaction")
    add_job(user, fresh)
    add_job(user, fresh, status="paused")
    add_job(user, fresh, activity=9)
    add_job(user, fresh, activity=11, status="ready")
    add_job(user, fresh, installer=99999999)
    client.force_login(user)
    response = client.post(
        reverse("aa_industry_planner:configuration"),
        {
            "form": "operations",
            "manufacturing_slots": 4,
            "reaction_slots": 4,
            "sync_error_grace_minutes": 30,
            "sidebar_slot_badge": badge,
        },
        follow=True,
    )
    assert response.status_code == 200
    assert b'name="sidebar_slot_badge"' in response.content
    assert UserPreferences.objects.get(user=user).sidebar_slot_badge == badge
    other_settings.refresh_from_db()
    assert other_settings.sidebar_slot_badge == "reaction"
    entry = next(
        item
        for item in render_menu(response.wsgi_request)
        if item.menu_item.text == "Industry Planner"
    )
    assert entry.count == count
    assert ("badge text-bg-primary" in entry.html) == (count is not None)


def test_sidebar_hides_badge_when_selected_slots_are_all_in_use(user, fresh, client):
    UserPreferences.objects.create(
        user=user, manufacturing_slots=1, reaction_slots=4, sidebar_slot_badge="manufacturing"
    )
    add_job(user, fresh)
    add_job(user, fresh, status="paused")
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:dashboard"))
    entry = next(
        item
        for item in render_menu(response.wsgi_request)
        if item.menu_item.text == "Industry Planner"
    )
    assert entry.count is None


def test_character_management_lives_in_configuration_and_inventory_keeps_counts(
    user, client, fresh
):
    client.force_login(user)
    config = client.get(reverse("aa_industry_planner:configuration")).content
    inventory = client.get(reverse("aa_industry_planner:inventory")).content
    assert b"Characters &amp; corporation sources" in config
    assert reverse("aa_industry_planner:connect_character").encode() in config
    assert reverse("aa_industry_planner:source_action", args=[fresh.pk]).encode() in config
    assert reverse("aa_industry_planner:connect_character").encode() not in inventory
    assert reverse("aa_industry_planner:source_action", args=[fresh.pk]).encode() not in inventory
    assert b"1 character connected" in inventory
    assert b"Manage characters &amp; sources" in inventory
    response = client.post(
        reverse("aa_industry_planner:source_action", args=[fresh.pk]),
        {"action": "include", "return_to": "https://example.com"},
    )
    assert response.url == reverse("aa_industry_planner:configuration")


def test_saved_plan_forecast_uses_current_slot_settings_without_recalculating_materials(
    user, client, recipes
):
    from aa_industry_planner.services.planning import create_plan

    preferences = UserPreferences.objects.create(user=user, manufacturing_slots=1, reaction_slots=2)
    plan = create_plan(user, 1, 1, use_inventory=False)
    snapshot = plan.snapshot.copy()
    client.force_login(user)
    url = reverse("aa_industry_planner:plan", args=[plan.pk])
    first = client.get(url)
    assert first.status_code == 200
    assert b"Production timing" in first.content and b"Estimated step finish" in first.content
    assert b"Each batch can start when a slot is free and its inputs are available" in first.content
    first_duration = first.context["timing"]["duration"]
    preferences.manufacturing_slots = 2
    preferences.save()
    second = client.get(url)
    assert second.context["timing"]["duration"] < first_duration
    plan.refresh_from_db()
    assert plan.snapshot == snapshot


def test_saved_plan_forecast_starts_a_branch_before_the_shared_reaction_step_finishes(
    user, client, recipes
):
    from aa_industry_planner.services.planning import create_plan

    UserPreferences.objects.create(user=user, manufacturing_slots=20, reaction_slots=1)
    plan = create_plan(user, 1, 1, use_inventory=False)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:plan", args=[plan.pk]))
    timings = {step.item_type_id: step.timing for step in response.context["steps"]}
    assert response.status_code == 200
    first, second = sorted(timings[tid]["start_at"] for tid in [2, 3])
    assert first < timings[4]["finish_at"] == second
    assert response.context["timing"]["slots"] == {"manufacturing": 20, "reaction": 1}
