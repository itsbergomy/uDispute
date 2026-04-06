"""
Cloud Storage Service — Cloudinary integration for uDispute.

Handles all file uploads, downloads, URL signing, and deletions via Cloudinary.
Replaces local filesystem storage so files persist across deploys.

Every URL returned by this module is **signed** — this bypasses Cloudinary's
'Restrict unsigned raw access' setting and ensures files are always accessible.

Usage:
    from services.cloud_storage import (
        upload_file, get_file_url, get_signed_url,
        download_to_temp, delete_file, is_configured
    )

    # Upload a Flask FileStorage object
    result = upload_file(file_obj, folder="clients/5", resource_type="raw")
    # result = { 'public_id': '...', 'secure_url': 'https://...' }

    # Get a signed URL for browser display (inline for PDFs)
    url = get_signed_url(result['secure_url'], inline=True)

    # Get a signed URL for download
    url = get_file_url(public_id_or_url)

    # Download to a temp file for processing (e.g., PDF parsing)
    temp_path = download_to_temp(public_id_or_url)

    # Delete (works with both public_id and full URL)
    delete_file(public_id_or_url)
"""

import os
import re
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


# ═══════════════════════════════════════════════════════════
#  Internal Utilities
# ═══════════════════════════════════════════════════════════

def _parse_cloudinary_url(url):
    """
    Extract (public_id, resource_type) from a Cloudinary URL.

    Handles:
        https://res.cloudinary.com/{cloud}/raw/upload/v{ver}/{public_id}
        https://res.cloudinary.com/{cloud}/image/upload/{public_id}

    Returns:
        (public_id, resource_type) on success
        (None, None) if the URL can't be parsed or isn't a Cloudinary URL
    """
    if not url or not url.startswith('http'):
        return None, None

    # Detect resource_type from the URL path segment
    resource_type = 'raw'
    if '/image/upload/' in url:
        resource_type = 'image'
    elif '/video/upload/' in url:
        resource_type = 'video'

    # Primary: /v{digits}/{public_id} at the end
    m = re.search(r'/v\d+/(.+)$', url)
    if not m:
        # Fallback: everything after /upload/
        m = re.search(r'/upload/(.+)$', url)
    if m:
        return m.group(1), resource_type

    return None, None


def _is_cloudinary_host(url):
    """Check if a URL points to a known Cloudinary domain (SSRF guard)."""
    if not url:
        return False
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    return hostname.endswith('res.cloudinary.com') or hostname.endswith('cloudinary.com')


# ═══════════════════════════════════════════════════════════
#  Upload
# ═══════════════════════════════════════════════════════════

def upload_file(file_obj, folder="", filename=None, resource_type="auto"):
    """
    Upload a file to Cloudinary.

    Args:
        file_obj: Flask FileStorage object, file path string, or file-like object
        folder: Subfolder within udispute/ (e.g., "clients/5", "users/12")
        filename: Optional custom filename (without extension). If None, uses original.
        resource_type: "auto", "image", or "raw" (use "raw" for PDFs/docs)

    Returns:
        dict with 'public_id', 'url', 'secure_url', 'resource_type', 'format',
        'original_filename' — or None on failure
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

        if isinstance(file_obj, str) or hasattr(file_obj, 'read'):
            result = cloudinary.uploader.upload(file_obj, **upload_opts)
        else:
            print(f"[CloudStorage] Unsupported file_obj type: {type(file_obj)}")
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


# ═══════════════════════════════════════════════════════════
#  URL Generation (always signed)
# ═══════════════════════════════════════════════════════════

def get_file_url(public_id_or_url, resource_type="raw", inline=False):
    """
    Get a **signed** Cloudinary URL for a stored file.

    Accepts either a bare public_id or a full Cloudinary URL.
    Full URLs are parsed to extract the public_id, then re-signed.
    This ensures access even when 'Restrict unsigned raw access' is enabled.

    Args:
        public_id_or_url: Cloudinary public_id string or full https:// URL
        resource_type: "raw", "image", "video". Auto-detected if input is a URL.
        inline: If True, adds fl_attachment:false so browsers display
                the file inline (PDFs render in-tab instead of downloading).

    Returns:
        Signed HTTPS URL string, or None on failure.
    """
    if not public_id_or_url:
        return None

    # If it's a full URL, extract the public_id so we can re-sign it
    if public_id_or_url.startswith('http'):
        parsed_id, parsed_type = _parse_cloudinary_url(public_id_or_url)
        if parsed_id:
            public_id_or_url = parsed_id
            resource_type = parsed_type
        else:
            # Not a Cloudinary URL or unparseable — return as-is
            return public_id_or_url

    try:
        opts = {
            "resource_type": resource_type,
            "secure": True,
            "sign_url": True,
            "type": "upload",
        }
        if inline:
            opts["flags"] = "attachment:false"

        url = cloudinary.utils.cloudinary_url(public_id_or_url, **opts)
        return url[0] if isinstance(url, tuple) else url

    except Exception as e:
        print(f"[CloudStorage] URL generation error: {e}")
        return None


def get_signed_url(url_or_public_id, resource_type=None, inline=True):
    """
    Convenience wrapper — returns a signed URL with inline display by default.

    Use this when serving files to the browser (PDFs, images in new tabs).
    The `inline=True` default adds fl_attachment:false so PDFs render
    in the browser instead of triggering a download.
    """
    return get_file_url(
        url_or_public_id,
        resource_type=resource_type or "raw",
        inline=inline,
    )


# ═══════════════════════════════════════════════════════════
#  Download (server-side)
# ═══════════════════════════════════════════════════════════

def download_to_temp(public_id_or_url, suffix=".pdf"):
    """
    Download a Cloudinary file to a temporary local file for processing.
    Returns the temp file path, or None on failure.
    Caller is responsible for cleanup (os.unlink).

    Uses signed URLs internally — works regardless of Cloudinary access settings.
    """
    url = get_file_url(public_id_or_url)
    if not url:
        return None

    # Sanitize suffix — must be a short extension like .pdf, .png, .jpg
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

    except http_requests.RequestException as e:
        print(f"[CloudStorage] Download failed ({resp.status_code if 'resp' in dir() else '?'}): {e}")
        return None
    except Exception as e:
        print(f"[CloudStorage] Download error: {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  Delete
# ═══════════════════════════════════════════════════════════

def delete_file(public_id_or_url, resource_type="raw"):
    """
    Delete a file from Cloudinary.

    Accepts either a bare public_id or a full Cloudinary URL.
    URLs are parsed to extract the public_id before deletion.

    Returns:
        True on success, False on failure
    """
    if not public_id_or_url:
        return False

    # Extract public_id from URL if needed
    if public_id_or_url.startswith('http'):
        parsed_id, parsed_type = _parse_cloudinary_url(public_id_or_url)
        if not parsed_id:
            print(f"[CloudStorage] Cannot extract public_id for deletion: {public_id_or_url}")
            return False
        public_id_or_url = parsed_id
        resource_type = parsed_type

    try:
        result = cloudinary.uploader.destroy(public_id_or_url, resource_type=resource_type)
        return result.get("result") == "ok"
    except Exception as e:
        print(f"[CloudStorage] Delete error: {e}")
        return False


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def is_cloudinary_url(url):
    """Check if a URL is a Cloudinary URL."""
    return _is_cloudinary_host(url)


def is_configured():
    """Check if Cloudinary credentials are set."""
    cfg = cloudinary.config()
    return bool(cfg.cloud_name and cfg.api_key and cfg.api_secret)
