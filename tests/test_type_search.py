from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils.translation import override
from eve_sde.models import ItemType

from aa_industry_planner.services.type_search import search_types


def test_search_prioritizes_prefix_but_keeps_substrings(types, django_assert_num_queries):
    types[1].name = "Raven"
    types[1].save()
    types[2].name = "Caldari Navy Raven"
    types[2].save()
    with django_assert_num_queries(2):
        results = search_types("Raven")
    assert [r["id"] for r in results] == [1, 2]
    assert results[0]["value"] == "1 · Raven"
    with django_assert_num_queries(0):
        assert search_types(" Raven ") == results


def test_search_reads_names_without_loading_descriptions(types):
    with CaptureQueriesContext(connection) as queries:
        assert search_types("Final product")[0]["name"] == "Final product"
    assert len(queries) == 2
    for query in queries:
        selected = query["sql"].split(" FROM ")[0].lower()
        assert "description" not in selected
        assert "mass" not in selected
        assert "name_fr_fr" not in selected


def test_full_prefix_page_does_not_scan_for_substrings(types, django_assert_num_queries):
    ItemType.objects.bulk_create(
        [
            ItemType(id=1000 + i, name=f"Raven variant {i:02}", group=types[1].group)
            for i in range(40)
        ]
    )
    with django_assert_num_queries(1):
        results = search_types("Raven")
    assert len(results) == 30
    assert results[0]["name"] == "Raven variant 00"


def test_cache_separates_languages_and_preserves_fallback(types, django_assert_num_queries):
    types[1].name_fr_fr = "Produit final"
    types[1].save()
    with override("en"):
        assert search_types("Produit") == []
    with override("fr-fr"):
        assert search_types("Produit")[0]["name"] == "Produit final"
        # Untranslated names still use modeltranslation's fallback on ID lookup.
        assert search_types("2")[0]["name"] == "Component A"
    with override("en"), django_assert_num_queries(0):
        assert search_types("Produit") == []


def test_short_and_invalid_numeric_queries_do_not_hit_the_sde(types, django_assert_num_queries):
    with django_assert_num_queries(0):
        for query in ["", " ", "r", "0", "9" * 100]:
            assert search_types(query) == []
    with django_assert_num_queries(1):
        assert search_types("1")[0]["id"] == 1
