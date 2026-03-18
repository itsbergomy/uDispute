"""
Letter delivery service — DocuPost integration for mailing dispute letters.
Extracted from dispute_ui.py.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

DOCUPOST_API_TOKEN = os.getenv("DOCUPOST_API_TOKEN")
DOCUPOST_SENDLETTER_URL = "https://app.docupost.com/api/1.1/wf/sendletter"


def get_docupost_token(user_id=None):
    """Resolve DocuPost API token — check user's BYOK key first, fall back to env var."""
    if user_id:
        try:
            from models import UserSetting
            from services.encryption import decrypt_value
            setting = UserSetting.query.filter_by(user_id=user_id, key='docupost_api_token').first()
            if setting and setting.value:
                return decrypt_value(setting.value)
        except Exception:
            pass  # Fall back to platform key
    return DOCUPOST_API_TOKEN


def mail_letter_via_docupost(pdf_url=None, html_content=None, recipient=None, sender=None, mail_options=None, api_token=None):
    """
    Send a letter via DocuPost USPS mailing service.

    Accepts EITHER a public pdf_url OR html_content (raw HTML sent in the body).
    DocuPost requires the PDF to be publicly accessible — local/auth-gated URLs won't work.

    Args:
        pdf_url: URL to a publicly hosted PDF (no auth required).
        html_content: Raw HTML string (up to 9000 chars). Used if pdf_url is None.
        recipient: Dict with keys: name, company, address1, address2, city, state, zip.
        sender: Dict with keys: name, company, address1, address2, city, state, zip.
        mail_options: Optional dict with keys: mail_class, servicelevel, color,
                      doublesided, return_envelope, description.
        api_token: Optional BYOK token. Falls back to DOCUPOST_API_TOKEN env var.

    Returns:
        Dict with 'success' bool and 'response' or 'error'.
    """
    token = api_token or DOCUPOST_API_TOKEN
    if not token:
        return {'success': False, 'error': 'DocuPost API token not configured'}

    if not pdf_url and not html_content:
        return {'success': False, 'error': 'No PDF URL or HTML content provided'}

    recipient = recipient or {}
    sender = sender or {}
    options = mail_options or {}

    params = {
        'api_token': token,
        # Recipient
        'to_name': recipient.get('name', ''),
        'to_company': recipient.get('company', ''),
        'to_address1': recipient.get('address1', ''),
        'to_address2': recipient.get('address2', ''),
        'to_city': recipient.get('city', ''),
        'to_state': recipient.get('state', ''),
        'to_zip': recipient.get('zip', ''),
        # Sender
        'from_name': sender.get('name', ''),
        'from_company': sender.get('company', ''),
        'from_address1': sender.get('address1', ''),
        'from_address2': sender.get('address2', ''),
        'from_city': sender.get('city', ''),
        'from_state': sender.get('state', ''),
        'from_zip': sender.get('zip', ''),
        # Mail options
        'class': options.get('mail_class', 'usps_first_class'),
        'servicelevel': options.get('servicelevel', ''),
        'color': options.get('color', 'false'),
        'doublesided': options.get('doublesided', 'true'),
        'return_envelope': options.get('return_envelope', 'false'),
        'description': options.get('description', ''),
    }

    # DocuPost accepts either a PDF URL (query param) or HTML (request body)
    if pdf_url:
        params['pdf'] = pdf_url
        body = None
    else:
        body = html_content

    try:
        if body:
            resp = requests.post(DOCUPOST_SENDLETTER_URL, params=params, data=body,
                                 headers={'Content-Type': 'text/html'})
        else:
            resp = requests.post(DOCUPOST_SENDLETTER_URL, params=params)
        print(f"[DocuPost] status={resp.status_code} body={resp.text[:500]}")

        # Try to parse JSON first — DocuPost returns 200 even on errors
        try:
            data = resp.json()
            print(f"[DocuPost] parsed JSON keys: {list(data.keys())}")
        except (ValueError, KeyError):
            data = {}

        # Check for error in JSON body (DocuPost returns 200 + {"error": "..."})
        if data.get('error'):
            print(f"[DocuPost] API ERROR: {data['error']}")
            return {'success': False, 'error': data['error']}

        if resp.status_code != 200 or b"<Error>" in resp.content:
            print(f"[DocuPost] HTTP ERROR: {resp.text[:500]}")
            return {'success': False, 'error': resp.text}

        # Success — extract tracking info
        result = {'success': True, 'response': resp.text}
        result['letter_id'] = (
            data.get('letter_id') or
            data.get('letterId') or
            data.get('id')
        )
        result['cost'] = (
            data.get('cost') or
            data.get('total_cost') or
            data.get('price')
        )
        return result
    except Exception as e:
        print(f"[DocuPost] EXCEPTION: {e}")
        return {'success': False, 'error': str(e)}
