"""Stable area identities and conservative name matching."""

import unicodedata

from django.core.exceptions import ValidationError
from django.db.models import Q


def normalize_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def split_aliases(value):
    return list(dict.fromkeys(part.strip() for part in value.split("|") if part.strip()))


def matches(value):
    from .models import OperationalZone

    key = normalize_name(value)
    return OperationalZone.objects.filter(
        Q(normalized_name=key) | Q(aliases__normalized_name=key)
    ).distinct()


def validate_identity(name, aliases=(), zone_id=None):
    keys = [normalize_name(name), *(normalize_name(alias) for alias in aliases)]
    if not keys[0]:
        raise ValidationError("Enter an area name.")
    if len(keys) != len(set(keys)):
        raise ValidationError("Aliases must be distinct from the area name and each other.")
    for value in [name, *aliases]:
        if len(value) > 100 or len(normalize_name(value)) > 200:
            raise ValidationError("Area names and aliases must be at most 100 characters.")
        if matches(value).exclude(pk=zone_id).exists():
            raise ValidationError(
                f'Name or alias "{value}" already identifies another area (including inactive areas).'
            )
