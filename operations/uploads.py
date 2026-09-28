"""Private, validated POD images. Never expose the storage directory as /media/."""

import hashlib
import io
import uuid
import warnings
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from PIL import Image, ImageOps, UnidentifiedImageError


class PrivatePhotoStorage(FileSystemStorage):
    def url(self, name):
        raise ValueError("POD photos require the authenticated proof-download endpoint.")


class BoundedImageUploadHandler(FileUploadHandler):
    def __init__(self, request=None):
        super().__init__(request)
        self.received = 0

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > settings.POD_MAX_UPLOAD_BYTES:
            if self.request is not None:
                setattr(self.request, "pod_upload_rejected", True)
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


def private_storage():
    return PrivatePhotoStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


def proof_path(instance, filename):
    return f"pod/{instance.delivery_id}/{uuid.uuid4().hex}.jpg"


def sanitize_photo(upload):
    if upload is None:
        return None
    if upload.size > settings.POD_MAX_UPLOAD_BYTES:
        raise ValidationError("Choose a photo smaller than 5 MB.")
    if Path(upload.name).suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise ValidationError("Choose a JPEG or PNG photo.")
    raw = upload.read(settings.POD_MAX_UPLOAD_BYTES + 1)
    upload.seek(0)
    if len(raw) > settings.POD_MAX_UPLOAD_BYTES:
        raise ValidationError("Choose a photo smaller than 5 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as check:
                if check.format not in ("JPEG", "PNG") or check.width * check.height > 16_000_000:
                    raise ValidationError("Use a JPEG or PNG photo of at most 16 megapixels.")
                check.verify()
            with Image.open(io.BytesIO(raw)) as source:
                # Decode and re-encode to strip metadata, appended content and original names.
                clean = ImageOps.exif_transpose(source).convert("RGB")
                clean.thumbnail((1600, 1600))
                output = io.BytesIO()
                clean.save(output, "JPEG", quality=85)
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise ValidationError("This file could not be read as a safe photo.") from exc
    data = output.getvalue()
    return ContentFile(data, name="proof.jpg"), hashlib.sha256(data).hexdigest()
