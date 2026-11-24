"""User-visible production assignments and optional storage by activity."""

from collections import defaultdict

from django.db.models import F
from eve_sde.models import BlueprintActivityProduct, ItemType

from aa_industry_planner.engine import StockScope
from aa_industry_planner.industry import BONUS_SCOPE_OFFSET, matching_bonus_families
from aa_industry_planner.models import (
    ActivityStorageLocation,
    Facility,
    FacilityAssignment,
    ItemPolicy,
)


class InventoryLocations:
    def __init__(self, user, policies=None):
        visible = Facility.visible_to(user)
        self.assignments = {
            (a.activity, a.scope_key): a.service
            for a in FacilityAssignment.objects.filter(
                user=user,
                service__online=True,
                service__facility__in=visible,
                service__activity=F("activity"),
            )
            .select_related("service__facility__solar_system")
            .prefetch_related(
                "service__rigs",
                "service__facility__fitted_modules",
                "service__facility__fitted_rigs",
            )
        }
        self.storage = defaultdict(list)
        self.names = {}
        for row in ActivityStorageLocation.objects.filter(
            user=user, facility__in=visible
        ).select_related("facility"):
            self.storage[row.activity].append(row.facility.structure_id)
            self.names[row.facility.structure_id] = row.facility.name
        for service in self.assignments.values():
            self.names[service.facility.structure_id] = service.facility.name
        self.all_ids = tuple(sorted(self.names))
        self.policies = (
            policies
            if policies is not None
            else {p.item_type_id: p for p in ItemPolicy.objects.filter(user=user)}
        )
        self.groups, self.activities = {}, {}

    def service_for(self, item_id, activity):
        if item_id not in self.groups:
            self.groups[item_id] = ItemType.objects.values_list(
                "group_id", "group__category_id"
            ).filter(pk=item_id).first() or (None, None)
        group_id, category_id = self.groups[item_id]
        service = self.assignments.get((activity, group_id))
        if service is None:
            for family in matching_bonus_families(activity, group_id, category_id):
                service = self.assignments.get((activity, BONUS_SCOPE_OFFSET + family))
                if service is not None:
                    break
        return service or self.assignments.get((activity, 0))

    def scope(self, item_id, activity):
        service = self.service_for(item_id, activity)
        destination = service.facility.structure_id if service else None
        ids = ([destination] if destination else []) + self.storage[activity]
        return StockScope(tuple(dict.fromkeys(ids)), destination, activity)

    def prime_items(self, item_ids):
        missing = sorted(set(item_ids) - self.activities.keys())
        for start in range(0, len(missing), 500):
            batch = missing[start : start + 500]
            grouped = defaultdict(set)
            for item_id, activity, blueprint_id in BlueprintActivityProduct.objects.filter(
                item_type_id__in=batch,
                blueprint_activity__activity__in=["manufacturing", "reaction"],
            ).values_list(
                "item_type_id",
                "blueprint_activity__activity",
                "blueprint_activity__blueprint_item_type_id",
            ):
                policy = self.policies.get(item_id)
                if (
                    not policy
                    or not policy.blueprint_type_id
                    or policy.blueprint_type_id == blueprint_id
                ):
                    grouped[item_id].add(activity)
            self.groups.update(
                {
                    tid: (group, category)
                    for tid, group, category in ItemType.objects.filter(pk__in=batch).values_list(
                        "pk", "group_id", "group__category_id"
                    )
                }
            )
            for tid in batch:
                activities = grouped[tid]
                self.activities[tid] = (
                    next(iter(activities))
                    if len(activities) == 1
                    else "ambiguous"
                    if activities
                    else None
                )
                self.groups.setdefault(tid, (None, None))

    def item_scope(self, item_id):
        if item_id not in self.activities:
            self.prime_items([item_id])
        activity = self.activities[item_id]
        if activity == "ambiguous":
            return StockScope()
        return self.scope(item_id, activity) if activity else StockScope(self.all_ids)
