from django import forms
from django.core.exceptions import ValidationError
from eve_sde.models import ItemType

from .industry import bonus_family_choices, family_activity
from .models import (
    Facility,
    FacilityAssignment,
    FacilityService,
    InventoryWatch,
    ItemPolicy,
    ManualStock,
    NotificationPreferences,
    ProductionPlan,
    UserPreferences,
)


class TypeWidget(forms.TextInput):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs.update(
            {
                "data-type-search": "true",
                "autocomplete": "off",
                "placeholder": "Search an EVE item…",
            }
        )

    def format_value(self, value):
        if value and str(value).isdigit():
            item = ItemType.objects.filter(pk=value).first()
            if item:
                return f"{item.pk} · {item.name}"
        return super().format_value(value)


class TypeField(forms.CharField):
    widget = TypeWidget

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return None
        try:
            return ItemType.objects.get(pk=int(value.split(" · ")[0]))
        except (ValueError, TypeError, ItemType.DoesNotExist) as exc:
            raise ValidationError("Choose a valid item from the search results.") from exc


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                widget_class = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                widget_class = "form-select"
            else:
                widget_class = "form-control"
            field.widget.attrs["class"] = widget_class


class PlanForm(StyledFormMixin, forms.Form):
    item_type = TypeField(label="Item to produce")
    quantity = forms.IntegerField(min_value=1, max_value=10**12, initial=1)
    name = forms.CharField(max_length=200, required=False)
    default_material_efficiency = forms.IntegerField(
        label="Default blueprint ME (%)",
        min_value=0,
        max_value=10,
        initial=10,
        help_text="Manufacturing blueprints default to ME 10. Individual blueprints can be changed in the plan.",
    )
    default_time_efficiency = forms.IntegerField(
        label="Default blueprint TE (%)",
        min_value=0,
        max_value=20,
        initial=20,
        help_text="Manufacturing blueprints default to TE 20. Reactions do not use ME or TE.",
    )
    use_inventory = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Use all included inventory sources. Stock is allocated within this plan; it is not reserved across separate plans.",
    )


class PlanSettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ProductionPlan
        fields = ["default_material_efficiency", "default_time_efficiency", "use_inventory"]
        labels = {
            "default_material_efficiency": "Default blueprint ME (%)",
            "default_time_efficiency": "Default blueprint TE (%)",
        }


class BlueprintRowForm(StyledFormMixin, forms.Form):
    item_type_id = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    material_efficiency = forms.IntegerField(
        label="ME override (%)", min_value=0, max_value=10, required=False
    )
    time_efficiency = forms.IntegerField(
        label="TE override (%)", min_value=0, max_value=20, required=False
    )


class BaseBlueprintFormSet(forms.BaseFormSet):
    def __init__(self, *args, **kwargs):
        self.expected_ids = {row["item_type_id"] for row in kwargs.get("initial", [])}
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        ids = [f.cleaned_data.get("item_type_id") for f in self.forms]
        if len(ids) != len(set(ids)) or set(ids) != self.expected_ids:
            raise ValidationError("Submit each manufacturing blueprint in this plan exactly once.")


BlueprintFormSet = forms.formset_factory(
    BlueprintRowForm, formset=BaseBlueprintFormSet, extra=0, max_num=1501, validate_max=True
)


def blueprint_rows(production):
    overrides = {o.item_type_id: o for o in production.blueprint_overrides.all()}
    rows = []
    for tid in production.snapshot.get("order", []):
        node = production.snapshot.get("nodes", {}).get(str(tid), {})
        recipe = node.get("recipe") or {}
        if recipe.get("activity") != "manufacturing" or not node.get("required"):
            continue
        override = overrides.get(int(tid))
        context = node.get("context") or {}
        rows.append(
            {
                "item_type_id": int(tid),
                "name": node.get("name", str(tid)),
                "material_efficiency": override.material_efficiency if override else None,
                "time_efficiency": override.time_efficiency if override else None,
                "effective_me": context.get("me", production.default_material_efficiency),
                "effective_te": context.get("te", production.default_time_efficiency),
            }
        )
    return rows


class ManualStockForm(StyledFormMixin, forms.ModelForm):
    item_type = TypeField()

    class Meta:
        model = ManualStock
        fields = ["item_type", "quantity", "location"]


class PolicyForm(StyledFormMixin, forms.ModelForm):
    item_type = TypeField()
    blueprint_type = TypeField(required=False, label="Blueprint (optional)")

    class Meta:
        model = ItemPolicy
        fields = ["item_type", "buy", "blueprint_type"]


class WatchForm(StyledFormMixin, forms.ModelForm):
    item_type = TypeField()

    class Meta:
        model = InventoryWatch
        fields = ["item_type", "minimum", "enabled"]


class AssignmentScopeSelect(forms.Select):
    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        if value:
            instance = getattr(value, "instance", None)
            option["attrs"]["data-activity"] = (
                instance.activity if instance else family_activity(value)
            )
        return option


class AssignmentForm(StyledFormMixin, forms.ModelForm):
    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = FacilityService.objects.filter(
            online=True, facility__in=Facility.visible_to(user)
        ).select_related("facility")
        choices = bonus_family_choices()
        self.fields["bonus_family"].choices = [
            ("", "All other products (catch-all)"),
            (
                "Manufacturing",
                [
                    (value, label)
                    for value, label in choices
                    if family_activity(value) == "manufacturing"
                ],
            ),
            (
                "Reactions",
                [
                    (value, label)
                    for value, label in choices
                    if family_activity(value) == "reaction"
                ],
            ),
        ]

    class Meta:
        model = FacilityAssignment
        fields = ["activity", "bonus_family", "service"]
        labels = {"bonus_family": "Industry bonus family"}
        widgets = {
            "activity": forms.Select(attrs={"data-assignment-activity": "true"}),
            "bonus_family": AssignmentScopeSelect(attrs={"data-assignment-family": "true"}),
            "service": AssignmentScopeSelect(attrs={"data-assignment-service": "true"}),
        }


class PreferencesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = UserPreferences
        fields = [
            "reaction_runs",
            "reaction_hours",
            "manufacturing_time_multiplier",
            "reaction_time_multiplier",
            "alpha_clone_tax",
        ]


class NotificationPreferencesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = NotificationPreferences
        fields = ["job_finished", "inventory_low", "sync_failed", "discord_dm"]
