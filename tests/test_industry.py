"""Golden vectors from CCP's SDE, plus admin/planner integration checks."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from eve_sde.models import ItemCategory, ItemGroup, ItemType

from aa_industry_planner.industry import (
    FitError,
    IndustryFit,
    catalog,
    entry,
    security_band,
    type_choices,
)
from aa_industry_planner.models import (
    Facility,
    FacilityAssignment,
    FacilityFittedRig,
    FacilityModule,
    FacilityRig,
)
from aa_industry_planner.services.planning import calculate

D = Decimal


@pytest.mark.parametrize(
    ("hull", "material", "time", "cost"),
    [
        (35825, ".99", ".85", ".97"),
        (35826, ".99", ".8", ".96"),
        (35827, ".99", ".7", ".95"),
        (35833, "1", "1", "1"),
    ],
)
def test_unrigged_manufacturing_hull_bonuses(hull, material, time, cost):
    fit = IndustryFit(hull, 30000001, -0.5, ((35878, True),))
    result = fit.multipliers("manufacturing", 25, 6)
    assert (result.material, result.time, result.cost) == (D(material), D(time), D(cost))


@pytest.mark.parametrize(
    ("security", "material", "time"),
    [(0.8, ".96624", ".532"), (0.2, ".944856", ".3808"), (-0.5, ".940104", ".3472")],
)
def test_t2_sotiyo_ship_rig_by_security(security, material, time):
    fit = IndustryFit(35827, 30000001, security, ((35878, True),), (37181,))
    result = fit.multipliers("manufacturing", 25, 6)
    assert result.material == D(material)
    assert result.time == D(time)
    # An XL ship rig never gives its ship bonus to components or equipment.
    assert fit.multipliers("manufacturing", 332, 17).material == D(".99")
    assert fit.multipliers("manufacturing", 53, 7).time == D(".7")


def test_wormhole_uses_nullsec_rig_strength_without_nullsec_supercapital_access():
    fit = IndustryFit(35827, 31000001, None, ((35878, True),), (37181,))
    assert fit.multipliers("manufacturing", 25, 6).material == D(".940104")
    with pytest.raises(FitError, match="sovereign-nullsec"):
        IndustryFit(35827, 31000001, -0.99, ((35877, True),), (), True).validate()


@pytest.mark.parametrize(
    ("security", "expected"),
    [(0.45, "high"), (0.4499, "low"), (0.0001, "low"), (0, "null"), (-0.1, "null")],
)
def test_security_boundaries(security, expected):
    assert security_band(30000001, security) == expected


@pytest.mark.parametrize("security", [None, float("nan"), float("inf"), 2])
def test_unknown_or_invalid_security_is_an_error(security):
    with pytest.raises(FitError):
        security_band(30000001, security)


@pytest.mark.parametrize(
    ("security", "material", "time"), [(0.2, ".976", ".57"), (-0.5, ".9736", ".552")]
)
def test_tatara_uses_reactor_security_multiplier(security, material, time):
    fit = IndustryFit(35836, 30000001, security, ((45537, True),), (46497,))
    result = fit.multipliers("reaction", 428, 4)
    assert result.material == D(material)
    assert result.time == D(time)
    assert result.cost == 1
    # A composite service cannot execute a hybrid reaction, even with an L rig.
    with pytest.raises(FitError, match="No online service"):
        fit.multipliers("reaction", 974, 4)


def test_three_reactor_types_are_independent_and_offline_services_do_not_enable_jobs():
    fit = IndustryFit(
        35836, 30000001, -0.2, ((45537, True), (45538, False), (45539, True)), (46497,)
    )
    assert fit.supports("reaction", 4096, 4)
    assert fit.supports("reaction", 4932, 4)
    assert not fit.supports("reaction", 974, 4)
    assert fit.multipliers("reaction", 4096, 4).material == D(".9736")


@pytest.mark.parametrize(
    ("security", "material"), [(0.2, ".920403"), (-0.5, ".986337"), (0.8, ".986337")]
)
def test_thukker_basic_capital_component_rig(security, material):
    fit = IndustryFit(35825, 30000001, security, ((35878, True),), (45544,))
    assert fit.multipliers("manufacturing", 873, 17).material == D(material)
    assert fit.multipliers("manufacturing", 25, 6).material == D(".99")


def test_thukker_xl_has_different_bonus_sources_for_different_components():
    fit = IndustryFit(35827, 30000001, 0.2, ((35878, True),), (45548,))
    assert fit.multipliers("manufacturing", 873, 17).material == D(".920403")
    assert fit.multipliers("manufacturing", 913, 17).material == D(".920403")
    assert fit.multipliers("manufacturing", 332, 17).material == D(".95238")
    assert fit.multipliers("manufacturing", 536, 17).time == D(".434")


def test_service_capital_and_supercapital_permissions():
    basic = IndustryFit(35827, 30000001, -0.5, ((35878, True),))
    assert not basic.supports("manufacturing", 485, 6)
    capital = IndustryFit(35827, 30000001, -0.5, ((35881, True),))
    assert capital.supports("manufacturing", 485, 6)
    assert not capital.supports("manufacturing", 30, 6)
    supercap = IndustryFit(35827, 30000001, -0.5, ((35877, True),), (), True)
    assert supercap.multipliers("manufacturing", 30, 6).material == D(".99")
    with pytest.raises(FitError, match="sovereign-nullsec"):
        IndustryFit(35827, 30000001, -0.5, ((35877, True),)).validate()


def test_science_jobs_get_time_and_cost_bonuses_without_manufacturing_material_discount():
    fit = IndustryFit(35825, 30000001, 0.8, ((35891, True), (35886, True)))
    for activity in ["researchMaterial", "researchTime", "copying", "invention"]:
        result = fit.multipliers(activity, 25, 6)
        assert (result.material, result.time, result.cost) == (D(1), D(".85"), D(".97"))


@pytest.mark.parametrize(
    ("fit", "message"),
    [
        (IndustryFit(35825, 30000001, -0.5, (), (37181,)), "wrong rig size"),
        (IndustryFit(35825, 30000001, -0.5, (), (37154, 37155)), "one rig per fitting group"),
        (IndustryFit(35825, 30000001, -0.5, (), (37155, 37157, 37159)), "calibration"),
        (IndustryFit(35825, 30000001, -0.5, ((45537, True),)), "cannot be fitted"),
        (IndustryFit(35836, 30000001, 0.5, ((45537, True),)), "high security"),
        (IndustryFit(35825, 30000001, -0.5, ((35881, True),)), "cannot be fitted"),
        (IndustryFit(35826, 30000001, 0.5, ((35881, True),)), "high security"),
    ],
)
def test_invalid_fittings_fail_explicitly(fit, message):
    with pytest.raises(FitError, match=message):
        fit.validate()


def test_every_shipped_rig_has_evaluable_rules():
    """Exercise all 125 actual Standup rigs, including science and faction rigs."""
    seen = 0
    for tid, _ in type_choices("rig"):
        for hull, _ in type_choices("hull"):
            rig = entry(tid)
            services = ((35878, True), (35891, True), (35886, True))
            if any(key == "reaction" for key in rig["bonuses"]):
                services = ((45537, True), (45538, True), (45539, True))
            fit = IndustryFit(hull, 30000001, -0.5, services, (tid,))
            try:
                fit.validate()
            except FitError:
                continue
            assert fit.preview()
            for row in fit.preview():
                result = row["multipliers"]
                assert 0 < result.material <= 1
                assert 0 < result.time <= 1
                assert 0 < result.cost <= 1
            seen += 1
            break
        else:
            pytest.fail(f"No valid evaluable fit for rig {tid}")
    assert seen == 125


@pytest.fixture
def industry_types(types):
    """Actual fitting types with enough SDE metadata for admin FK validation."""
    for tid, row in catalog()["types"].items():
        group_id = row["groupID"]
        group_row = catalog()["groups"][str(group_id)]
        category, _ = ItemCategory.objects.get_or_create(
            id=group_row["categoryID"], defaults={"name": "Structure category"}
        )
        group, _ = ItemGroup.objects.get_or_create(
            id=group_id, defaults={"name": group_row["name"], "category": category}
        )
        ItemType.objects.get_or_create(
            id=int(tid), defaults={"name": row["name"], "group": group, "published": True}
        )


def auto_facility(service, hull=35827):
    facility = service.facility
    facility.structure_type_id = hull
    facility.solar_system.security_status = -0.5
    facility.solar_system.save()
    facility.save()
    FacilityModule.objects.create(facility=facility, item_type_id=35878)
    return facility


def test_planner_uses_actual_product_bonuses_and_snapshots_fit(
    user, recipes, facility, industry_types
):
    structure = auto_facility(facility)
    FacilityFittedRig.objects.create(facility=structure, item_type_id=37181)
    # Old manual multipliers are deliberately ignored after switching to a fit.
    facility.material_multiplier = D(".5")
    facility.save()
    FacilityRig.objects.create(service=facility, item_type=recipes[2], material_multiplier=D(".5"))
    FacilityAssignment.objects.create(user=user, activity="manufacturing", service=facility)
    result, snapshot = calculate(user, 1, 10)
    assert result.nodes[1].context.material_multiplier == D(".940104")
    assert result.nodes[1].context.time_multiplier == D(".3472")
    assert result.nodes[1].context.cost_multiplier == D(".95")
    context = snapshot["nodes"]["1"]["context"]
    assert context["facility_fit"]["rig_type_ids"] == [37181]
    assert catalog()["release"] in context["bonus_source"]


def test_modules_sync_activity_availability_without_resetting_taxes(facility, industry_types):
    structure = auto_facility(facility)
    facility.facility_tax = D(".08")
    facility.save()
    module = structure.fitted_modules.get()
    module.online = False
    module.save()
    facility.refresh_from_db()
    assert not facility.online
    assert facility.facility_tax == D(".08")
    module.online = True
    module.save()
    facility.refresh_from_db()
    assert facility.online
    module.delete()
    facility.refresh_from_db()
    assert not facility.online


def test_monitor_displays_calculated_bonuses(user, client, facility, industry_types):
    structure = auto_facility(facility)
    FacilityFittedRig.objects.create(facility=structure, item_type_id=37181)
    user.user_permissions.add(Permission.objects.get(codename="manage_facilities"))
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:facilities"))
    assert response.status_code == 200
    assert b"0.940104" in response.content
    assert b"Standup Manufacturing Plant I" in response.content


def admin_payload(system, rigs):
    data = {
        "name": "Configured Sotiyo",
        "structure_id": "1000000000999",
        "solar_system": system.pk,
        "structure_type": 35827,
        "active": "on",
        "_save": "Save",
        "fitted_modules-TOTAL_FORMS": "1",
        "fitted_modules-INITIAL_FORMS": "0",
        "fitted_modules-MIN_NUM_FORMS": "0",
        "fitted_modules-MAX_NUM_FORMS": "1000",
        "fitted_modules-0-item_type": 35878,
        "fitted_modules-0-online": "on",
        "fitted_rigs-TOTAL_FORMS": str(len(rigs)),
        "fitted_rigs-INITIAL_FORMS": "0",
        "fitted_rigs-MIN_NUM_FORMS": "0",
        "fitted_rigs-MAX_NUM_FORMS": "3",
        "services-TOTAL_FORMS": "0",
        "services-INITIAL_FORMS": "0",
        "services-MIN_NUM_FORMS": "0",
        "services-MAX_NUM_FORMS": "0",
    }
    for i, tid in enumerate(rigs):
        data[f"fitted_rigs-{i}-item_type"] = tid
    return data


def test_admin_can_save_a_fit_without_entering_multipliers(user, client, facility, industry_types):
    user.is_staff = user.is_superuser = True
    user.save()
    client.force_login(user)
    system = facility.facility.solar_system
    system.security_status = -0.5
    system.save()
    response = client.post(
        reverse("admin:aa_industry_planner_facility_add"), admin_payload(system, [37181])
    )
    assert response.status_code == 302
    structure = Facility.objects.get(name="Configured Sotiyo")
    assert structure.services.get().online
    assert structure.industry_fit().multipliers("manufacturing", 25, 6).material == D(".940104")
    response = client.get(reverse("admin:aa_industry_planner_facility_change", args=[structure.pk]))
    assert response.status_code == 200
    assert b"0.940104" in response.content


def test_admin_rejects_duplicate_tech_variants_before_saving(
    user, client, facility, industry_types
):
    user.is_staff = user.is_superuser = True
    user.save()
    client.force_login(user)
    system = facility.facility.solar_system
    system.security_status = -0.5
    system.save()
    response = client.post(
        reverse("admin:aa_industry_planner_facility_add"), admin_payload(system, [37180, 37181])
    )
    assert response.status_code == 200
    assert b"Only one rig per fitting group" in response.content
    assert not Facility.objects.filter(name="Configured Sotiyo").exists()
