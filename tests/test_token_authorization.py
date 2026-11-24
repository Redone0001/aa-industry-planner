from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone
from esi.models import CallbackRedirect, Scope, Token

from aa_industry_planner.models import InventorySource
from aa_industry_planner.providers import CHARACTER_SCOPES, CORPORATION_SCOPES
from aa_industry_planner.services.inventory import authorized_sources, source_overview
from aa_industry_planner.services.sync import validate_source


def configure_kind(user, source, kind):
    if kind == "corporation":
        user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
        source.kind, source.owner_id = kind, 98000001
        source.save()
    return CORPORATION_SCOPES if kind == "corporation" else CHARACTER_SCOPES


def callback(client, token, kind):
    CallbackRedirect.objects.create(
        session_key=client.session.session_key,
        state="local-test-state",
        url=reverse(f"aa_industry_planner:connect_{kind}"),
        token=token,
    )


@pytest.mark.parametrize("kind", ["character", "corporation"])
def test_sso_authorization_url_requests_exact_required_scopes(user, client, kind):
    if kind == "corporation":
        user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
    client.force_login(user)
    response = client.get(reverse(f"aa_industry_planner:connect_{kind}"))
    assert response.status_code == 302
    params = parse_qs(urlparse(response.url).query)
    expected = CORPORATION_SCOPES if kind == "corporation" else CHARACTER_SCOPES
    assert set(params["scope"][0].split()) == set(expected)
    assert not InventorySource.objects.filter(user=user).exists()


@pytest.mark.parametrize(
    "kind,missing",
    [
        *[("character", scope) for scope in CHARACTER_SCOPES],
        *[("corporation", scope) for scope in CORPORATION_SCOPES],
    ],
)
def test_callback_rejects_each_missing_scope_before_connecting(user, source, client, kind, missing):
    configure_kind(user, source, kind)
    token = source.token
    source.delete()
    token.scopes.remove(Scope.objects.get(name=missing))
    client.force_login(user)
    callback(client, token, kind)
    with (
        patch("aa_industry_planner.tasks.refresh_source.delay") as queued,
        patch("aa_industry_planner.providers.ESI") as provider,
    ):
        response = client.get(reverse(f"aa_industry_planner:connect_{kind}"))
    assert response.status_code == 403
    assert not InventorySource.objects.filter(user=user).exists()
    queued.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("kind", ["character", "corporation"])
def test_valid_callback_connects_the_selected_token_with_extra_scopes(user, source, client, kind):
    configure_kind(user, source, kind)
    token = source.token
    source.delete()
    client.force_login(user)
    callback(client, token, kind)
    api = Mock()
    api.character.return_value = {"corporation_id": 98000001}
    api.roles.return_value = {"roles": ["Director"]}
    with (
        patch("aa_industry_planner.providers.ESI", return_value=api),
        patch("aa_industry_planner.tasks.refresh_source.delay") as queued,
    ):
        response = client.get(reverse(f"aa_industry_planner:connect_{kind}"))
    assert response.status_code == 302
    connected = InventorySource.objects.get(user=user, kind=kind)
    assert connected.token_id == token.pk
    queued.assert_called_once_with(connected.pk)


@pytest.mark.parametrize("kind", ["character", "corporation"])
def test_missing_scope_is_not_counted_as_connected_and_is_reported(user, source, client, kind):
    required = configure_kind(user, source, kind)
    missing = required[0]
    source.token.scopes.remove(Scope.objects.get(name=missing))
    assert not authorized_sources(user).filter(pk=source.pk).exists()
    overview = source_overview(user)
    assert overview[f"connected_{'characters' if kind == 'character' else 'corporations'}"] == 0
    assert overview["sources"][0].missing_scopes == [missing]
    client.force_login(user)
    for page in ["inventory", "jobs"]:
        content = client.get(reverse(f"aa_industry_planner:{page}")).content
        assert b"Missing ESI permissions" in content
        assert missing.encode() in content


def test_incomplete_token_is_not_an_existing_token_choice(user, source, client):
    source.token.scopes.remove(Scope.objects.get(name=CHARACTER_SCOPES[1]))
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:connect_character"))
    assert response.status_code == 302
    assert set(parse_qs(urlparse(response.url).query)["scope"][0].split()) == set(CHARACTER_SCOPES)


def test_existing_token_selection_accepts_all_scopes_without_new_sso(user, source, client):
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:connect_character"))
    assert response.status_code == 200
    assert list(response.context["tokens"]) == [source.token]
    with patch("aa_industry_planner.tasks.refresh_source.delay") as queued:
        response = client.post(
            reverse("aa_industry_planner:connect_character"), {"_token": source.token_id}
        )
    assert response.status_code == 302
    queued.assert_called_once_with(source.pk)


@pytest.mark.parametrize("kind", ["character", "corporation"])
def test_import_uses_the_rotated_token_instance_without_refreshing_twice(user, source, kind):
    configure_kind(user, source, kind)
    api = Mock()
    api.character.return_value = {"corporation_id": 98000001}
    api.roles.return_value = {"roles": ["Director"]}
    token = source.token
    Token.objects.filter(pk=token.pk).update(created=timezone.now() - timezone.timedelta(hours=1))
    source = InventorySource.objects.select_related("token").get(pk=source.pk)
    with (
        patch(
            "esi.sso.refresh_token",
            return_value={
                "access_token": "rotated-test-access",
                "refresh_token": "rotated-test-refresh",
            },
        ) as refresh,
        patch(
            "esi.managers.TokenManager.validate_access_token",
            return_value={"owner": token.character_owner_hash},
        ),
    ):
        validate_source(source, api)
        assert source.token.valid_access_token() == "rotated-test-access"
    refresh.assert_called_once_with("test-refresh-token")


@pytest.mark.parametrize("unclaimed", [True, False])
def test_callback_checks_token_ownership_before_claiming(
    user, other_user, source, client, unclaimed
):
    token = source.token
    source.delete()
    token.user = None if unclaimed else other_user
    token.save(update_fields=["user"])
    client.force_login(user)
    callback(client, token, "character")
    with patch("aa_industry_planner.tasks.refresh_source.delay") as queued:
        response = client.get(reverse("aa_industry_planner:connect_character"))
    token.refresh_from_db()
    if unclaimed:
        assert response.status_code == 302
        assert token.user_id == user.pk
        queued.assert_called_once()
    else:
        assert response.status_code == 403
        assert token.user_id == other_user.pk
        assert not InventorySource.objects.filter(user=user).exists()
        queued.assert_not_called()


def test_callback_cannot_connect_an_expired_unrefreshable_token(user, source, client):
    token = source.token
    source.delete()
    Token.objects.filter(pk=token.pk).update(
        created=timezone.now() - timezone.timedelta(hours=1), refresh_token=None
    )
    client.force_login(user)
    callback(client, token, "character")
    with patch("aa_industry_planner.tasks.refresh_source.delay") as queued:
        response = client.get(reverse("aa_industry_planner:connect_character"))
    assert response.status_code == 403
    assert not InventorySource.objects.filter(user=user).exists()
    queued.assert_not_called()
