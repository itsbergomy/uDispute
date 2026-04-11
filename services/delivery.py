"""
Letter delivery service — DocuPost integration for mailing dispute letters.
Handles mailing via platform DocuPost account and per-letter Stripe billing.
"""

import os
import logging
import requests
import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_TEST_SECRET_KEY")

DOCUPOST_API_TOKEN = os.getenv("DOCUPOST_API_TOKEN")
DOCUPOST_SENDLETTER_URL = "https://app.docupost.com/api/1.1/wf/sendletter"

logger = logging.getLogger(__name__)

# Mailing prices in cents
MAILING_PRICES = {
    'first_class': 0,         # Included in subscription
    'usps_first_class': 0,    # Alias
    'certified': 1499,        # $14.99
    'certified_rr': 1699,     # $16.99
}


def charge_mailing_fee(user, letter_count, mail_class):
    """
    Charge the user for premium mailing via Stripe.

    Args:
        user: User model instance
        letter_count: Number of letters being mailed
        mail_class: 'first_class', 'certified', or 'certified_rr'

    Returns:
        dict with 'charged' (bool), 'amount_cents', 'error' (if failed)
    """
    # Admin accounts are never charged
    if getattr(user, 'is_admin', False):
        logger.info(f"[MAILING] Admin user {user.id} — skipping charge for {letter_count} {mail_class} letters")
        return {'charged': False, 'amount_cents': 0, 'skipped': 'admin'}

    price_per_letter = MAILING_PRICES.get(mail_class, 0)

    # First Class is included — no charge
    if price_per_letter == 0:
        return {'charged': False, 'amount_cents': 0, 'skipped': 'included'}

    total_cents = price_per_letter * letter_count

    # Need a Stripe customer ID to charge
    customer_id = getattr(user, 'stripe_customer_id', None)
    if not customer_id:
        logger.error(f"[MAILING] No Stripe customer ID for user {user.id} — cannot charge for {mail_class}")
        return {'charged': False, 'amount_cents': total_cents, 'error': 'No payment method on file'}

    try:
        # Create and confirm a PaymentIntent using the customer's default payment method
        intent = stripe.PaymentIntent.create(
            amount=total_cents,
            currency='usd',
            customer=customer_id,
            confirm=True,
            automatic_payment_methods={'enabled': True, 'allow_redirects': 'never'},
            description=f'uDispute mailing — {letter_count}x {mail_class.replace("_", " ").title()}',
            metadata={
                'user_id': str(user.id),
                'letter_count': str(letter_count),
                'mail_class': mail_class,
            },
        )

        if intent.status == 'succeeded':
            logger.info(f"[MAILING] Charged user {user.id}: ${total_cents/100:.2f} for {letter_count} {mail_class} letters")
            return {'charged': True, 'amount_cents': total_cents, 'payment_intent_id': intent.id}
        else:
            logger.error(f"[MAILING] Payment not succeeded for user {user.id}: status={intent.status}")
            return {'charged': False, 'amount_cents': total_cents, 'error': f'Payment status: {intent.status}'}

    except stripe.error.CardError as e:
        logger.error(f"[MAILING] Card declined for user {user.id}: {e}")
        return {'charged': False, 'amount_cents': total_cents, 'error': 'Card declined'}
    except stripe.error.StripeError as e:
        logger.error(f"[MAILING] Stripe error for user {user.id}: {e}")
        return {'charged': False, 'amount_cents': total_cents, 'error': str(e)}


def get_docupost_token(user_id=None):
    """Return the platform DocuPost API token. All mailing goes through the platform account."""
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
