from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.utils.html import format_html, format_html_join
from eve_sde.models import ItemType

from .industry import FitError, catalog, type_choices
from .models import (
    ActivityStorageLocation,
    AdjustedPrice,
    Facility,
    FacilityAssignment,
    FacilityFittedRig,
    FacilityModule,
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


class FacilityForm(forms.ModelForm):
    class Meta:
        model = Facility
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["structure_type"].queryset = ItemType.objects.filter(
            pk__in=[tid for tid, _ in type_choices("hull")]
        ).order_by("name")
        self.fields["structure_type"].required = self.instance._state.adding


class FittedModuleForm(forms.ModelForm):
    class Meta:
        model = FacilityModule
        fields = ["item_type", "online"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_type"].queryset = ItemType.objects.filter(
            pk__in=[tid for tid, _ in type_choices("service")]
        ).order_by("name")


class FittedRigForm(forms.ModelForm):
    class Meta:
        model = FacilityFittedRig
        fields = ["item_type"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_type"].queryset = ItemType.objects.filter(
            pk__in=[tid for tid, _ in type_choices("rig")]
        ).order_by("name")


class FitFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [
            f.cleaned_data
            for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE")
        ]
        if not self.instance.structure_type_id:
            if rows:
                raise ValidationError(
                    "Select the structure hull before adding fitted modules or rigs."
                )
            return
        if not self.instance.solar_system_id:
            return  # The parent form reports the missing solar system.
        modules, rigs = [], []
        if self.model is FacilityModule:
            modules = [(r["item_type"].pk, r.get("online", False)) for r in rows]
        else:
            rigs = [r["item_type"].pk for r in rows]
        try:
            # Service and rig constraints are independent. Validate the final
            # submitted rows so a deleted T1 rig can be replaced with a T2 rig.
            self.instance.industry_fit(modules=modules, rigs=rigs).validate()
        except FitError as exc:
            raise ValidationError(str(exc)) from exc


class FittedModuleInline(admin.TabularInline):
    model = FacilityModule
    form = FittedModuleForm
    formset = FitFormSet
    extra = 0
    verbose_name_plural = "Installed industry service modules"


class FittedRigInline(admin.TabularInline):
    model = FacilityFittedRig
    form = FittedRigForm
    formset = FitFormSet
    extra = 0
    max_num = 3
    verbose_name_plural = "Installed industry rigs (shared by all services)"


class ServiceInline(admin.TabularInline):
    model = FacilityService
    extra = 0

    def get_fields(self, request, obj=None):
        if obj is None or obj.structure_type_id:
            return ["activity", "online", "facility_tax", "scc_surcharge"]
        return [
            "activity",
            "online",
            "service_name",
            "material_multiplier",
            "time_multiplier",
            "cost_multiplier",
            "facility_tax",
            "scc_surcharge",
        ]

    def get_readonly_fields(self, request, obj=None):
        return ["activity", "online"] if obj is None or obj.structure_type_id else []

    def has_add_permission(self, request, obj=None):
        return bool(obj and not obj.structure_type_id) and super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return bool(obj and not obj.structure_type_id) and super().has_delete_permission(
            request, obj
        )


class RigInline(admin.TabularInline):
    model = FacilityRig
    extra = 0
    raw_id_fields = ["item_type", "applies_to_group"]


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    form = FacilityForm
    list_display = ["name", "structure_type", "solar_system", "structure_id", "active"]
    list_filter = ["active"]
    search_fields = ["name", "structure_id"]
    raw_id_fields = ["solar_system"]
    filter_horizontal = ["allowed_groups"]
    inlines = [FittedModuleInline, FittedRigInline, ServiceInline]
    readonly_fields = ["calculated_bonuses"]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.sync_services()

    @admin.display(description="Calculated facility bonuses")
    def calculated_bonuses(self, obj):
        if not obj or not obj.pk:
            return "Save the structure and its fit to see calculated bonuses."
        if not obj.structure_type_id:
            return "Legacy manual configuration. Select a hull and declare the complete fit to enable automatic bonuses."
        try:
            rows = obj.industry_fit().preview()
        except FitError as exc:
            return str(exc)
        if not rows:
            return "No online industry services."
        return format_html(
            "<p>CCP SDE {} · {} space. Facility bonuses; blueprint and character bonuses are applied separately.</p>"
            "<table><thead><tr><th>Activity</th><th>Product family</th><th>Material ×</th><th>Time ×</th><th>Cost ×</th></tr></thead><tbody>{}</tbody></table>",
            catalog()["release"],
            obj.industry_fit().band,
            format_html_join(
                "",
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                (
                    (
                        r["activity"],
                        r["product"],
                        r["multipliers"].material,
                        r["multipliers"].time,
                        r["multipliers"].cost,
                    )
                    for r in rows
                ),
            ),
        )


@admin.register(FacilityService)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["facility", "activity", "online", "facility_tax", "scc_surcharge"]
    list_filter = ["activity", "online"]
    inlines = [RigInline]

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.facility.structure_type_id:
            return ["facility", "activity", "online"]
        return []

    def get_fields(self, request, obj=None):
        if obj and obj.facility.structure_type_id:
            return ["facility", "activity", "online", "facility_tax", "scc_surcharge"]
        return super().get_fields(request, obj)

    def get_inlines(self, request, obj):
        return [] if obj and obj.facility.structure_type_id else super().get_inlines(request, obj)


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
    readonly_fields = [
        "last_assets_sync",
        "last_jobs_sync",
        "last_error",
        "sync_error_since",
        "sync_failed_at",
    ]
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
    ActivityStorageLocation,
    FacilityAssignment,
    ItemPolicy,
    InventoryWatch,
    ReplenishmentTask,
    Notification,
    AdjustedPrice,
]:
    admin.site.register(model)
