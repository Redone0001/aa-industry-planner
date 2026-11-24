from collections import defaultdict

from django.db.models import Count, Sum

from aa_industry_planner.models import Asset, IndustryJob, InventorySource, ManualStock


def authorized_sources(user):
    """Recheck local AA ownership and permissions on reads, not only at connection time."""
    from allianceauth.authentication.models import CharacterOwnership

    sources = InventorySource.objects.filter(user=user, enabled=True).select_related("token")
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
        if owner_hash != token.character_owner_hash:
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


def inventory_totals(user):
    sources = authorized_sources(user).filter(include_in_planning=True)
    totals = defaultdict(int)
    for row in (
        Asset.objects.filter(source__in=sources, is_blueprint_copy=False)
        .values("type_id")
        .annotate(total=Sum("quantity"))
    ):
        totals[row["type_id"]] += row["total"]
    for row in (
        ManualStock.objects.filter(source__in=sources)
        .values("item_type_id")
        .annotate(total=Sum("quantity"))
    ):
        totals[row["item_type_id"]] += row["total"]
    return dict(totals)


def source_overview(user):
    """Connection counts are separate from manual stock and AA character ownership."""
    allowed = set(authorized_sources(user).values_list("pk", flat=True))
    sources = list(InventorySource.objects.filter(user=user).order_by("kind", "name"))
    job_counts = dict(
        IndustryJob.objects.filter(user=user)
        .values("source_id")
        .annotate(total=Count("pk"))
        .values_list("source_id", "total")
    )
    for source in sources:
        source.connected = source.pk in allowed and source.kind != "manual"
        source.needs_reconnection = source.kind != "manual" and source.pk not in allowed
        source.imported_jobs = job_counts.get(source.pk, 0)
    return {
        "sources": sources,
        "connected_characters": sum(s.connected and s.kind == "character" for s in sources),
        "connected_corporations": sum(s.connected and s.kind == "corporation" for s in sources),
    }
