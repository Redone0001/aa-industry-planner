from datetime import UTC, datetime, timedelta
from math import ceil

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone
from eveuniverse.models import EveEntity

from aa_industry_planner.models import IndustryJob, InventorySource, UserPreferences
from aa_industry_planner.services.notifications import discord_embed, notify_job_finished

from .test_services import make_job

pytestmark = pytest.mark.django_db


def test_all_jobs_dashboard_scopes_slots_and_ranks_30_day_runs(
    user, source, other_user, types, client, monkeypatch
):
    now = datetime(2026, 11, 27, 8, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    source.last_jobs_sync = now
    source.save(update_fields=["last_jobs_sync"])
    user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
    UserPreferences.objects.create(user=user, manufacturing_slots=4, reaction_slots=1)
    corp = InventorySource.objects.create(
        user=user, token=source.token, kind="corporation", owner_id=98000001, name="Industrial Corp"
    )
    inactive = InventorySource.objects.create(
        user=user, kind="character", owner_id=42, name="Unavailable", enabled=False
    )
    foreign = InventorySource.objects.create(
        user=other_user, kind="manual", name="Other user's jobs"
    )
    colleague = EveEntity.objects.create(pk=90000991, name="Rin Talos", category="character")
    soon = now + timedelta(hours=1)
    last = now + timedelta(hours=27, milliseconds=1)
    jobs = [
        make_job(user, source, job_id=1, runs=4, end_date=last),
        make_job(user, source, job_id=2, runs=2, status="paused", end_date=soon),
        make_job(user, source, job_id=3, runs=1, end_date=now),
        make_job(user, source, job_id=4, runs=3, activity_id=9, product_type_id=4, end_date=soon),
        make_job(
            user,
            corp,
            job_id=5,
            runs=12,
            activity_id=11,
            product_type_id=4,
            installer_id=source.owner_id,
            end_date=soon,
        ),
        make_job(
            user,
            corp,
            job_id=6,
            runs=20,
            product_type_id=2,
            installer_id=colleague.pk,
            status="ready",
        ),
        make_job(
            user,
            corp,
            job_id=7,
            runs=40,
            activity_id=11,
            product_type_id=3,
            installer_id=colleague.pk,
        ),
    ]
    make_job(
        user, source, job_id=8, runs=900, start_date=now - timedelta(days=31), status="delivered"
    )
    make_job(user, source, job_id=9, runs=900, status="cancelled")
    make_job(user, inactive, job_id=10, runs=900, product_type_id=5)
    make_job(other_user, foreign, job_id=11, runs=900, product_type_id=5)
    make_job(
        user,
        source,
        job_id=12,
        runs=5,
        activity_id=11,
        product_type_id=None,
        blueprint_type_id=104,
        status="ready",
    )
    client.force_login(user)

    response = client.get(reverse("aa_industry_planner:jobs"))
    assert response.status_code == 200
    assert b"<th>Installer</th>" in response.content and b"Rin Talos" in response.content
    assert b'name="status"' not in response.content and b"90000991" not in response.content
    assert response.context["preview_job"].installer_name == "builder"
    assert [job.pk for job in response.context["jobs"] if job.job_id in {10, 11}] == []
    operations = response.context["operations"]
    manufacturing, reaction = operations["rows"]
    assert [manufacturing[k] for k in ("slots", "active", "paused", "past_finish", "idle")] == [
        4,
        1,
        1,
        1,
        1,
    ]
    assert [reaction[k] for k in ("active", "ready", "used_percent", "idle")] == [2, 1, 200, 0]
    assert [(item["name"], item["runs"]) for item in manufacturing["ranking"]] == [
        ("Component A", 20),
        ("Final product", 7),
    ]
    assert [(item["name"], item["runs"]) for item in reaction["ranking"]] == [
        ("Component B", 40),
        ("Shared material", 15),
        ("Shared blueprint", 5),
    ]
    assert operations["burndown"] == {
        "now": int(now.timestamp() * 1000),
        "manufacturing": [ceil(last.timestamp() * 1000)],
        "reaction": [ceil(soon.timestamp() * 1000)] * 2,
    }
    note = notify_job_finished(jobs[5])
    assert "Installer: Rin Talos" in note.message
    assert "Installer: **Rin Talos**" in discord_embed(note)["description"]

    IndustryJob.objects.bulk_create(
        [
            IndustryJob(
                user=user,
                source=source,
                job_id=100 + i,
                installer_id=source.owner_id,
                activity_id=3,
                blueprint_type_id=101,
                facility_id=1,
                runs=1,
                status="delivered",
                start_date=now,
                end_date=now,
            )
            for i in range(100)
        ]
    )
    paginated = client.get(reverse("aa_industry_planner:jobs"), {"page": 2, "status": "paused"})
    assert paginated.context["jobs"].number == 2
    assert paginated.context["operations"] == operations


def test_jobs_dashboard_shows_when_data_is_missing(user, client):
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:jobs"))
    assert response.status_code == 200
    assert b"Slot utilisation appears after the first job import." in response.content
    assert b"No jobs imported from your connected sources." in response.content
    assert b"data-job-burndown" not in response.content
