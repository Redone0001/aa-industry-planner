from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils.timezone import template_localtime
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
    minutes, seconds = divmod(seconds, 60)
    return " ".join(
        f"{amount}{unit}"
        for amount, unit in [(days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")]
        if amount
    )


@register.simple_tag
def production_estimate(seconds, finish_at):
    day = 86_400
    if seconds > 10 * day:
        unit, date_format = day, "d M Y"
    else:
        unit = 3_600 if seconds > day else 60
        date_format = "d M Y H:i T"
        local_finish = template_localtime(finish_at)
        elapsed = timedelta(
            minutes=local_finish.minute if unit == 3_600 else 0,
            seconds=local_finish.second,
            microseconds=local_finish.microsecond,
        )
        if elapsed:
            finish_at += timedelta(seconds=unit) - elapsed

    rounded_seconds = (seconds + unit - 1) // unit * unit
    return dict(remaining=duration(rounded_seconds), finish_at=finish_at, date_format=date_format)


@register.filter
def type_name(value):
    try:
        item = ItemType.objects.filter(pk=int(value)).first()
    except (ValueError, TypeError):
        item = None
    return str(item.name) if item else str(value)
