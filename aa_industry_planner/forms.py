from django import forms
from django.core.exceptions import ValidationError
from eve_sde.models import ItemType

from .models import (
    Facility,
    FacilityAssignment,
    FacilityService,
    InventoryWatch,
    ItemPolicy,
    ManualStock,
    NotificationPreferences,
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
    use_inventory = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Use all included inventory sources. Stock is allocated within this plan; it is not reserved across separate plans.",
    )


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
        fields = ["item_type", "buy", "material_efficiency", "time_efficiency", "blueprint_type"]


class WatchForm(StyledFormMixin, forms.ModelForm):
    item_type = TypeField()

    class Meta:
        model = InventoryWatch
        fields = ["item_type", "minimum", "enabled"]


class AssignmentForm(StyledFormMixin, forms.ModelForm):
    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = FacilityService.objects.filter(
            online=True, facility__in=Facility.visible_to(user)
        ).select_related("facility")

    class Meta:
        model = FacilityAssignment
        fields = ["activity", "item_group", "service"]


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
