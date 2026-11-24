import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import resolve, reverse
from django.utils import timezone

from aa_industry_planner.auth_hooks import IndustryMenu, url_hook
from aa_industry_planner.models import InventorySource, ManualStock, Notification
from aa_industry_planner.services.planning import create_plan

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name", ["dashboard", "plans", "inventory", "jobs", "watches", "configuration", "notifications"]
)
def test_main_pages_render_real_aa_base(user, client, name):
    client.force_login(user)
    response = client.get(reverse(f"aa_industry_planner:{name}"))
    assert response.status_code == 200
    assert b"Industry Planner" in response.content


def test_plan_tree_renders_shared_steps_and_input_list(user, client, recipes):
    production = create_plan(user, 1, 1)
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    assert response.status_code == 200
    assert b"Shared material" in response.content
    assert b"Raw input" in response.content
    assert b"System index" in response.content


def test_cross_user_plan_inventory_and_notifications_are_private(user, other_user, client, recipes):
    production = create_plan(other_user, 1, 1, name="SECRET PLAN")
    source = InventorySource.objects.create(user=other_user, kind="manual", name="SECRET SOURCE")
    item = ManualStock.objects.create(source=source, item_type=recipes[1], quantity=1)
    Notification.objects.create(
        user=other_user,
        kind="inventory_low",
        event_key="private",
        title="SECRET NOTIFICATION",
        message="private",
    )
    client.force_login(user)
    assert client.get(reverse("aa_industry_planner:plan", args=[production.pk])).status_code == 404
    assert (
        client.post(reverse("aa_industry_planner:delete_manual", args=[item.pk])).status_code == 404
    )
    assert b"SECRET SOURCE" not in client.get(reverse("aa_industry_planner:inventory")).content
    assert (
        b"SECRET NOTIFICATION"
        not in client.get(reverse("aa_industry_planner:notifications")).content
    )


def test_module_permission_enforced(other_user, client):
    other_user.user_permissions.clear()
    client.force_login(other_user)
    assert client.get(reverse("aa_industry_planner:dashboard")).status_code == 403


def test_corporation_permission_is_checked_before_requesting_sso_scopes(user, client):
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:connect_corporation"))
    assert response.status_code == 403


def test_post_only_mutations_and_csrf(user, client, recipes):
    production = create_plan(user, 1, 1)
    client.force_login(user)
    url = reverse("aa_industry_planner:archive_plan", args=[production.pk])
    assert client.get(url).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(url).status_code == 403


def test_type_search_respects_active_modeltranslation(user, client, types):
    types[1].name_fr_fr = "Produit final"
    types[1].save()
    from django.utils.translation import override

    client.force_login(user)
    with override("fr-fr"):
        data = client.get(reverse("aa_industry_planner:type_search"), {"q": "Produit"}).json()
    assert data["results"][0]["name"] == "Produit final"


def test_inventory_post_uses_absolute_quantity(user, client, types, assigned_facility):
    client.force_login(user)
    for quantity in [5, 8]:
        response = client.post(
            reverse("aa_industry_planner:inventory"),
            {
                "item_type": "1 · Final product",
                "quantity": quantity,
                "location": "Hangar",
                "facility": assigned_facility.pk,
            },
        )
        assert response.status_code == 302
    assert ManualStock.objects.count() == 1
    assert ManualStock.objects.get().quantity == 8


def test_facility_monitor_requires_separate_permission(user, client, facility):
    client.force_login(user)
    url = reverse("aa_industry_planner:facilities")
    assert client.get(url).status_code == 403
    user.user_permissions.add(Permission.objects.get(codename="manage_facilities"))
    assert client.get(url).status_code == 200


def test_mark_read_only_updates_current_users_notes(user, other_user, client):
    note = Notification.objects.create(
        user=user, event_key="one", kind="job_finished", title="Done", message="Done"
    )
    other = Notification.objects.create(
        user=other_user, event_key="one", kind="job_finished", title="Done", message="Done"
    )
    client.force_login(user)
    client.post(reverse("aa_industry_planner:mark_read"))
    note.refresh_from_db()
    other.refresh_from_db()
    assert note.read_at <= timezone.now()
    assert other.read_at is None


def test_auth_hooks_resolve_single_industry_prefix():
    from django.urls import URLResolver

    hook = url_hook()
    assert isinstance(hook.include_pattern, URLResolver)
    assert hook.include_pattern.resolve("industry/").url_name == "dashboard"
    assert IndustryMenu().url_name == "aa_industry_planner:dashboard"
    assert resolve("/industry/").namespace == "aa_industry_planner"
