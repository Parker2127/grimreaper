"""Rules that keep GrimReaper from touching things it shouldn't."""

from __future__ import annotations

from .models import Resource

KEEP_TAG_KEYS = {"grimreaper:keep", "do-not-delete", "donotdelete"}
NAME_MARKERS = ("do-not-delete", "donotdelete")


def protection_reason(resource: Resource) -> str | None:
    """Return why a resource must not be deleted, or None if it's fair game."""
    tags = {k.lower(): v for k, v in resource.tags.items()}
    for key in KEEP_TAG_KEYS:
        if key in tags:
            return f"tagged {key}"
    label = f"{resource.id} {resource.name}".lower()
    if any(marker in label for marker in NAME_MARKERS):
        return "name says do-not-delete"
    if "aws:cloudformation:stack-name" in tags:
        return f"managed by CloudFormation stack {tags['aws:cloudformation:stack-name']} (delete the stack instead)"
    if not resource.deletable:
        return "GrimReaper can't safely delete this kind automatically; remove it in the console"
    return None
