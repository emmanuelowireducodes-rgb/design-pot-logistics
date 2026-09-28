"""Compatibility symbols needed to load the already-applied POD image migration.

No runtime upload, image processing, storage, or delivery-photo endpoint uses these.
"""

from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage

private_storage = FileSystemStorage(location=Path(settings.BASE_DIR) / "legacy_pod_storage")


def proof_path(instance, filename):
    return f"legacy-pod/{instance.delivery_id}/{filename}"
