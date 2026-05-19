"""
Helper for assembling LiteLLM image content blocks from GCS URIs.
Imported lazily by the runner so text-only runs don't pay the GCS
dependency cost.
"""

import base64
import os
from io import BytesIO

from google.cloud import storage
from PIL import Image

GCS_BUCKET = os.environ.get("GCS_BUCKET", "uc-and-d-assets")


def _client() -> storage.Client:
    return storage.Client()


def _uri_to_b64(uri: str, gcs: storage.Client, max_width: int = 1568) -> str:
    path = uri.removeprefix(f"gs://{GCS_BUCKET}/")
    blob = gcs.bucket(GCS_BUCKET).blob(path)
    data = blob.download_as_bytes()
    img = Image.open(BytesIO(data))
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode()


def image_content_blocks(uris: list[str], page_start: int) -> list[dict]:
    gcs = _client()
    blocks: list[dict] = []
    for i, uri in enumerate(uris):
        page_num = page_start + i
        blocks.append({"type": "text", "text": f"--- Page {page_num} (image) ---"})
        b64 = _uri_to_b64(uri, gcs)
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    return blocks
