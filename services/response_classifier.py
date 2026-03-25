"""
Response Classifier — auto-classifies bureau response letters by matching them
to dispute accounts and determining the outcome.
Uses text extraction (pdfplumber) with GPT-4o vision fallback.
"""

import os
import re
import logging

logger = logging.getLogger(__name__)

# Keywords for outcome classification
_OUTCOME_PATTERNS = {
    'removed': [
        r'has been deleted',
        r'has been removed',
        r'will be deleted',
        r'will be removed',
        r'account.*removed',
        r'deleted.*from.*report',
        r'no longer appears',
    ],
    'updated': [
        r'has been (updated|modified|corrected)',
        r'information.*corrected',
        r'account.*updated',
        r'changes.*made',
        r'been modified',
    ],
    'verified': [
        r'verified as accurate',
        r'has been verified',
        r'information.*accurate',
        r'investigation.*verified',
        r'confirmed.*accurate',
        r'reporting.*correct',
        r'verified.*reported',
    ],
    'no_response': [],  # This is determined by time, not text
}


def extract_text_from_file(file_path):
    """Extract text from a PDF or image file."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.pdf':
        try:
            import pdfplumber
            text = ''
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages[:5]:  # Max 5 pages
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + '\n'
            return text.strip()
        except Exception as e:
            logger.warning(f"pdfplumber failed on {file_path}: {e}")
            return ''
    elif ext in ('.png', '.jpg', '.jpeg'):
        # For images, try OCR via GPT-4o vision
        try:
            return _vision_extract(file_path)
        except Exception as e:
            logger.warning(f"Vision extract failed on {file_path}: {e}")
            return ''
    return ''


def _vision_extract(file_path):
    """Use GPT-4o vision to extract text from an image."""
    import base64
    from openai import OpenAI

    client = OpenAI()
    with open(file_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    ext = os.path.splitext(file_path)[1].lower().replace('.', '')
    mime = f"image/{ext}" if ext != 'jpg' else 'image/jpeg'

    resp = client.chat.completions.create(
        model='gpt-4o',
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Extract all text from this document image. Return only the text content, nothing else.'},
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}},
            ],
        }],
        temperature=0,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


def classify_outcome(text):
    """
    Classify the outcome from response letter text.
    Returns (outcome, confidence) tuple.
    """
    text_lower = text.lower()

    scores = {}
    for outcome, patterns in _OUTCOME_PATTERNS.items():
        if not patterns:
            continue
        match_count = sum(1 for p in patterns if re.search(p, text_lower))
        if match_count > 0:
            scores[outcome] = match_count

    if not scores:
        return None, 0.0

    best = max(scores, key=scores.get)
    confidence = min(scores[best] / 3.0, 1.0)  # 3+ pattern matches = full confidence
    return best, round(confidence, 2)


def match_to_account(text, accounts):
    """
    Match response letter text to a specific dispute account.

    Args:
        text: Extracted text from the response letter
        accounts: List of DisputeAccount objects or dicts with account_name, account_number

    Returns:
        (matched_account, confidence) or (None, 0.0)
    """
    text_upper = text.upper()
    best_match = None
    best_score = 0

    for acct in accounts:
        score = 0
        name = acct.account_name if hasattr(acct, 'account_name') else acct.get('account_name', '')
        number = acct.account_number if hasattr(acct, 'account_number') else acct.get('account_number', '')

        # Check account number (strongest signal)
        if number:
            # Strip masking characters and check for digit overlap
            clean_number = re.sub(r'[^0-9]', '', number)
            if len(clean_number) >= 4:
                # Check if last 4 digits appear in text
                last4 = clean_number[-4:]
                if last4 in text:
                    score += 3

                # Check if longer sequence appears
                if len(clean_number) >= 6:
                    last6 = clean_number[-6:]
                    if last6 in text:
                        score += 2

        # Check account/creditor name
        if name:
            # Normalize name for matching
            clean_name = re.sub(r'[#\d]+$', '', name).strip().upper()
            if clean_name and clean_name in text_upper:
                score += 2

            # Check individual words (for partial matches like "CAP ONE" vs "CAPITAL ONE")
            name_words = [w for w in clean_name.split() if len(w) > 2]
            word_matches = sum(1 for w in name_words if w in text_upper)
            if word_matches >= 2:
                score += 1

        if score > best_score:
            best_score = score
            best_match = acct

    confidence = min(best_score / 5.0, 1.0)  # 5+ signals = full confidence
    return best_match, round(confidence, 2)


def classify_response_file(file_path, accounts):
    """
    Full classification pipeline: extract text, match to account, determine outcome.

    Args:
        file_path: Path to the uploaded response file
        accounts: List of DisputeAccount objects with pending outcomes

    Returns:
        dict with account_id, account_name, outcome, confidence, or None if no match
    """
    text = extract_text_from_file(file_path)
    if not text:
        return None

    # Match to account
    matched_account, match_confidence = match_to_account(text, accounts)
    if not matched_account or match_confidence < 0.3:
        return None

    # Classify outcome
    outcome, outcome_confidence = classify_outcome(text)
    if not outcome:
        return None

    account_id = matched_account.id if hasattr(matched_account, 'id') else matched_account.get('id')
    account_name = matched_account.account_name if hasattr(matched_account, 'account_name') else matched_account.get('account_name', '')

    return {
        'account_id': account_id,
        'account_name': account_name,
        'outcome': outcome,
        'match_confidence': match_confidence,
        'outcome_confidence': outcome_confidence,
        'text_preview': text[:300],
    }
