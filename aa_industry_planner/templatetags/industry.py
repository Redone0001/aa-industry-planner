from decimal import Decimal, InvalidOperation

from django import template
from eve_sde.models import ItemType

register = template.Library()


@register.filter
def percentage(value):
    if value is None:
        return "Unavailable"
    try:
        return f"{Decimal(str(value)) * 100:.2f}%"
    except (InvalidOperation, ValueError):
        return "Unavailable"


@register.filter
def duration(value):
    seconds = int(value or 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return (f"{days}d " if days else "") + f"{hours}h {minutes}m"


@register.filter
def type_name(value):
    try:
        item = ItemType.objects.filter(pk=int(value)).first()
    except (ValueError, TypeError):
        item = None
    return str(item.name) if item else str(value)
