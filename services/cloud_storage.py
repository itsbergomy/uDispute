"""
Cloud Storage Service — Cloudinary integration for uDispute.

Handles all file uploads, downloads, and deletions via Cloudinary.
Replaces local filesystem storage so files persist across deploys.

Usage:
    from services.cloud_storage import upload_file, get_file_url, delete_file, download_to_temp

    # Upload a Flask FileStorage object
    result = upload_file(file_obj, folder="clients/5")
    # result = { 'public_id': 'udispute/clients/5/report.pdf', 'url': 'https://...', 'secure_url': 'https://...' }

    # Get a URL for a stored file
    url = get_file_url(public_id)

    # Download to a temp file for processing (e.g., PDF parsing)
    temp_path = download_to_temp(public_id_or_url)

    # Delete
    delete_file(public_id)
"""

import os
import tempfile
import requests as http_requests
import cloudinary
import cloudinary.uploader
import cloudinary.api

# ── Configure Cloudinary from environment ──
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', ''),
    secure=True
)

ROOT_FOLDER = "udispute"


def upload_file(file_obj, folder="", filename=None, resource_type="auto"):
    """
    Upload a file to Cloudinary.

    Args:
        file_obj: Flask FileStorage object, file path string, or file-like object
        folder: Subfolder within udispute/ (e.g., "clients/5", "users/12")
        filename: Optional custom filename (without extension). If None, uses original.
        resource_type: "auto", "image", or "raw" (use "raw" for PDFs/docs)

    Returns:
        dict with 'public_id', 'url', 'secure_url', 'resource_type'
        or None on failure
    """
    try:
        full_folder = f"{ROOT_FOLDER}/{folder}" if folder else ROOT_FOLDER

        upload_opts = {
            "folder": full_folder,
            "resource_type": resource_type,
            "overwrite": True,
            "invalidate": True,
        }

        if filename:
            upload_opts["public_id"] = filename

        # Handle different input types
        if isinstance(file_obj, str):
            # It's a file path
            result = cloudinary.uploader.upload(file_obj, **upload_opts)
        elif hasattr(file_obj, 'read'):
            # Flask FileStorage or file-like object
            result = cloudinary.uploader.upload(file_obj, **upload_opts)
        else:
            return None

        return {
            "public_id": result.get("public_id"),
            "url": result.get("url"),
            "secure_url": result.get("secure_url"),
            "resource_type": result.get("resource_type"),
            "format": result.get("format"),
            "original_filename": result.get("original_filename"),
        }

    except Exception as e:
        print(f"[CloudStorage] Upload error: {e}")
        return None


def upload_from_path(file_path, folder="", filename=None):
    """Upload a file from a local path. Convenience wrapper."""
    return upload_file(file_path, folder=folder, filename=filename, resource_type="raw")


def get_file_url(public_id_or_url, resource_type="raw"):
    """
    Get the public URL for a stored file.

    If it's already a full URL (starts with http), return as-is.
    Otherwise, build a signed Cloudinary URL from the public_id.
    Signed URLs bypass the "restrict unsigned raw access" security setting.
    """
    if not public_id_or_url:
        return None

    if public_id_or_url.startswith("http"):
        return public_id_or_url

    try:
        url = cloudinary.utils.cloudinary_url(
            public_id_or_url,
            resource_type=resource_type,
            secure=True,
            sign_url=True
        )
        return url[0] if isinstance(url, tuple) else url
    except Exception as e:
        print(f"[CloudStorage] URL generation error: {e}")
        return None


def download_to_temp(public_id_or_url, suffix=".pdf"):
    """
    Download a Cloudinary file to a temporary local file for processing.
    Returns the temp file path. Caller is responsible for cleanup.

    Use this when services need a local file path (PDF parsing, image conversion, etc.)
    """
    url = get_file_url(public_id_or_url)
    if not url:
        return None

    # Sanitize suffix — must be a short extension like .pdf, .png, .jpg
    # Callers sometimes pass mangled Cloudinary URL fragments as suffix
    if suffix and (len(suffix) > 10 or '/' in suffix):
        suffix = '.pdf'
    if suffix and not suffix.startswith('.'):
        suffix = '.' + suffix

    try:
        resp = http_requests.get(url, timeout=30)
        resp.raise_for_status()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name

    except Exception as e:
        print(f"[CloudStorage] Download error: {e}")
        return None


def delete_file(public_id, resource_type="raw"):
    """
    Delete a file from Cloudinary.

    Args:
        public_id: The Cloudinary public_id of the file
        resource_type: "raw", "image", or "video"

    Returns:
        True on success, False on failure
    """
    if not public_id:
        return False

    # If it's a URL, don't try to delete
    if public_id.startswith("http"):
        return False

    try:
        result = cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        return result.get("result") == "ok"
    except Exception as e:
        print(f"[CloudStorage] Delete error: {e}")
        return False


def is_cloudinary_url(url):
    """Check if a URL is a Cloudinary URL."""
    if not url:
        return False
    return "cloudinary" in url or "res.cloudinary.com" in url


def is_configured():
    """Check if Cloudinary credentials are set."""
    cfg = cloudinary.config()
    return bool(cfg.cloud_name and cfg.api_key and cfg.api_secret)
