"""Seed only the repository's local SQLite demo. Recipes are synthetic."""
# ruff: noqa: E402

import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"

import django  # noqa: E402

django.setup()

from allianceauth.eveonline.models import EveCharacter  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402
from eve_sde.models import (
    BlueprintActivity,
    BlueprintActivityMaterial,
    BlueprintActivityProduct,
    ItemCategory,
    ItemGroup,
    ItemType,
    SolarSystem,
)  # noqa: E402

from aa_industry_planner.industry import catalog
from aa_industry_planner.models import (
    AdjustedPrice,
    Facility,
    FacilityAssignment,
    FacilityFittedRig,
    FacilityModule,
    FacilityService,
    IndustryJob,
    InventorySource,
    InventoryWatch,
    ManualStock,
    Notification,
    SystemCostIndex,
)  # noqa: E402
from aa_industry_planner.services.jobs import allocate_job  # noqa: E402
from aa_industry_planner.services.planning import create_plan  # noqa: E402
from aa_industry_planner.services.watches import evaluate_watches  # noqa: E402

assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
assert Path(settings.DATABASES["default"]["NAME"]).resolve() == ROOT / "dev.sqlite3"


def seed_fit_examples():
    """Add real fitting metadata and example fits to this synthetic local demo."""
    data = catalog()
    for tid, row in data["types"].items():
        group_id = row["groupID"]
        metadata = data["groups"][str(group_id)]
        category, _ = ItemCategory.objects.get_or_create(
            id=metadata["categoryID"], defaults={"name": "Structure fitting types"}
        )
        group, _ = ItemGroup.objects.get_or_create(
            id=group_id, defaults={"name": metadata["name"], "category": category}
        )
        ItemType.objects.get_or_create(
            id=int(tid), defaults={"name": row["name"], "group": group, "published": True}
        )
    system, _ = SolarSystem.objects.get_or_create(
        id=900000002, defaults={"name": "Demo · Nullsec Fit Preview", "security_status": -0.5}
    )
    for structure_id, name, hull, modules, rig in [
        (999000000002, "Demo · Sotiyo with T2 ship rig", 35827, [35878, 35881], 37181),
        (999000000003, "Demo · Tatara with T2 reactor rig", 35836, [45537, 45538, 45539], 46497),
    ]:
        facility, _ = Facility.objects.get_or_create(
            structure_id=structure_id,
            defaults={"name": name, "solar_system": system, "structure_type_id": hull},
        )
        for module in modules:
            FacilityModule.objects.get_or_create(facility=facility, item_type_id=module)
        FacilityFittedRig.objects.get_or_create(facility=facility, item_type_id=rig)
        facility.sync_services()


seed_fit_examples()

if User.objects.filter(username="industry-demo").exists():
    print("Demo already seeded. Local login: industry-demo / local-industry-demo")
    raise SystemExit(0)

user = User.objects.create_superuser("industry-demo", "demo@example.test", "local-industry-demo")
character = EveCharacter.objects.create(
    character_id=999000001,
    character_name="Demo Industrialist",
    corporation_id=999000002,
    corporation_name="Demo Foundry",
)
type(user.profile).objects.filter(pk=user.profile.pk).update(main_character=character)
category = ItemCategory.objects.create(id=900000001, name="Demo production")
group = ItemGroup.objects.create(id=900000001, name="Demo components", category=category)
names = {
    1: "Demo · Industrial Drone II",
    2: "Demo · Drone Assembly",
    3: "Demo · Guidance Unit",
    4: "Demo · Advanced Alloy",
    5: "Demo · Raw Metal",
}
types = {}
for number, name in names.items():
    types[number] = ItemType.objects.create(
        id=900000000 + number, name=name, group=group, published=True
    )
    AdjustedPrice.objects.create(type_id=types[number].pk, adjusted_price=Decimal(1000 * number))
for number, materials in [(1, {2: 2, 3: 1}), (2, {4: 4}), (3, {4: 3}), (4, {5: 100})]:
    blueprint = ItemType.objects.create(
        id=900000100 + number, name=f"{names[number]} Blueprint", group=group
    )
    activity = "reaction" if number == 4 else "manufacturing"
    bp = BlueprintActivity.objects.create(
        id=f"{blueprint.pk}:{activity}",
        blueprint_item_type=blueprint,
        activity=activity,
        time=3600,
        max_production_limit=1000,
    )
    BlueprintActivityProduct.objects.create(
        blueprint_activity=bp, item_type=types[number], quantity=10 if number == 4 else 1
    )
    for child, quantity in materials.items():
        BlueprintActivityMaterial.objects.create(
            blueprint_activity=bp, item_type=types[child], quantity=quantity
        )

system = SolarSystem.objects.create(id=900000001, name="Demo · Foundry System")
facility = Facility.objects.create(
    name="Meridian Manufacturing Campus", structure_id=999000000001, solar_system=system
)
now = timezone.now()
for activity, base in [("manufacturing", Decimal(".028")), ("reaction", Decimal(".017"))]:
    service = FacilityService.objects.create(
        facility=facility,
        activity=activity,
        service_name="Demo service",
        material_multiplier=Decimal(".98"),
        time_multiplier=Decimal(".8"),
    )
    FacilityAssignment.objects.create(user=user, activity=activity, service=service)
    for hour in range(120):
        SystemCostIndex.objects.create(
            solar_system=system,
            activity=activity,
            cost_index=base + Decimal(hour % 17) / 10000,
            recorded_at=(now - timezone.timedelta(hours=hour)).replace(
                minute=0, second=0, microsecond=0
            ),
        )
source = InventorySource.objects.create(user=user, kind="manual", name="Declared demo hangar")
ManualStock.objects.create(
    source=source, item_type=types[5], quantity=15000, facility=facility, location="Meridian Campus"
)
production = create_plan(user, types[1].pk, 100, "November drone restock")
step = production.steps.get(item_type=types[4])
job = IndustryJob.objects.create(
    user=user,
    job_id=999100001,
    source=source,
    installer_id=character.character_id,
    activity_id=11,
    blueprint_type_id=900000104,
    product_type_id=types[4].pk,
    facility_id=facility.structure_id,
    runs=step.runs,
    status="ready",
    start_date=now - timezone.timedelta(hours=12),
    end_date=now - timezone.timedelta(minutes=20),
)
allocate_job(user, job.pk, step.pk)
InventoryWatch.objects.create(user=user, item_type=types[1], minimum=200)
InventoryWatch.objects.create(user=user, item_type=types[5], minimum=20000)
evaluate_watches(user)
Notification.objects.create(
    user=user,
    event_key="demo-job",
    kind="job_finished",
    title="Alloy reaction complete",
    message="The demo alloy reaction has finished. Deliver the job to make its output available for your production chain.",
)
print("Seeded synthetic demo data. Local login: industry-demo / local-industry-demo")
