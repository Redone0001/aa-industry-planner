import json
from pathlib import Path
from unittest.mock import Mock, patch

from aa_industry_planner.providers import ESI, esi, required_scopes


def test_operations_exist_in_reviewed_ccp_contract():
    contract = json.loads((Path(__file__).parent / "fixtures" / "esi_contract.json").read_text())
    assert set(esi._operations) == set(contract["operations"])
    assert contract["operations"]["GetCharactersDetail"]["tags"] == ["Character"]
    assert contract["operations"]["GetCharactersCharacterIdAssets"]["security"] == [
        {"OAuth2": ["esi-assets.read_assets.v1"]}
    ]


def test_asset_import_uses_all_pages_and_passes_token_object():
    provider = ESI()
    source = Mock(kind="corporation", owner_id=98000001, token=object())
    client = Mock()
    operation = client.Assets.GetCorporationsCorporationIdAssets.return_value
    operation.results.return_value = [{"item_id": 1}]
    with patch.object(esi, "_client", client):
        assert provider.assets(source) == [{"item_id": 1}]
    client.Assets.GetCorporationsCorporationIdAssets.assert_called_once_with(
        corporation_id=98000001, token=source.token
    )
    operation.results.assert_called_once_with(use_etag=False)


def test_job_import_requests_completed_jobs():
    source = Mock(kind="character", owner_id=90000001, token=object())
    client = Mock()
    client.Industry.GetCharactersCharacterIdIndustryJobs.return_value.results.return_value = []
    with patch.object(esi, "_client", client):
        assert ESI().jobs(source) == []
    client.Industry.GetCharactersCharacterIdIndustryJobs.assert_called_once_with(
        character_id=90000001, include_completed=True, token=source.token
    )


def test_requested_scopes_match_the_reviewed_ccp_endpoint_contract():
    contract = json.loads((Path(__file__).parent / "fixtures" / "esi_contract.json").read_text())
    for kind, operations in {
        "character": ["GetCharactersCharacterIdAssets", "GetCharactersCharacterIdIndustryJobs"],
        "corporation": [
            "GetCorporationsCorporationIdAssets",
            "GetCorporationsCorporationIdIndustryJobs",
            "GetCharactersCharacterIdRoles",
        ],
    }.items():
        expected = {
            scope
            for operation in operations
            for security in contract["operations"][operation]["security"]
            for scope in security["OAuth2"]
        }
        assert set(required_scopes(kind)) == expected
