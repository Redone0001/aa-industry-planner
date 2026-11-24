from django.contrib import admin

from .models import (
    AdjustedPrice,
    Facility,
    FacilityAssignment,
    FacilityRig,
    FacilityService,
    IndustryJob,
    InventorySource,
    InventoryWatch,
    ItemPolicy,
    Notification,
    PlannerConfiguration,
    ProductionPlan,
    ReplenishmentTask,
    SystemCostIndex,
)


class ServiceInline(admin.TabularInline):
    model = FacilityService
    extra = 0


class RigInline(admin.TabularInline):
    model = FacilityRig
    extra = 0
    raw_id_fields = ["item_type", "applies_to_group"]


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ["name", "solar_system", "structure_id", "active"]
    list_filter = ["active"]
    search_fields = ["name", "structure_id"]
    raw_id_fields = ["solar_system"]
    filter_horizontal = ["allowed_groups"]
    inlines = [ServiceInline]


@admin.register(FacilityService)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["facility", "activity", "online", "facility_tax", "scc_surcharge"]
    list_filter = ["activity", "online"]
    inlines = [RigInline]


@admin.register(SystemCostIndex)
class IndexAdmin(admin.ModelAdmin):
    list_display = ["solar_system", "activity", "cost_index", "recorded_at"]
    list_filter = ["activity"]
    date_hierarchy = "recorded_at"
    raw_id_fields = ["solar_system"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PlannerConfiguration)
class ConfigurationAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not PlannerConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventorySource)
class SourceAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "kind", "enabled", "last_assets_sync", "last_jobs_sync"]
    list_filter = ["kind", "enabled"]
    exclude = ["token"]
    readonly_fields = ["last_assets_sync", "last_jobs_sync", "last_error"]
    raw_id_fields = ["user"]


@admin.register(ProductionPlan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "item_type", "quantity", "created_at", "archived"]
    raw_id_fields = ["user", "item_type"]
    readonly_fields = ["snapshot"]


@admin.register(IndustryJob)
class JobAdmin(admin.ModelAdmin):
    list_display = ["job_id", "user", "status", "activity_id", "runs", "end_date"]
    list_filter = ["status", "activity_id"]
    raw_id_fields = ["user", "source"]


for model in [
    FacilityAssignment,
    ItemPolicy,
    InventoryWatch,
    ReplenishmentTask,
    Notification,
    AdjustedPrice,
]:
    admin.site.register(model)
