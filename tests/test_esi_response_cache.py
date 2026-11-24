from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx2
import pytest
from django.core.cache import cache
from django.utils import timezone
from esi.exceptions import HTTPNotModified
from esi.openapi_clients import EsiOperation

from aa_industry_planner.models import Asset, IndustryJob, Notification
from aa_industry_planner.providers import ESI, esi
from aa_industry_planner.tasks import refresh_source


def cached_operation(name, pages, *, paginated=False, parameters=None):
    """Use django-esi's actual response/ETag caches without making HTTP requests."""
    spec = SimpleNamespace(
        operationId=name,
        parameters=[SimpleNamespace(name="page")] if paginated else [],
        security=[{"OAuth2": []}],
        extensions={},
        tags=["CacheTest"],
    )
    api = SimpleNamespace(
        app_name="industry-cache-regression",
        createRequest=lambda _: SimpleNamespace(
            _process_request=lambda response: (response.headers, response.json())
        ),
    )
    operation = EsiOperation(("get", f"/test/{name}", spec, None), api)
    operation(**(parameters or {}))
    for page, payload in enumerate(pages, 1):
        if paginated:
            operation._kwargs["page"] = page
        headers = {
            "ETag": f'"{name}-{page}"',
            "X-Pages": str(len(pages)),
            "Date": format_datetime(timezone.now(), usegmt=True),
            "Cache-Control": "public, max-age=3600",
        }
        response = httpx2.Response(200, headers=headers, json=payload)
        cache.set(operation._cache_key(), response, 3600)
        cache.set(operation._etag_key(), headers["ETag"], 3600)
    operation._make_request = Mock(
        side_effect=AssertionError("Valid response cache must avoid HTTP")
    )
    return operation


def test_default_django_esi_mode_reproduces_not_modified_for_all_cached_pages():
    operation = cached_operation("AllCached", [[{"id": 1}], [{"id": 2}]], paginated=True)
    with pytest.raises(HTTPNotModified):
        operation.results()
    operation._make_request.assert_not_called()


@pytest.mark.parametrize("kind", ["character", "corporation"])
@pytest.mark.parametrize("dataset", ["assets", "jobs"])
def test_imports_return_every_cached_page_despite_existing_etags(kind, dataset):
    source = SimpleNamespace(kind=kind, owner_id=90000001, token=object())
    owner = "corporation_id" if kind == "corporation" else "character_id"
    operation_name = f"Get{'CorporationsCorporation' if kind == 'corporation' else 'CharactersCharacter'}Id{'Assets' if dataset == 'assets' else 'IndustryJobs'}"
    params = {owner: source.owner_id}
    if dataset == "jobs":
        params["include_completed"] = True
    rows = [[{"id": 1}], [{"id": 2}]]
    operation = cached_operation(operation_name, rows, paginated=True, parameters=params)
    client = SimpleNamespace(
        **{
            "Assets" if dataset == "assets" else "Industry": SimpleNamespace(
                **{operation_name: operation}
            )
        }
    )
    with patch.object(esi, "_client", client):
        for _ in range(2):
            assert getattr(ESI(), dataset)(source) == [{"id": 1}, {"id": 2}]
    operation._make_request.assert_not_called()


@pytest.mark.parametrize(
    "method,tag,name,payload,args,parameters",
    [
        (
            "character",
            "Character",
            "GetCharactersDetail",
            {"corporation_id": 98000001},
            [90000001],
            {"character_id": 90000001},
        ),
        (
            "roles",
            "Character",
            "GetCharactersCharacterIdRoles",
            {"roles": ["Director"]},
            [SimpleNamespace(character_id=90000001)],
            {"character_id": 90000001},
        ),
        ("indices", "Industry", "GetIndustrySystems", [{"solar_system_id": 30000142}], [], {}),
        ("prices", "Market", "GetMarketsPrices", [{"type_id": 34, "adjusted_price": 5}], [], {}),
    ],
)
def test_identity_role_and_public_reads_return_cached_payloads(
    method, tag, name, payload, args, parameters
):
    operation = cached_operation(name, [payload], parameters=parameters)
    client = SimpleNamespace(**{tag: SimpleNamespace(**{name: operation})})
    with patch.object(esi, "_client", client):
        assert getattr(ESI(), method)(*args) == payload
    operation._make_request.assert_not_called()


def test_worker_imports_cached_assets_and_changed_jobs_without_retry_or_losing_stock(user, source):
    owner = {"character_id": source.owner_id}
    assets = [{"item_id": 42, "type_id": 34, "quantity": 11, "location_id": 60000001}]
    assets_op = cached_operation("Assets", [assets], paginated=True, parameters=owner)
    now = timezone.now()
    jobs = [
        {
            "job_id": 54321,
            "installer_id": source.owner_id,
            "activity_id": 5,
            "blueprint_type_id": 101,
            "facility_id": 1000000000001,
            "runs": 9,
            "status": "active",
            "start_date": now.isoformat(),
            "end_date": (now + timezone.timedelta(days=6)).isoformat(),
        }
    ]
    jobs_op = cached_operation("Jobs", [jobs], parameters=dict(owner, include_completed=True))
    # Jobs have a fresh complete payload, while assets already match a previous ETag.
    cache.delete(jobs_op._etag_key())
    client = SimpleNamespace(
        Assets=SimpleNamespace(GetCharactersCharacterIdAssets=assets_op),
        Industry=SimpleNamespace(GetCharactersCharacterIdIndustryJobs=jobs_op),
    )
    Asset.objects.create(source=source, item_id=42, type_id=34, quantity=8, location_id=60000001)
    source.last_error = "ESI refresh failed before this fix."
    source.save()
    with (
        patch.object(esi, "_client", client),
        patch.object(
            refresh_source, "retry", side_effect=AssertionError("Cached imports must not retry")
        ),
        patch("aa_industry_planner.tasks.resolve_entity_names.delay"),
    ):
        refresh_source.run(source.pk)
        refresh_source.run(source.pk)
    source.refresh_from_db()
    assert source.enabled
    assert source.last_error == ""
    assert source.last_assets_sync and source.last_jobs_sync
    assert Asset.objects.get(source=source, item_id=42).quantity == 11
    assert IndustryJob.objects.get(source=source, job_id=54321).runs == 9
    assert not Notification.objects.filter(user=user, kind="sync_failed").exists()
    assets_op._make_request.assert_not_called()
    jobs_op._make_request.assert_not_called()


@pytest.mark.parametrize("dataset", ["assets", "jobs"])
def test_valid_empty_cached_response_is_returned_as_empty_dataset(dataset):
    name = (
        "GetCharactersCharacterIdAssets"
        if dataset == "assets"
        else "GetCharactersCharacterIdIndustryJobs"
    )
    source = SimpleNamespace(kind="character", owner_id=90000001, token=object())
    params = {"character_id": source.owner_id}
    if dataset == "jobs":
        params["include_completed"] = True
    operation = cached_operation(name, [[]], paginated=True, parameters=params)
    client = SimpleNamespace(
        **{"Assets" if dataset == "assets" else "Industry": SimpleNamespace(**{name: operation})}
    )
    with patch.object(esi, "_client", client):
        assert getattr(ESI(), dataset)(source) == []
    operation._make_request.assert_not_called()


def test_expired_body_cache_refetches_full_data_without_sending_old_etag():
    source = SimpleNamespace(kind="character", owner_id=90000001, token=object())
    operation = cached_operation(
        "ExpiredAssets", [[{"id": 1}]], paginated=True, parameters={"character_id": source.owner_id}
    )
    # The body cache expires before django-esi's seven-day ETag entry.
    cache.delete(operation._cache_key())
    new_response = httpx2.Response(
        200,
        headers={
            "ETag": '"new-assets"',
            "X-Pages": "1",
            "Date": format_datetime(timezone.now(), usegmt=True),
            "Cache-Control": "public, max-age=3600",
        },
        json=[{"id": 2}],
    )
    operation._make_request = Mock(
        return_value=(new_response.headers, new_response.json(), new_response)
    )
    client = SimpleNamespace(Assets=SimpleNamespace(GetCharactersCharacterIdAssets=operation))
    with patch.object(esi, "_client", client):
        assert ESI().assets(source) == [{"id": 2}]
        assert ESI().assets(source) == [{"id": 2}]
    operation._make_request.assert_called_once()
    assert operation._make_request.call_args.args[1] is None
