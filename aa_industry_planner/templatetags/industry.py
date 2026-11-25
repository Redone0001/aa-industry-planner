from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from eve_sde.models import ItemType

register = template.Library()


@register.filter
def quantity(value):
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


@register.filter
def compact_isk(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "Unavailable"
        amount = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if abs(amount) < 1_000_000:
            return f"{amount:,} ISK"
        divisor, unit = (
            (Decimal("1000000000"), "B")
            if abs(amount) >= 1_000_000_000
            else (Decimal("1000000"), "M")
        )
        scaled = (amount / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if unit == "M" and abs(scaled) >= 1000:
            scaled = (amount / Decimal("1000000000")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            unit = "B"
        return f"{format(scaled, 'f').rstrip('0').rstrip('.')} {unit} ISK"
    except (InvalidOperation, ValueError, TypeError):
        return "Unavailable"


@register.filter
def percentage(value):
    if value is None:
        return "Unavailable"
    try:
        return f"{Decimal(str(value)) * 100:.2f}%"
    except (InvalidOperation, ValueError):
        return "Unavailable"


@register.filter
def exact_number(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "Unavailable"
        result = format(amount, ",f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    except (InvalidOperation, ValueError, TypeError):
        return "Unavailable"


@register.filter
def exact_percentage(value):
    try:
        amount = Decimal(str(value))
        return f"{exact_number(amount * 100)}%" if amount.is_finite() else "Unavailable"
    except (InvalidOperation, ValueError, TypeError):
        return "Unavailable"


@register.filter
def duration(value):
    seconds = int(value or 0)
    if seconds < 60:
        return f"{seconds}s"
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
