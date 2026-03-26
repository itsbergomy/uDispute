"""
Letter Quality Gate — pre-send validation for dispute letters.
Runs 10 rule checks against every generated letter before it's sent.
Pure Python regex/keyword checks — no API calls, <50ms per letter, zero cost.
"""

import re


# ── Known Original Creditors (FDCPA does NOT apply) ──
ORIGINAL_CREDITORS = {
    'CAPITAL ONE', 'CHASE', 'JP MORGAN CHASE', 'JPMORGAN CHASE',
    'DISCOVER', 'AMERICAN EXPRESS', 'AMEX', 'WELLS FARGO',
    'BANK OF AMERICA', 'CITIBANK', 'CITI', 'US BANK',
    'BARCLAYS', 'SYNCHRONY', 'NAVY FEDERAL', 'USAA', 'PNC',
    'TD BANK', 'REGIONS', 'TRUIST', 'FIFTH THIRD', 'ALLY',
    'SOFI', 'MARCUS', 'GEORGIAS OWN', 'GEORGIA\'S OWN',
    'BRIDGECREST', 'EDFINANCIAL', 'NELNET', 'NAVIENT',
    'GREAT LAKES', 'MOHELA', 'SALLIE MAE', 'TOYOTA FINANCIAL',
    'HONDA FINANCIAL', 'BMW FINANCIAL', 'FORD MOTOR CREDIT',
}

# ── Known Debt Collectors (FDCPA applies) ──
DEBT_COLLECTORS = {
    'LVNV FUNDING', 'MIDLAND CREDIT', 'PORTFOLIO RECOVERY',
    'PRA GROUP', 'CONVERGENT', 'ENHANCED RECOVERY', 'IC SYSTEM',
    'TRANSWORLD', 'AFNI', 'ALLIED INTERSTATE', 'ASSET ACCEPTANCE',
    'CAVALRY', 'CREDIT CORP', 'ENCORE CAPITAL',
    'FIRST SOURCE ADVANTAGE', 'JEFFERSON CAPITAL',
    'RESURGENT CAPITAL', 'UNIFIN',
}

# ── Phantom document patterns (evidence fabrication) ──
PHANTOM_DOC_PATTERNS = [
    r'1099[\s-]?C',
    r'attached chat log',
    r'see attached.*(?:receipt|log|statement|record)',
    r'payment receipt.*attached',
    r'(?:enclosed|attached).*(?:bank statement|canceled check)',
    r'internal notes?\s*\(see attached',
]

# ── Blank placeholder patterns ──
BLANK_PATTERNS = [
    r'\$[_]{2,}',           # $____ or $___
    r'[_]{3,}/[_]{3,}',     # ____/____
    r'[_]{3,}',             # any run of 3+ underscores
    r'\[AMOUNT\]',
    r'\[DATE\]',
    r'\[BALANCE\]',
    r'\[DOFD\]',
    r'\[INSERT',
    r'\[YOUR\s',
    r'\[ACCOUNT',
    r'\[SIGNATURE\]',
    r'\[YOUR NAME\]',
    r'\[ADDRESS\]',
    r'\[CITY\]',
    r'\[STATE\]',
    r'\[ZIP\]',
]

# ── FDCPA section patterns ──
FDCPA_PATTERNS = [
    r'§\s*1692',
    r'FDCPA',
    r'Fair Debt Collection Practices Act',
    r'15\s*U\.?S\.?C\.?\s*§?\s*1692',
]

# ── Strategy-specific keywords ──
STRATEGY_KEYWORDS = {
    'arbitration': ['arbitration', 'arbitration clause', 'binding arbitration', 'AAA', 'JAMS'],
    'consumer_law': ['FCRA', 'Fair Credit Reporting Act', '15 U.S.C.', 'consumer protection', 'statutory'],
    'ACDV_response': ['method of verification', 'ACDV', 'verification procedure', 'reinvestigation'],
    'default': ['FCRA', 'inaccurate', 'dispute', 'investigation'],
}


class QualityResult:
    """Result of a letter quality check."""
    def __init__(self):
        self.passed = True
        self.score = 100
        self.failures = []
        self.warnings = []

    def fail(self, rule, message):
        self.passed = False
        self.failures.append(f"[Rule {rule}] {message}")
        self.score = max(0, self.score - 15)

    def warn(self, rule, message):
        self.warnings.append(f"[Rule {rule}] {message}")
        self.score = max(0, self.score - 5)

    def to_dict(self):
        return {
            'passed': self.passed,
            'score': self.score,
            'failures': self.failures,
            'warnings': self.warnings,
        }


def _is_original_creditor(account_name):
    """Check if an account name matches a known original creditor."""
    name_upper = (account_name or '').upper().strip()
    for oc in ORIGINAL_CREDITORS:
        if oc in name_upper or name_upper.startswith(oc.split()[0]):
            return True
    return False


def check_letter_quality(
    letter_text,
    account_name='',
    account_number='',
    bureau='',
    prompt_pack='default',
    round_number=1,
    is_original_creditor=None,
    client_full_name='',
    client_address='',
    parsed_balance=None,
    parsed_dofd=None,
    user_provided_docs=None,
):
    """
    Run all 10 quality rules against a letter.
    Returns QualityResult with pass/fail, score, failures, and warnings.
    """
    result = QualityResult()
    text = letter_text or ''
    text_upper = text.upper()
    user_provided_docs = user_provided_docs or []

    # Auto-detect if original creditor when not specified
    if is_original_creditor is None:
        is_original_creditor = _is_original_creditor(account_name)

    # ══════════════════════════════════════════════════
    # Rule 1: Account Accuracy
    # ══════════════════════════════════════════════════
    if account_name:
        # Check account name or a significant portion appears in letter
        name_parts = account_name.upper().split()
        name_found = any(part in text_upper for part in name_parts if len(part) > 2)
        if not name_found:
            result.fail(1, f"Account name '{account_name}' not found in letter body")

    if account_number:
        # Check at least the last 4-6 digits appear (accounts are often masked)
        clean_num = re.sub(r'[^0-9X]', '', account_number)
        digits_only = re.sub(r'[^0-9]', '', clean_num)
        if digits_only and len(digits_only) >= 4:
            last_digits = digits_only[-4:]
            if last_digits not in text:
                # Try with X masking pattern
                if clean_num not in text and account_number not in text:
                    result.warn(1, f"Account number not clearly referenced in letter")

    # ══════════════════════════════════════════════════
    # Rule 2: Dispute Structure
    # ══════════════════════════════════════════════════
    dispute_point_patterns = [
        r'DISPUTE\s*POINT\s*[#\d]',
        r'POINT\s*[#]?\s*\d',
        r'(?:First|Second|Third|Fourth|Fifth)\s+(?:Dispute|Point|Issue)',
        r'(?:1\.|2\.|3\.)\s+(?:The|This|My|I)',
    ]
    dispute_points_found = sum(
        1 for p in dispute_point_patterns
        if re.search(p, text, re.IGNORECASE)
    )
    if dispute_points_found == 0:
        # Check for numbered items as alternative structure
        numbered = re.findall(r'^\s*\d+[\.\)]\s+', text, re.MULTILINE)
        if len(numbered) < 2:
            result.warn(2, "Letter may lack structured dispute points")

    # Check for Metro 2 field references
    metro2_pattern = r'(?:Field|Fld)\s*(?:#?\s*)?\d{1,2}|Metro\s*2'
    if not re.search(metro2_pattern, text, re.IGNORECASE):
        result.warn(2, "No Metro 2 field references found — consider adding for stronger dispute")

    # Length check
    word_count = len(text.split())
    if word_count < 150:
        result.fail(2, f"Letter too short ({word_count} words) — minimum 150 for effective dispute")
    elif word_count > 3000:
        result.warn(2, f"Letter may be too long ({word_count} words) — consider trimming")

    # ══════════════════════════════════════════════════
    # Rule 3: Evidence Integrity
    # ══════════════════════════════════════════════════
    for pattern in PHANTOM_DOC_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            matched_text = match.group(0)
            # Check if user actually provided this document
            doc_types_provided = [d.lower() for d in user_provided_docs]
            if '1099' in matched_text.lower() and '1099-c' not in doc_types_provided:
                result.fail(3, f"References '{matched_text}' but consumer hasn't provided this document")
            elif 'chat log' in matched_text.lower() and 'chat_log' not in doc_types_provided:
                result.fail(3, f"References '{matched_text}' — possible evidence fabrication")
            elif 'attached' in matched_text.lower():
                result.warn(3, f"References attached document: '{matched_text}' — verify consumer provided it")

    # ══════════════════════════════════════════════════
    # Rule 4: Legal Citation Accuracy (FDCPA Guard)
    # ══════════════════════════════════════════════════
    if is_original_creditor:
        for pattern in FDCPA_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                result.fail(4, f"FDCPA cited against original creditor '{account_name}' — FDCPA only applies to debt collectors")
                break

    # Check FCRA citations exist (should be in every dispute letter)
    if not re.search(r'FCRA|Fair Credit Reporting Act|15\s*U\.?S\.?C\.?\s*§?\s*168[1-9]', text, re.IGNORECASE):
        result.warn(4, "No FCRA citation found — most dispute letters should reference FCRA")

    # ══════════════════════════════════════════════════
    # Rule 5: Strategy Alignment
    # ══════════════════════════════════════════════════
    keywords = STRATEGY_KEYWORDS.get(prompt_pack, STRATEGY_KEYWORDS['default'])
    keywords_found = sum(1 for kw in keywords if kw.lower() in text.lower())
    if keywords_found == 0:
        result.warn(5, f"Letter doesn't contain expected keywords for '{prompt_pack}' pack")

    # ══════════════════════════════════════════════════
    # Rule 6: Escalation Continuity (Round 2+)
    # ══════════════════════════════════════════════════
    if round_number >= 2:
        escalation_indicators = [
            r'previous(?:ly)?\s+(?:sent|filed|submitted|disputed)',
            r'prior\s+(?:dispute|letter|correspondence|attempt)',
            r'(?:second|third|fourth|2nd|3rd|4th)\s+(?:request|dispute|demand|notice)',
            r'(?:failed|refused|neglected)\s+to\s+(?:respond|investigate|correct)',
            r'escalat',
            r'follow[\s-]?up',
        ]
        escalation_found = any(
            re.search(p, text, re.IGNORECASE) for p in escalation_indicators
        )
        if not escalation_found:
            result.fail(6, f"Round {round_number} letter lacks escalation language — must reference prior dispute attempts")

    # ══════════════════════════════════════════════════
    # Rule 7: Tone & Professionalism
    # ══════════════════════════════════════════════════
    profanity_patterns = [
        r'\b(?:fuck|shit|damn|ass|bitch|hell)\b',
        r'\b(?:idiot|stupid|moron|incompetent)\b',
    ]
    for pattern in profanity_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            result.fail(7, "Unprofessional language detected — remove before sending")
            break

    threat_patterns = [
        r'(?:sue|lawsuit|court)\s+(?:you|your)',
        r'(?:physical|bodily)\s+harm',
        r'(?:i will|i\'ll)\s+(?:destroy|ruin|come after)',
    ]
    for pattern in threat_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            if prompt_pack != 'arbitration':  # Arbitration letters can reference legal action
                result.warn(7, "Threatening language detected — consider softer phrasing")
            break

    # ══════════════════════════════════════════════════
    # Rule 8: Recipient Accuracy
    # ══════════════════════════════════════════════════
    bureau_names = ['EXPERIAN', 'TRANSUNION', 'TRANS UNION', 'EQUIFAX']
    if bureau:
        bureau_upper = bureau.upper()
        # Bureau letter should mention the bureau name
        if bureau_upper in ['EXPERIAN', 'TRANSUNION', 'EQUIFAX']:
            if bureau_upper not in text_upper and bureau_upper.replace(' ', '') not in text_upper.replace(' ', ''):
                result.warn(8, f"Bureau name '{bureau}' not found in letter addressed to them")

    # ══════════════════════════════════════════════════
    # Rule 9: No Blank Placeholders
    # ══════════════════════════════════════════════════
    for pattern in BLANK_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result.fail(9, f"Blank placeholder found: '{match.group(0)}' — e-OSCAR will flag this as a template")
            break  # One failure is enough, don't spam

    # Additional check: if balance/DOFD are referenced as blanks
    if parsed_balance is None:
        # If letter mentions a dollar amount with blanks
        if re.search(r'(?:balance|amount).*\$\s*[_\?]{2,}', text, re.IGNORECASE):
            result.fail(9, "Balance cited as blank — either use actual amount or rephrase without it")

    if parsed_dofd is None:
        if re.search(r'(?:DOFD|date of first delinquency).*[_\?]{2,}', text, re.IGNORECASE):
            result.fail(9, "DOFD cited as blank — either use actual date or rephrase without it")

    # ══════════════════════════════════════════════════
    # Rule 10: Signature Block Validation
    # ══════════════════════════════════════════════════
    # Check last 500 chars for signature area
    sig_area = text[-500:] if len(text) > 500 else text

    if client_full_name:
        name_parts = client_full_name.split()
        # At least last name should appear in signature area
        last_name = name_parts[-1] if name_parts else ''
        if last_name and last_name.upper() not in sig_area.upper():
            result.fail(10, f"Client name '{client_full_name}' not found in signature area")

    # Check for placeholder signatures
    sig_placeholders = [r'\[SIGNATURE\]', r'\[YOUR NAME\]', r'\[NAME\]', r'_{4,}\s*$']
    for pattern in sig_placeholders:
        if re.search(pattern, sig_area, re.IGNORECASE | re.MULTILINE):
            result.fail(10, "Signature area contains placeholder — must use actual client name")
            break

    return result


def format_failures_for_retry(result):
    """
    Format quality gate failures into a string that can be injected
    into the AI prompt for letter regeneration.
    """
    if not result.failures:
        return ''

    lines = ["QUALITY GATE FAILURES — fix these issues in the regenerated letter:"]
    for f in result.failures:
        lines.append(f"  • {f}")
    lines.append("")
    lines.append("Do NOT repeat these mistakes. If a value is unknown, rephrase without blanks.")
    return '\n'.join(lines)
