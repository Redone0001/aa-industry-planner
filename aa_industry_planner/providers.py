"""Use django-esi's token refresh, pagination, ETag cache and rate limiting."""

from django.conf import settings
from esi.openapi_clients import ESIClientProvider

from . import __version__

CHARACTER_SCOPES = ["esi-assets.read_assets.v1", "esi-industry.read_character_jobs.v1"]
CORPORATION_SCOPES = [
    "esi-assets.read_corporation_assets.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-characters.read_corporation_roles.v1",
]

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
    ],
)


def unpack(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [unpack(row) for row in value]
    return value


class ESI:
    def character(self, character_id):
        return unpack(esi.client.Character.GetCharactersDetail(character_id=character_id).result())

    def roles(self, token):
        return unpack(
            esi.client.Character.GetCharactersCharacterIdRoles(
                character_id=token.character_id, token=token
            ).result()
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
        return unpack(operation.results())

    def jobs(self, source):
        if source.kind == "corporation":
            operation = esi.client.Industry.GetCorporationsCorporationIdIndustryJobs(
                corporation_id=source.owner_id, include_completed=True, token=source.token
            )
        else:
            operation = esi.client.Industry.GetCharactersCharacterIdIndustryJobs(
                character_id=source.owner_id, include_completed=True, token=source.token
            )
        return unpack(operation.results())

    def indices(self):
        return unpack(esi.client.Industry.GetIndustrySystems().result())

    def prices(self):
        return unpack(esi.client.Market.GetMarketsPrices().result())
