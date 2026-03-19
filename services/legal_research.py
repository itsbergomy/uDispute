"""
Legal Research Agent — combines CFPB complaint data with CourtListener
case law to build evidence packages for dispute letter escalation.

Used by both Pro Plan (user-facing) and Business Plan (pipeline-driven).
"""

import logging
from services.cfpb_search import search_complaints
from services.courtlistener_search import search_fcra_cases, LANDMARK_CASES

logger = logging.getLogger(__name__)

# Map parser inaccuracy types to CFPB issue/sub_issue filters
INACCURACY_TO_CFPB_ISSUE = {
    'status_contradicts_history': {
        'issue': 'Incorrect information on your report',
        'sub_issues': ['Account status incorrect', 'Account information incorrect'],
    },
    'account_type_mismatch': {
        'issue': 'Incorrect information on your report',
        'sub_issues': ['Account information incorrect'],
    },
    'original_creditor_not_reflected': {
        'issue': 'Incorrect information on your report',
        'sub_issues': ['Account information incorrect'],
    },
    'closed_with_balance': {
        'issue': 'Incorrect information on your report',
        'sub_issues': ['Account status incorrect', 'Account information incorrect'],
    },
    'charge_off_not_in_status': {
        'issue': 'Incorrect information on your report',
        'sub_issues': ['Account status incorrect'],
    },
    'verification_inadequate': {
        'issue': 'Problem with a credit reporting company\'s investigation into an existing problem',
        'sub_issues': [
            'Their investigation did not fix an error on your report',
            'Was not notified of investigation status or results',
            'Investigation took more than 30 days',
        ],
    },
}

# FCRA section citations for each inaccuracy type
FCRA_CITATIONS = {
    'status_contradicts_history': {
        'section': '15 U.S.C. § 1681s-2(a)(1)(A)',
        'title': 'Duty of Furnishers — Accuracy',
        'text': 'A furnisher shall not report information to a consumer reporting agency '
                'if the furnisher knows or has reasonable cause to believe that the information '
                'is inaccurate.',
    },
    'account_type_mismatch': {
        'section': '15 U.S.C. § 1681e(b)',
        'title': 'Accuracy of Reports',
        'text': 'Whenever a consumer reporting agency prepares a consumer report it shall '
                'follow reasonable procedures to assure maximum possible accuracy of the '
                'information concerning the individual about whom the report relates.',
    },
    'original_creditor_not_reflected': {
        'section': '15 U.S.C. § 1681s-2(a)(1)(A)',
        'title': 'Duty of Furnishers — Accuracy',
        'text': 'A furnisher shall not report information to a consumer reporting agency '
                'if the furnisher knows or has reasonable cause to believe that the information '
                'is inaccurate.',
    },
    'closed_with_balance': {
        'section': '15 U.S.C. § 1681s-2(a)(1)(A)',
        'title': 'Duty of Furnishers — Accuracy',
        'text': 'A furnisher shall not report information to a consumer reporting agency '
                'if the furnisher knows or has reasonable cause to believe that the information '
                'is inaccurate.',
    },
    'charge_off_not_in_status': {
        'section': '15 U.S.C. § 1681s-2(a)(1)(A)',
        'title': 'Duty of Furnishers — Accuracy',
        'text': 'A furnisher shall not report information to a consumer reporting agency '
                'if the furnisher knows or has reasonable cause to believe that the information '
                'is inaccurate.',
    },
    'verification_inadequate': {
        'section': '15 U.S.C. § 1681i(a)',
        'title': 'Procedure in Case of Disputed Accuracy — Reinvestigation',
        'text': 'If the completeness or accuracy of any item of information contained in a '
                'consumer\'s file is disputed by the consumer, the agency shall conduct a '
                'reasonable reinvestigation to determine whether the disputed information '
                'is inaccurate and record the current status of the disputed information.',
    },
}


def research_dispute(company_name, inaccuracy_type=None, inaccuracy_detail=None,
                     bureau_response=None, round_number=1):
    """
    Conduct legal research for a specific dispute account.

    Args:
        company_name: The creditor or collection agency name
        inaccuracy_type: Type from parser's _detect_inaccuracies() (optional)
        inaccuracy_detail: Human-readable inaccuracy description (optional)
        bureau_response: Text of bureau's response letter (for rounds 2+)
        round_number: Current dispute round (affects research depth)

    Returns:
        dict with structured legal research package:
        {
            'cfpb_summary': {...},    # CFPB complaint statistics
            'case_law': {...},        # Relevant court opinions
            'fcra_citation': {...},   # Applicable FCRA section
            'prompt_context': str,    # Pre-formatted text for prompt injection
        }
    """
    package = {
        'cfpb_summary': None,
        'case_law': None,
        'fcra_citation': None,
        'prompt_context': '',
    }

    # 1. CFPB complaint research
    cfpb = _research_cfpb(company_name, inaccuracy_type)
    package['cfpb_summary'] = cfpb

    # 2. Case law research
    case_law = _research_case_law(company_name, inaccuracy_type)
    package['case_law'] = case_law

    # 3. FCRA citation
    if inaccuracy_type and inaccuracy_type in FCRA_CITATIONS:
        package['fcra_citation'] = FCRA_CITATIONS[inaccuracy_type]
    elif inaccuracy_type == 'verification_inadequate' or round_number >= 2:
        # After round 1, verification inadequacy is always relevant
        package['fcra_citation'] = FCRA_CITATIONS['verification_inadequate']

    # 4. Build prompt context string
    package['prompt_context'] = _build_prompt_context(
        cfpb, case_law, package['fcra_citation'],
        inaccuracy_detail, bureau_response, round_number
    )

    return package


def _research_cfpb(company_name, inaccuracy_type=None):
    """
    Search CFPB complaints and generate statistics summary.
    """
    if not company_name:
        return None

    # Search for all complaints against this company
    result = search_complaints(company_name, limit=100, has_narrative=True)

    if result.get('error'):
        return {'error': result['error'], 'total': 0}

    total = result.get('total', 0)
    complaints = result.get('complaints', [])

    if total == 0:
        return {'total': 0, 'company': company_name}

    # Analyze complaint patterns
    issue_counts = {}
    win_count = 0
    recent_complaints = []

    for c in complaints:
        # Count by issue type
        issue = c.get('issue', 'Unknown')
        issue_counts[issue] = issue_counts.get(issue, 0) + 1

        # Count wins
        response = c.get('company_response', '')
        if response in ('Closed with monetary relief', 'Closed with non-monetary relief'):
            win_count += 1

        # Collect recent complaints with narratives for context
        if c.get('narrative') and len(recent_complaints) < 3:
            recent_complaints.append({
                'date': c.get('date_received', ''),
                'issue': issue,
                'sub_issue': c.get('sub_issue', ''),
                'narrative_preview': c['narrative'][:300],
                'response': response,
            })

    # Filter for matching inaccuracy type if specified
    matching_count = 0
    if inaccuracy_type and inaccuracy_type in INACCURACY_TO_CFPB_ISSUE:
        target = INACCURACY_TO_CFPB_ISSUE[inaccuracy_type]
        target_issue = target['issue']
        target_subs = target.get('sub_issues', [])

        for c in complaints:
            if c.get('issue') == target_issue:
                if not target_subs or c.get('sub_issue') in target_subs:
                    matching_count += 1

    # Top issues
    top_issues = sorted(issue_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    win_rate = round(win_count / len(complaints) * 100, 1) if complaints else 0

    return {
        'company': company_name,
        'total': total,
        'analyzed': len(complaints),
        'matching_issue_count': matching_count,
        'win_count': win_count,
        'win_rate': win_rate,
        'top_issues': top_issues,
        'recent_examples': recent_complaints,
    }


def _research_case_law(company_name=None, inaccuracy_type=None):
    """
    Search CourtListener for relevant FCRA case law.
    Falls back to landmark cases if API isn't available.
    """
    result = search_fcra_cases(
        company_name=company_name,
        inaccuracy_type=inaccuracy_type or 'verification_inadequate',
        limit=5,
        date_after='2015-01-01',
    )

    cases = result.get('cases', [])
    landmarks = result.get('landmark_cases', [])

    return {
        'search_cases': cases,
        'landmark_cases': landmarks,
        'total': result.get('total', 0),
        'error': result.get('error'),
    }


def _build_prompt_context(cfpb, case_law, fcra_citation,
                          inaccuracy_detail, bureau_response, round_number):
    """
    Build a formatted text block for injection into dispute letter prompts.
    This is the string that gets passed to GPT alongside the inaccuracy data.
    """
    sections = []

    # --- FCRA Citation ---
    if fcra_citation:
        sections.append(
            f"**Applicable FCRA Provision:**\n"
            f"{fcra_citation['section']} — {fcra_citation['title']}\n"
            f"\"{fcra_citation['text']}\""
        )

    # --- CFPB Data ---
    if cfpb and cfpb.get('total', 0) > 0:
        cfpb_text = (
            f"**CFPB Complaint Data:**\n"
            f"The CFPB Consumer Complaint Database contains {cfpb['total']:,} complaints "
            f"against {cfpb.get('company', 'this company')}."
        )

        if cfpb.get('matching_issue_count', 0) > 0:
            cfpb_text += (
                f" Of the {cfpb['analyzed']} most recent complaints analyzed, "
                f"{cfpb['matching_issue_count']} involve the same type of reporting issue "
                f"present in this dispute."
            )

        if cfpb.get('win_rate', 0) > 0:
            cfpb_text += (
                f" {cfpb['win_rate']}% of analyzed complaints resulted in relief "
                f"for the consumer."
            )

        # Top issues
        if cfpb.get('top_issues'):
            top = cfpb['top_issues'][:3]
            issue_list = ', '.join(f'"{iss}" ({cnt})' for iss, cnt in top)
            cfpb_text += f"\nMost common complaint types: {issue_list}."

        sections.append(cfpb_text)

    # --- Case Law ---
    if case_law:
        case_texts = []

        # Landmark cases first
        for lm in (case_law.get('landmark_cases') or [])[:2]:
            case_texts.append(
                f"- *{lm['case_name']}*, {lm['citation']}: "
                f"{lm['holding']}"
            )

        # Search results (recent cases)
        for sc in (case_law.get('search_cases') or [])[:3]:
            name = sc.get('case_name_short') or sc.get('case_name', '')
            citation = sc.get('citation', '')
            date = (sc.get('date_filed') or '')[:4]
            court = sc.get('court_citation_string', sc.get('court', ''))
            snippet = sc.get('snippet', '').replace('<mark>', '').replace('</mark>', '')

            if name and (citation or (court and date)):
                cite_str = citation or f"({court} {date})"
                case_texts.append(f"- *{name}*, {cite_str}")

        if case_texts:
            sections.append(
                "**Relevant Case Law:**\n" + '\n'.join(case_texts)
            )

    # --- Bureau Response Context (Round 2+) ---
    if bureau_response and round_number >= 2:
        sections.append(
            f"**Bureau/Creditor Response to Previous Dispute (Round {round_number - 1}):**\n"
            f"{bureau_response[:500]}"
        )

    # --- Inaccuracy Detail ---
    if inaccuracy_detail:
        sections.append(
            f"**Specific Reporting Error Detected:**\n{inaccuracy_detail}"
        )

    return '\n\n'.join(sections)


def research_for_prompt(account_name, account_number=None, inaccuracies=None,
                        bureau_response=None, round_number=1):
    """
    High-level convenience function for dispute letter generation.
    Takes parser output and returns prompt-ready legal context.

    Args:
        account_name: Creditor name from parser
        account_number: Account number (optional, for context)
        inaccuracies: List of inaccuracy dicts from parser (optional)
        bureau_response: Bureau's response text (for rounds 2+)
        round_number: Current dispute round

    Returns:
        str: Formatted legal context for prompt injection
    """
    if not account_name:
        return ''

    # Use the first inaccuracy type if available
    inaccuracy_type = None
    inaccuracy_detail = None
    if inaccuracies:
        # Map inaccuracy descriptions to types
        for inacc in inaccuracies:
            desc = inacc if isinstance(inacc, str) else inacc.get('description', '')
            inaccuracy_type = _classify_inaccuracy(desc)
            inaccuracy_detail = desc
            if inaccuracy_type:
                break

    # For round 2+, always research verification adequacy
    if round_number >= 2 and not inaccuracy_type:
        inaccuracy_type = 'verification_inadequate'

    package = research_dispute(
        company_name=account_name,
        inaccuracy_type=inaccuracy_type,
        inaccuracy_detail=inaccuracy_detail,
        bureau_response=bureau_response,
        round_number=round_number,
    )

    return package.get('prompt_context', '')


def _classify_inaccuracy(description):
    """
    Classify an inaccuracy description string into a known type.
    """
    desc_lower = description.lower()

    if 'status' in desc_lower and ('contradict' in desc_lower or 'history' in desc_lower):
        return 'status_contradicts_history'
    if 'account type' in desc_lower and ('mismatch' in desc_lower or 'collection' in desc_lower or 'open account' in desc_lower):
        return 'account_type_mismatch'
    if 'original creditor' in desc_lower:
        return 'original_creditor_not_reflected'
    if 'closed' in desc_lower and 'balance' in desc_lower:
        return 'closed_with_balance'
    if 'charge' in desc_lower and 'off' in desc_lower and 'status' in desc_lower:
        return 'charge_off_not_in_status'
    if 'verif' in desc_lower or 'reinvestigat' in desc_lower or 'inadequate' in desc_lower:
        return 'verification_inadequate'

    return None
