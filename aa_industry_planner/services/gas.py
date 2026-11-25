"""Raw and compressed gas shortages for EVE Multibuy."""

from collections import Counter

from eve_sde.models import ItemType

from .readiness import multibuy_text

HARVESTABLE_CLOUD = 711
COMPRESSED_GAS = 4168

# CCP SDE compressibleTypes, release 20261125T110706Z, restricted to gas.
COMPRESSED_TYPES = {
    25268: 62396,
    25273: 62390,
    25274: 62394,
    25275: 62387,
    25276: 62392,
    25277: 62391,
    25278: 62393,
    25279: 62386,
    28694: 62377,
    28695: 62379,
    28696: 62380,
    28697: 62381,
    28698: 62382,
    28699: 62383,
    28700: 62384,
    28701: 62385,
    30370: 62399,
    30371: 62397,
    30372: 62398,
    30373: 62403,
    30374: 62400,
    30375: 62402,
    30376: 62404,
    30377: 62406,
    30378: 62405,
}


def gas_multibuy(inputs, yield_percent):
    shortages = {row["type_id"]: row for row in inputs if row["missing"] > 0}
    counterparts = {COMPRESSED_TYPES[tid] for tid in shortages.keys() & COMPRESSED_TYPES.keys()}
    gas_types = {
        tid: (group, name)
        for tid, group, name in ItemType.objects.filter(
            pk__in=shortages.keys() | counterparts, group_id__in=[HARVESTABLE_CLOUD, COMPRESSED_GAS]
        ).values_list("pk", "group_id", "name")
    }
    raw, unavailable = [], []
    compressed = Counter()
    for tid, row in shortages.items():
        group, _ = gas_types.get(tid, (None, ""))
        if group == COMPRESSED_GAS:
            compressed[tid] += row["missing"]
        elif group == HARVESTABLE_CLOUD:
            raw.append(row)
            compressed_id = COMPRESSED_TYPES.get(tid)
            if compressed_id not in gas_types:
                unavailable.append(row["name"])
            else:
                compressed[compressed_id] += (
                    row["missing"] * 100 + yield_percent - 1
                ) // yield_percent

    return dict(
        raw=multibuy_text(raw),
        compressed=multibuy_text(
            {"name": gas_types[tid][1], "missing": amount} for tid, amount in compressed.items()
        ),
        unavailable=sorted(unavailable, key=str.casefold),
        yield_percent=yield_percent,
    )
