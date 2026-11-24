from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from eve_sde.models import SolarSystem

from aa_industry_planner.models import Facility, SystemCostIndex

pytestmark = pytest.mark.django_db


@pytest.fixture
def monitor(user, client):
    user.user_permissions.add(Permission.objects.get(codename="manage_facilities"))
    client.force_login(user)
    return client


def test_monitor_groups_structures_with_one_latest_index_strip_and_chart(monitor, facility):
    system = facility.facility.solar_system
    second = Facility.objects.create(
        name="Forge Two", structure_id=1000000000002, solar_system=system
    )
    other_system = SolarSystem.objects.create(id=30000001, name="Another system")
    Facility.objects.create(
        name="Remote forge", structure_id=1000000000003, solar_system=other_system
    )
    now = timezone.now()
    for minutes, cost in [(50, ".0014"), (5, ".0042")]:
        SystemCostIndex.objects.create(
            solar_system=system,
            activity="reaction",
            cost_index=cost,
            recorded_at=now - timedelta(minutes=minutes),
        )
    SystemCostIndex.objects.create(
        solar_system=system,
        activity="manufacturing",
        cost_index=".0023",
        recorded_at=now,
    )
    with CaptureQueriesContext(connection) as queries:
        response = monitor.get(reverse("aa_industry_planner:facilities"))
    assert response.status_code == 200
    systems = response.context["systems"]
    assert [entry["name"] for entry in systems] == ["Another system", "Jita"]
    assert systems[1]["facilities"] == [facility.facility, second]
    assert [index.activity for index in systems[1]["indices"]] == ["manufacturing", "reaction"]
    assert systems[1]["indices"][1].cost_index == Decimal(".0042")
    html = response.content.decode()
    assert html.count("data-index-history=") == 2
    assert html.count('data-index-activity="reaction"') == 1
    assert "Reactions only" in html
    assert reverse("aa_industry_planner:index_history", args=[facility.facility.pk]) not in html
    assert html.count(reverse("aa_industry_planner:system_index_history", args=[system.pk])) == 1
    assert sum('FROM "aa_industry_planner_systemcostindex"' in q["sql"] for q in queries) == 1


def test_grouping_hides_restricted_structures_and_empty_systems(monitor, user, facility):
    restricted = Group.objects.create(name="Restricted industrialists")
    hidden = Facility.objects.create(
        name="Secret structure",
        structure_id=1000000000002,
        solar_system=facility.facility.solar_system,
        notes="Secret fit",
    )
    hidden.allowed_groups.add(restricted)
    secret_system = SolarSystem.objects.create(id=30000001, name="Secret system")
    only_hidden = Facility.objects.create(
        name="Hidden remote structure",
        structure_id=1000000000003,
        solar_system=secret_system,
    )
    only_hidden.allowed_groups.add(restricted)
    Facility.objects.create(
        name="Inactive structure",
        structure_id=1000000000004,
        solar_system=facility.facility.solar_system,
        active=False,
    )
    response = monitor.get(reverse("aa_industry_planner:facilities"))
    assert len(response.context["systems"]) == 1
    assert response.context["systems"][0]["facilities"] == [facility.facility]
    assert b"Secret" not in response.content
    assert b"Inactive structure" not in response.content
    url = reverse("aa_industry_planner:system_index_history", args=[secret_system.pk])
    assert monitor.get(url).status_code == 404
    # Joining an allowed group makes the system and history accessible.
    user.groups.add(restricted)
    assert monitor.get(url).status_code == 200


def test_system_and_legacy_history_share_retention_and_chronological_samples(monitor, facility):
    now = timezone.now()
    system = facility.facility.solar_system
    for days, activity, cost in [
        (91, "reaction", ".01"),
        (2, "reaction", ".0041"),
        (0, "manufacturing", ".03"),
    ]:
        SystemCostIndex.objects.create(
            solar_system=system,
            activity=activity,
            cost_index=cost,
            recorded_at=now - timedelta(days=days),
        )
    system_history = monitor.get(
        reverse("aa_industry_planner:system_index_history", args=[system.pk])
    )
    legacy_history = monitor.get(
        reverse("aa_industry_planner:index_history", args=[facility.facility.pk])
    )
    assert system_history.status_code == 200
    data = system_history.json()
    assert data == legacy_history.json()
    assert data["system"] == "Jita"
    assert [sample["activity"] for sample in data["samples"]] == ["reaction", "manufacturing"]
    assert Decimal(data["samples"][0]["cost_index"]) == Decimal(".0041")
    facility.facility.active = False
    facility.facility.save()
    assert (
        monitor.get(
            reverse("aa_industry_planner:system_index_history", args=[system.pk])
        ).status_code
        == 404
    )


@pytest.mark.parametrize("route", ["index_history", "system_index_history"])
def test_index_history_requires_monitor_permission(user, client, facility, route):
    client.force_login(user)
    pk = facility.facility.pk if route == "index_history" else facility.facility.solar_system_id
    assert client.get(reverse(f"aa_industry_planner:{route}", args=[pk])).status_code == 403
