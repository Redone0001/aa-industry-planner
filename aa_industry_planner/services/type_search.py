"""Small, language-aware autocomplete queries over the installed SDE."""

from hashlib import sha256

from django.core.cache import cache
from django.utils.translation import get_language
from eve_sde.models import ItemType

RESULT_LIMIT = 30
CACHE_SECONDS = 300


def search_types(query):
    query = query.strip()[:100]
    numeric = query.isdecimal()
    if not query or (len(query) < 2 and not numeric):
        return []
    key = f"aip:type-search:v1:{get_language()}:{sha256(query.encode()).hexdigest()}"
    results = cache.get(key)
    if results is not None:
        return results

    # values_list preserves modeltranslation's active language and name fallback
    # without constructing 48-field ItemType instances and loading descriptions.
    items = ItemType.objects.order_by("name").values_list("id", "name")
    if numeric:
        type_id = int(query)
        rows = list(items.filter(pk=type_id)) if 0 < type_id <= 2**63 - 1 else []
    else:
        # A prefix lookup can use SDE's name index on the production database.
        # Avoid the substring query entirely when the prefix fills the dropdown.
        rows = list(items.filter(name__istartswith=query)[:RESULT_LIMIT])
        if len(rows) < RESULT_LIMIT:
            rows += list(
                items.filter(name__icontains=query).exclude(pk__in=[tid for tid, _ in rows])[
                    : RESULT_LIMIT - len(rows)
                ]
            )
    results = [
        {"id": tid, "name": str(name or tid), "value": f"{tid} · {name or tid}"}
        for tid, name in rows
    ]
    cache.set(key, results, CACHE_SECONDS)
    return results
