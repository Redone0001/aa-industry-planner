"""Use django-esi's token refresh, pagination, response cache and rate limiting."""

from email.utils import parsedate_to_datetime

from django.conf import settings
from esi.openapi_clients import ESIClientProvider

from . import __version__

CHARACTER_SCOPES = ["esi-assets.read_assets.v1", "esi-industry.read_character_jobs.v1"]
CORPORATION_SCOPES = [
    "esi-assets.read_corporation_assets.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-characters.read_corporation_roles.v1",
]
STRUCTURE_NAME_SCOPE = "esi-universe.read_structures.v1"


def required_scopes(kind):
    if kind == "corporation":
        return CORPORATION_SCOPES
    if kind == "character":
        return CHARACTER_SCOPES
    if kind == "structure":
        return [STRUCTURE_NAME_SCOPE]
    return []


def missing_scopes(token, kind):
    granted = {scope.name for scope in token.scopes.all()} if token else set()
    return [scope for scope in required_scopes(kind) if scope not in granted]


esi = ESIClientProvider(
    compatibility_date="2026-11-19",
    ua_appname="aa-industry-planner",
    ua_version=__version__,
    ua_url=getattr(settings, "SITE_URL", None),
    operations=[
        "GetCharactersDetail",
        "GetCharactersCharacterIdRoles",
        "GetCharactersCharacterIdAssets",
        "GetCorporationsCorporationIdAssets",
        "GetCharactersCharacterIdIndustryJobs",
        "GetCorporationsCorporationIdIndustryJobs",
        "GetIndustrySystems",
        "GetMarketsPrices",
        "GetUniverseStructuresStructureId",
    ],
)


def unpack(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [unpack(row) for row in value]
    return value


def public_cache_metadata(response):
    result = {}
    for header, field in [("Last-Modified", "source_modified_at"), ("Expires", "cache_expires_at")]:
        try:
            value = response.headers.get(header)
            result[field] = parsedate_to_datetime(value) if value else None
        except (TypeError, ValueError, OverflowError):
            result[field] = None
    return result


class ESI:
    # These consumers need complete bodies, even when unchanged. ETag mode raises
    # HTTPNotModified for cached responses too. Disable conditional ETag reads;
    # keep response caching, cache expiry, pagination and rate limits enabled.
    def character(self, character_id):
        return unpack(
            esi.client.Character.GetCharactersDetail(character_id=character_id).result(
                use_etag=False
            )
        )

    def structure(self, structure_id, token):
        return unpack(
            esi.client.Universe.GetUniverseStructuresStructureId(
                structure_id=structure_id, token=token
            ).result(use_etag=False)
        )

    def roles(self, token):
        return unpack(
            esi.client.Character.GetCharactersCharacterIdRoles(
                character_id=token.character_id, token=token
            ).result(use_etag=False)
        )

    def assets(self, source):
        if source.kind == "corporation":
            operation = esi.client.Assets.GetCorporationsCorporationIdAssets(
                corporation_id=source.owner_id, token=source.token
            )
        else:
            operation = esi.client.Assets.GetCharactersCharacterIdAssets(
                character_id=source.owner_id, token=source.token
            )
        return unpack(operation.results(use_etag=False))

    def jobs(self, source):
        if source.kind == "corporation":
            operation = esi.client.Industry.GetCorporationsCorporationIdIndustryJobs(
                corporation_id=source.owner_id, include_completed=True, token=source.token
            )
        else:
            operation = esi.client.Industry.GetCharactersCharacterIdIndustryJobs(
                character_id=source.owner_id, include_completed=True, token=source.token
            )
        return unpack(operation.results(use_etag=False))

    def indices(self):
        data, response = esi.client.Industry.GetIndustrySystems().result(
            use_etag=False, return_response=True
        )
        self.indices_metadata = public_cache_metadata(response)
        return unpack(data)

    def prices(self):
        data, response = esi.client.Market.GetMarketsPrices().result(
            use_etag=False, return_response=True
        )
        self.prices_metadata = public_cache_metadata(response)
        return unpack(data)
