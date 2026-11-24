from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import Permission
from django.utils import timezone
from pydantic import BaseModel

from aa_industry_planner.models import Asset, IndustryJob
from aa_industry_planner.providers import unpack
from aa_industry_planner.services.inventory import inventory_totals
from aa_industry_planner.services.sync import sync_source


class AssetResponse(BaseModel):
    item_id: int
    type_id: int
    quantity: int
    location_id: int
    location_flag: str | None = None
    is_blueprint_copy: bool | None = None


@pytest.mark.parametrize("kind", ["character", "corporation"])
@pytest.mark.parametrize(
    "copy,flag",
    [(None, "Hangar"), (False, None), (True, "Hangar"), (False, "Hangar"), (None, None)],
)
def test_sdk_optional_asset_values_are_saved_without_nulls(user, source, kind, copy, flag):
    if kind == "corporation":
        user.user_permissions.add(Permission.objects.get(codename="use_corporation_data"))
        source.kind, source.owner_id = kind, 98000001
        source.save()
    response = AssetResponse(
        item_id=42,
        type_id=34,
        quantity=5,
        location_id=60000001,
        is_blueprint_copy=copy,
        location_flag=flag,
    )
    api = Mock()
    api.character.return_value = {"corporation_id": 98000001}
    api.roles.return_value = {"roles": ["Director"]}
    api.assets.return_value = unpack([response])
    now = timezone.now()
    api.jobs.return_value = [
        {
            "job_id": 54321,
            "installer_id": source.token.character_id,
            "activity_id": 5,
            "blueprint_type_id": 101,
            "product_type_id": None,
            "facility_id": 1000000000001,
            "runs": 9,
            "successful_runs": None,
            "status": "active",
            "cost": None,
            "start_date": now.isoformat(),
            "end_date": (now + timezone.timedelta(days=6)).isoformat(),
        }
    ]
    source.last_error = "Previous import failed."
    source.save()
    with patch("aa_industry_planner.tasks.resolve_entity_names.delay"):
        sync_source(source, api)
    asset = Asset.objects.get(source=source, item_id=42)
    assert asset.is_blueprint_copy is (copy is True)
    assert asset.location_flag == (flag or "")
    assert inventory_totals(user) == ({} if copy else {34: 5})
    assert IndustryJob.objects.get(source=source, job_id=54321).product_type_id is None
    source.refresh_from_db()
    assert source.last_error == ""
    assert source.last_assets_sync and source.last_jobs_sync
