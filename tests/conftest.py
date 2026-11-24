from unittest.mock import patch

import pytest
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from celery import current_app
from django.contrib.auth.models import Permission, User
from django.utils import timezone
from esi.models import Scope, Token
from eve_sde.models import (
    BlueprintActivity,
    BlueprintActivityMaterial,
    BlueprintActivityProduct,
    ItemCategory,
    ItemGroup,
    ItemType,
    SolarSystem,
)

from aa_industry_planner.models import (
    Facility,
    FacilityAssignment,
    FacilityService,
    InventorySource,
)
from aa_industry_planner.providers import CHARACTER_SCOPES, CORPORATION_SCOPES

current_app.config_from_object("django.conf:settings", namespace="CELERY")


def attach_character(user, character_id, owner_hash):
    character = EveCharacter.objects.create(
        character_id=character_id,
        character_name=user.username,
        corporation_id=98000001,
        corporation_name="Industrial Corp",
    )
    CharacterOwnership.objects.create(user=user, character=character, owner_hash=owner_hash)
    type(user.profile).objects.filter(pk=user.profile.pk).update(main_character=character)
    user.profile.refresh_from_db()
    return character


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def user(db):
    user = User.objects.create_user("builder", password="local-only")
    user.user_permissions.add(Permission.objects.get(codename="access_planner"))
    attach_character(user, 90000001, "owner-hash-1")
    return user


@pytest.fixture
def other_user(db):
    other = User.objects.create_user("other")
    other.user_permissions.add(Permission.objects.get(codename="access_planner"))
    attach_character(other, 90000002, "owner-hash-2")
    return other


@pytest.fixture
def types(db):
    category = ItemCategory.objects.create(id=6, name="Ships")
    group = ItemGroup.objects.create(id=25, name="Frigate", category=category)
    return {
        i: ItemType.objects.create(id=i, name=name, group=group, published=True)
        for i, name in [
            (1, "Final product"),
            (2, "Component A"),
            (3, "Component B"),
            (4, "Shared material"),
            (5, "Raw input"),
            (101, "Final blueprint"),
            (102, "Component A blueprint"),
            (103, "Component B blueprint"),
            (104, "Shared blueprint"),
        ]
    }


def add_recipe(types, product_id, materials, quantity=1, activity="manufacturing", seconds=60):
    blueprint = types[product_id + 100]
    recipe = BlueprintActivity.objects.create(
        id=f"{blueprint.pk}:{activity}",
        blueprint_item_type=blueprint,
        activity=activity,
        time=seconds,
        max_production_limit=10000,
    )
    BlueprintActivityProduct.objects.create(
        blueprint_activity=recipe, item_type=types[product_id], quantity=quantity
    )
    for material, amount in materials.items():
        BlueprintActivityMaterial.objects.create(
            blueprint_activity=recipe, item_type=types[material], quantity=amount
        )
    return recipe


@pytest.fixture
def recipes(types):
    add_recipe(types, 1, {2: 2, 3: 3})
    add_recipe(types, 2, {4: 2})
    add_recipe(types, 3, {4: 1})
    add_recipe(types, 4, {5: 10}, quantity=5, activity="reaction", seconds=3600)
    return types


@pytest.fixture
def facility(db):
    system = SolarSystem.objects.create(id=30000142, name="Jita")
    facility = Facility.objects.create(
        name="Forge One", structure_id=1000000000001, solar_system=system
    )
    return FacilityService.objects.create(facility=facility, activity="manufacturing")


@pytest.fixture
def source(user):
    ownership = user.character_ownerships.select_related("character").get()
    character = ownership.character
    with patch.object(EveCharacter.objects, "get_or_create_esi", return_value=character):
        token = Token.objects.create(
            user=user,
            character_id=character.character_id,
            character_name=character.character_name,
            character_owner_hash=ownership.owner_hash,
            access_token="test-access-token",
            refresh_token="test-refresh-token",
            created=timezone.now(),
        )
    token.scopes.set(
        [
            Scope.objects.get_or_create(name=name)[0]
            for name in CHARACTER_SCOPES + CORPORATION_SCOPES
        ]
    )
    return InventorySource.objects.create(
        user=user,
        kind="character",
        owner_id=character.character_id,
        name=character.character_name,
        token=token,
    )


@pytest.fixture
def assigned_facility(user, facility):
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    return facility.facility
