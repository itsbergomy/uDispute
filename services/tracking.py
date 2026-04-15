"""
Letter delivery tracking — polls DocuPost for status updates on mailed letters.
"""

import logging
from datetime import datetime
import requests
from models import db, ClientDisputeLetter, MailedLetter
from services.delivery import get_docupost_token

logger = logging.getLogger(__name__)

DOCUPOST_STATUS_URL = "https://app.docupost.com/api/1.1/wf/letterstatus"

# Active statuses that should be polled
ACTIVE_STATUSES = {'submitted', 'queued', 'processing', 'in_transit'}

# Normalize DocuPost status names to internal statuses
STATUS_MAP = {
    'submitted': 'submitted',
    'queued': 'queued',
    'processing': 'processing',
    'printed': 'processing',
    'in transit': 'in_transit',
    'in_transit': 'in_transit',
    'mailed': 'in_transit',
    'delivered': 'delivered',
    'returned': 'returned',
    'error': 'error',
    'cancelled': 'cancelled',
}


def _poll_docupost(letter, docupost_id, user_id=None):
    """
    Shared logic for polling DocuPost for a letter's delivery status.
    Updates the given letter record in-place and commits.
    Works for both ClientDisputeLetter (Business) and MailedLetter (Pro).
    """
    token = get_docupost_token(user_id)
    if not token:
        return {'status': letter.delivery_status, 'updated': False, 'error': 'No API token'}

    try:
        resp = requests.get(DOCUPOST_STATUS_URL, params={
            'api_token': token,
            'letter_id': docupost_id,
        }, timeout=15)

        if resp.status_code != 200:
            logger.warning(f"DocuPost status check failed: HTTP {resp.status_code}")
            return {'status': letter.delivery_status, 'updated': False, 'error': f'HTTP {resp.status_code}'}

        data = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}

        new_status = (data.get('status') or data.get('delivery_status') or '').lower()
        tracking = data.get('tracking_number') or data.get('tracking') or ''
        normalized = STATUS_MAP.get(new_status, new_status or letter.delivery_status)

        updated = False
        if normalized and normalized != letter.delivery_status:
            letter.delivery_status = normalized
            updated = True
        if tracking and tracking != letter.tracking_number:
            letter.tracking_number = tracking
            updated = True

        letter.delivery_status_updated_at = datetime.utcnow()
        db.session.commit()

        return {
            'status': letter.delivery_status,
            'tracking_number': letter.tracking_number,
            'updated': updated,
        }

    except Exception as e:
        logger.error(f"DocuPost status poll error: {e}")
        return {'status': letter.delivery_status, 'updated': False, 'error': str(e)}


def poll_mailed_letter_status(mailed_letter_id, user_id=None):
    """
    Poll DocuPost for delivery status of a Pro user's MailedLetter.
    """
    letter = MailedLetter.query.get(mailed_letter_id)
    if not letter or not letter.docupost_letter_id:
        return {'status': None, 'updated': False, 'error': 'No DocuPost ID'}
    if user_id and letter.user_id != user_id:
        return {'status': None, 'updated': False, 'error': 'Unauthorized'}
    return _poll_docupost(letter, letter.docupost_letter_id, user_id)


def poll_all_user_mailed_letters(user_id):
    """
    Poll DocuPost for all of a user's active (non-delivered) mailed letters.
    Returns summary: polled, updated, errors.
    """
    letters = MailedLetter.query.filter(
        MailedLetter.user_id == user_id,
        MailedLetter.docupost_letter_id.isnot(None),
        MailedLetter.delivery_status.in_(list(ACTIVE_STATUSES) + [None])
    ).all()

    polled = 0
    updated = 0
    errors = 0
    for letter in letters:
        result = _poll_docupost(letter, letter.docupost_letter_id, user_id)
        polled += 1
        if result.get('updated'):
            updated += 1
        if result.get('error'):
            errors += 1
    return {'polled': polled, 'updated': updated, 'errors': errors}


def poll_letter_status(letter_id, user_id=None):
    """
    Poll DocuPost for the current delivery status of a single letter.
    Updates the letter record in-place and commits.

    Returns dict with 'status' and 'updated' flag.
    """
    letter = ClientDisputeLetter.query.get(letter_id)
    if not letter or not letter.docupost_letter_id:
        return {'status': None, 'updated': False, 'error': 'No DocuPost ID'}

    token = get_docupost_token(user_id)
    if not token:
        return {'status': letter.delivery_status, 'updated': False, 'error': 'No API token'}

    try:
        resp = requests.get(DOCUPOST_STATUS_URL, params={
            'api_token': token,
            'letter_id': letter.docupost_letter_id,
        }, timeout=15)

        if resp.status_code != 200:
            logger.warning(f"DocuPost status check failed: HTTP {resp.status_code}")
            return {'status': letter.delivery_status, 'updated': False, 'error': f'HTTP {resp.status_code}'}

        data = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}

        new_status = data.get('status', data.get('delivery_status', '')).lower()
        tracking = data.get('tracking_number', data.get('tracking', ''))

        # Normalize status names
        status_map = {
            'queued': 'queued',
            'processing': 'processing',
            'printed': 'processing',
            'in transit': 'in_transit',
            'in_transit': 'in_transit',
            'mailed': 'in_transit',
            'delivered': 'delivered',
            'returned': 'returned',
            'error': 'error',
            'cancelled': 'cancelled',
        }
        normalized = status_map.get(new_status, new_status or letter.delivery_status)

        updated = False
        if normalized and normalized != letter.delivery_status:
            letter.delivery_status = normalized
            updated = True
        if tracking and tracking != letter.tracking_number:
            letter.tracking_number = tracking
            updated = True

        letter.delivery_status_updated_at = datetime.utcnow()
        db.session.commit()

        return {'status': letter.delivery_status, 'tracking_number': letter.tracking_number, 'updated': updated}

    except Exception as e:
        logger.error(f"DocuPost status poll error for letter {letter_id}: {e}")
        return {'status': letter.delivery_status, 'updated': False, 'error': str(e)}


def poll_all_pending(user_id=None):
    """
    Poll all letters with active delivery statuses.
    Returns summary dict with counts.
    """
    letters = ClientDisputeLetter.query.filter(
        ClientDisputeLetter.delivery_status.in_(ACTIVE_STATUSES),
        ClientDisputeLetter.docupost_letter_id.isnot(None),
    ).all()

    results = {'polled': 0, 'updated': 0, 'errors': 0}
    for letter in letters:
        result = poll_letter_status(letter.id, user_id=user_id)
        results['polled'] += 1
        if result.get('updated'):
            results['updated'] += 1
        if result.get('error'):
            results['errors'] += 1

    return results
