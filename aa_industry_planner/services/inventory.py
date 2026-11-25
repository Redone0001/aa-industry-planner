from collections import defaultdict

from django.db.models import Count
from eve_sde.models import NPCStation

from aa_industry_planner.models import Asset, Facility, IndustryJob, InventorySource, ManualStock
from aa_industry_planner.providers import missing_scopes


def authorized_sources(user):
    """Recheck local AA ownership and permissions on reads, not only at connection time."""
    from allianceauth.authentication.models import CharacterOwnership

    sources = (
        InventorySource.objects.filter(user=user, enabled=True)
        .select_related("token")
        .prefetch_related("token__scopes")
    )
    owned = {
        o.character.character_id: (o.owner_hash, o.character.corporation_id)
        for o in CharacterOwnership.objects.filter(user=user).select_related("character")
    }
    allowed = []
    for source in sources:
        if source.kind == "manual":
            allowed.append(source.pk)
            continue
        token = source.token
        if not token or token.user_id != user.pk or token.character_id not in owned:
            continue
        owner_hash, corporation_id = owned[token.character_id]
        if owner_hash != token.character_owner_hash or missing_scopes(token, source.kind):
            continue
        if source.kind == "corporation":
            if (
                not user.has_perm("aa_industry_planner.use_corporation_data")
                or corporation_id != source.owner_id
            ):
                continue
        elif token.character_id != source.owner_id:
            continue
        allowed.append(source.pk)
    return InventorySource.objects.filter(pk__in=allowed)


def inventory_by_location(user, locations=None, *, include_elsewhere=False):
    from .locations import InventoryLocations

    locations = locations or InventoryLocations(user)
    allowed = set(locations.all_ids)
    if not allowed and not include_elsewhere:
        return {}
    sources = authorized_sources(user).filter(include_in_planning=True)
    rows = list(
        Asset.objects.filter(source__in=sources).values(
            "source_id",
            "item_id",
            "type_id",
            "quantity",
            "location_id",
            "location_flag",
            "is_blueprint_copy",
        )
    )
    parents = {(r["source_id"], r["item_id"]): r["location_id"] for r in rows}
    known_locations = allowed
    hangar_roots = set()
    if include_elsewhere:
        known_locations = set(Facility.visible_to(user).values_list("structure_id", flat=True))
        # A top-level hangar or corporation office can identify an unregistered
        # Upwell structure. Other missing item parents are ambiguous.
        hangar_roots = {
            (row["source_id"], row["location_id"])
            for row in rows
            if row["location_id"] >= 10**12
            and (row["source_id"], row["location_id"]) not in parents
            and row["location_flag"] in {"Hangar", "OfficeFolder"}
        }
    roots = {}

    def root(source_id, location_id):
        trail, seen = [], set()
        current = location_id
        while current not in known_locations:
            key = (source_id, current)
            if include_elsewhere and (60_000_000 <= current < 64_000_000 or key in hangar_roots):
                break
            if key in roots:
                current = roots[key]
                break
            if key in seen or key not in parents:
                current = None
                break
            seen.add(key)
            trail.append(key)
            current = parents[key]
        for key in trail:
            roots[key] = current
        return current

    totals = defaultdict(lambda: defaultdict(int))
    for row in rows:
        location = root(row["source_id"], row["location_id"])
        # OfficeFolder entries locate corp hangars; they are not consumable items.
        if (
            location is not None
            and not row["is_blueprint_copy"]
            and row["location_flag"] != "OfficeFolder"
        ):
            totals[row["type_id"]][location] += row["quantity"]
    manual = ManualStock.objects.filter(
        source__in=sources, facility__structure_id__in=known_locations
    )
    for row in manual.values("item_type_id", "facility__structure_id", "quantity"):
        totals[row["item_type_id"]][row["facility__structure_id"]] += row["quantity"]
    return {tid: dict(amounts) for tid, amounts in totals.items()}


def stock_location_names(user, location_ids):
    names = dict(NPCStation.objects.filter(pk__in=location_ids).values_list("pk", "name"))
    names.update(
        Facility.visible_to(user)
        .filter(structure_id__in=location_ids)
        .values_list("structure_id", "name")
    )
    return {loc: names.get(loc) or f"Location #{loc}" for loc in location_ids}


def inventory_totals(user, locations=None):
    from .locations import InventoryLocations

    locations = locations or InventoryLocations(user)
    stock = inventory_by_location(user, locations)
    totals = {}
    locations.prime_items(stock)
    for tid, amounts in stock.items():
        allowed = set(locations.item_scope(tid).location_ids)
        total = sum(quantity for loc, quantity in amounts.items() if loc in allowed)
        if total:
            totals[tid] = total
    return totals


def source_overview(user):
    """Connection counts are separate from manual stock and AA character ownership."""
    allowed = set(authorized_sources(user).values_list("pk", flat=True))
    sources = list(
        InventorySource.objects.filter(user=user)
        .select_related("token")
        .prefetch_related("token__scopes")
        .order_by("kind", "name")
    )
    job_counts = dict(
        IndustryJob.objects.filter(user=user)
        .values("source_id")
        .annotate(total=Count("pk"))
        .values_list("source_id", "total")
    )
    for source in sources:
        source.missing_scopes = missing_scopes(source.token, source.kind)
        source.connected = source.pk in allowed and source.kind != "manual"
        source.needs_reconnection = source.kind != "manual" and source.pk not in allowed
        source.imported_jobs = job_counts.get(source.pk, 0)
    return {
        "sources": sources,
        "connected_characters": sum(s.connected and s.kind == "character" for s in sources),
        "connected_corporations": sum(s.connected and s.kind == "corporation" for s in sources),
        "pending_syncs": sum(bool(s.last_error) and not s.sync_failed_at for s in sources),
        "failed_syncs": sum(bool(s.sync_failed_at) for s in sources),
    }
