"""
Azure Blob Storage helpers for PDF persistence.

Design notes:
  - `DefaultAzureCredential` gives one code path for both prod (managed identity
    on Azure infra) and local dev (falls back to Azure CLI / env credentials).
  - Blobs are named `{sha256}.pdf` inside a single container. Flat layout is
    fine at internal-tool scale; if the container ever approaches millions of
    blobs, consider sharding by `{sha256[:2]}/{sha256[2:4]}/...`.
  - Dedup is handled by the DB unique index on `sha256_hash`. This module
    treats a pre-existing blob as success (content is identical by definition)
    rather than an error — the DB insert is what ultimately rejects duplicates.
  - Size guards live in the caller (extractor / app). Storage does what it is
    told; it does not second-guess the bytes handed to it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

logger = logging.getLogger(__name__)

PDF_CONTENT_TYPE = "application/pdf"

# User delegation SAS is scoped by how long the delegation key itself is valid;
# Azure caps that at 7 days. We refresh the key hourly and issue short-lived
# per-blob SAS tokens on top. 5-minute margin avoids handing out a SAS that
# expires mid-request.
_UDK_LIFETIME = timedelta(hours=1)
_UDK_REFRESH_MARGIN = timedelta(minutes=5)
_SAS_LIFETIME = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Config + client
# ---------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set. See .env.example.")
    return value


@lru_cache(maxsize=1)
def _service_client() -> BlobServiceClient:
    """Lazy, cached BlobServiceClient. Used for both blob I/O and user
    delegation key issuance."""
    account = _required_env("AZURE_STORAGE_ACCOUNT")
    account_url = f"https://{account}.blob.core.windows.net"
    # DefaultAzureCredential is lazy — it does not authenticate until the first
    # request, so constructing the client here makes no network call.
    credential = DefaultAzureCredential()
    return BlobServiceClient(account_url=account_url, credential=credential)


@lru_cache(maxsize=1)
def _container_client():
    """Lazy, cached BlobContainerClient bound to the configured container.

    Cached so we reuse connection pools across calls within a Streamlit session.
    """
    container = _required_env("AZURE_BLOB_CONTAINER")
    return _service_client().get_container_client(container)


_udk_cache: dict = {"key": None, "expires_on": None}


def _get_user_delegation_key():
    """Return a cached user delegation key, refreshing when it's close to expiry.

    `DefaultAzureCredential` is OAuth-based (no account key), so SAS tokens must
    be signed with a user delegation key fetched from the service. The key is
    good for up to 7 days; we use 1 hour to keep the blast radius small.
    """
    now = datetime.now(timezone.utc)
    expires = _udk_cache.get("expires_on")
    key = _udk_cache.get("key")
    if key is not None and expires is not None and expires - _UDK_REFRESH_MARGIN > now:
        return key
    start = now - timedelta(minutes=5)
    expiry = now + _UDK_LIFETIME
    key = _service_client().get_user_delegation_key(
        key_start_time=start, key_expiry_time=expiry
    )
    _udk_cache["key"] = key
    _udk_cache["expires_on"] = expiry
    return key


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_sha256(data: bytes) -> str:
    """Hex SHA-256 of `data`. Used as both the dedup key and the blob name."""
    return hashlib.sha256(data).hexdigest()


def blob_path_for(sha256: str) -> str:
    """Relative blob path (container-relative) for a given hash."""
    return f"{sha256}.pdf"


def upload_pdf(data: bytes, sha256: str, original_filename: str) -> str:
    """
    Upload a PDF to blob storage and return its relative path.

    If a blob with the same hash already exists, the upload is skipped and the
    existing path is returned — identical bytes by definition, so this is safe.

    Args:
        data: raw PDF bytes.
        sha256: pre-computed hex SHA-256 of `data`. The caller already has this
            from the dedup check; passing it in avoids re-hashing.
        original_filename: retained as blob metadata for forensics only.

    Returns:
        The relative blob path to persist in the DB.

    Raises:
        azure.core.exceptions.HttpResponseError: on unrecoverable storage errors.
    """
    blob_name = blob_path_for(sha256)
    blob = _container_client().get_blob_client(blob_name)
    try:
        blob.upload_blob(
            data,
            overwrite=False,
            content_settings=ContentSettings(content_type=PDF_CONTENT_TYPE),
            metadata={"original_filename": original_filename, "sha256": sha256},
        )
        logger.info("Uploaded blob %s (%d bytes)", blob_name, len(data))
    except ResourceExistsError:
        # Concurrent upload of the same PDF, or a prior DB insert failed after
        # the blob went up. Either way the bytes are identical, so this is a
        # no-op at the storage layer.
        logger.info("Blob %s already exists; skipping upload", blob_name)
    return blob_name


def get_pdf_url(blob_path: str) -> str:
    """Generate a time-limited read-only URL for the given blob.

    Uses a user delegation SAS so the same code path works for both local dev
    (service principal) and prod (managed identity) — no account key required.
    """
    account = _required_env("AZURE_STORAGE_ACCOUNT")
    container = _required_env("AZURE_BLOB_CONTAINER")
    udk = _get_user_delegation_key()
    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=blob_path,
        user_delegation_key=udk,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + _SAS_LIFETIME,
    )
    return f"https://{account}.blob.core.windows.net/{container}/{blob_path}?{sas}"


def download_pdf(blob_path: str) -> bytes:
    """
    Fetch PDF bytes for the given relative path.

    Raises:
        FileNotFoundError: if no blob exists at `blob_path` — re-raised rather
            than leaking Azure-specific exceptions to callers that only care
            whether the file is there.
    """
    blob = _container_client().get_blob_client(blob_path)
    try:
        downloader = blob.download_blob()
        return downloader.readall()
    except ResourceNotFoundError as exc:
        raise FileNotFoundError(f"Blob not found: {blob_path}") from exc
