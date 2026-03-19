"""
CourtListener Case Law Search — searches federal and state court opinions
for FCRA-related case law relevant to credit report disputes.

Requires a free CourtListener account and API token.
Set COURTLISTENER_API_TOKEN in your environment.
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

CL_API_BASE = "https://www.courtlistener.com/api/rest/v4"
CL_SEARCH_URL = f"{CL_API_BASE}/search/"

# FCRA statute sections mapped to search-friendly terms
FCRA_SEARCH_TERMS = {
    'status_contradicts_history': [
        'FCRA "inaccurate information"',
        '"1681s-2" "duty of furnisher" accuracy',
    ],
    'account_type_mismatch': [
        'FCRA "account type" misreport',
        '"1681e" "reasonable procedures" accuracy',
    ],
    'original_creditor_not_reflected': [
        'FCRA "debt buyer" "original creditor" reporting',
        '"1681s-2" furnisher accuracy "collection account"',
    ],
    'closed_with_balance': [
        'FCRA "closed account" balance reporting inaccurate',
        '"1681s-2" "paid" balance "reporting"',
    ],
    'charge_off_not_in_status': [
        'FCRA "charge off" status "inaccurate reporting"',
        '"1681s-2" charge-off status accuracy',
    ],
    'verification_inadequate': [
        'FCRA "reinvestigation" "reasonable investigation" "1681i"',
        '"verified" dispute "boilerplate" inadequate',
    ],
}

# Landmark FCRA cases to always include when relevant
LANDMARK_CASES = [
    {
        'case_name': 'Cushman v. Trans Union Corp.',
        'citation': '115 F.3d 220 (3d Cir. 1997)',
        'holding': 'CRA must conduct reasonable reinvestigation beyond merely parroting furnisher response.',
        'relevance': ['verification_inadequate', 'status_contradicts_history'],
    },
    {
        'case_name': 'Gorman v. Wolpoff & Abramson, LLP',
        'citation': '584 F.3d 1147 (9th Cir. 2009)',
        'holding': 'Debt collectors who report to CRAs are furnishers subject to FCRA accuracy duties.',
        'relevance': ['original_creditor_not_reflected', 'account_type_mismatch'],
    },
    {
        'case_name': 'Saunders v. Branch Banking and Trust Co.',
        'citation': '526 F.3d 142 (4th Cir. 2008)',
        'holding': 'Furnisher must conduct reasonable investigation upon receiving dispute notice.',
        'relevance': ['verification_inadequate', 'status_contradicts_history'],
    },
    {
        'case_name': 'Gillespie v. Equifax Information Services',
        'citation': '484 F.3d 938 (7th Cir. 2007)',
        'holding': 'CRA cannot rely solely on automated dispute system; must actually investigate.',
        'relevance': ['verification_inadequate'],
    },
    {
        'case_name': 'Johnson v. MBNA America Bank, NA',
        'citation': '357 F.3d 426 (4th Cir. 2004)',
        'holding': 'Furnisher duty to investigate triggered by notice of dispute from CRA.',
        'relevance': ['verification_inadequate', 'status_contradicts_history'],
    },
]


def _get_token():
    """Get CourtListener API token from environment."""
    return os.environ.get('COURTLISTENER_API_TOKEN', '')


def search_case_law(query, limit=10, court=None, date_after=None):
    """
    Search CourtListener for court opinions matching a query.

    Args:
        query: Search string (e.g., 'FCRA "Jefferson Capital" inaccurate')
        limit: Max results to return
        court: Court filter (e.g., 'scotus', 'ca3' for 3rd Circuit)
        date_after: Only cases after this date (YYYY-MM-DD)

    Returns:
        dict with 'total', 'cases', and optional 'error'
    """
    token = _get_token()
    if not token:
        logger.warning("COURTLISTENER_API_TOKEN not set — returning landmark cases only")
        return {
            'total': 0,
            'cases': [],
            'error': 'CourtListener API token not configured. Set COURTLISTENER_API_TOKEN.',
            'landmark_only': True,
        }

    params = {
        'q': query,
        'type': 'o',  # opinions
        'order_by': 'score desc',
        'highlight': 'on',
    }

    if court:
        params['court'] = court
    if date_after:
        params['filed_after'] = date_after

    headers = {'Authorization': f'Token {token}'}

    try:
        resp = requests.get(CL_SEARCH_URL, params=params, headers=headers, timeout=30)

        if resp.status_code == 401:
            return {'total': 0, 'cases': [], 'error': 'Invalid CourtListener API token'}
        if resp.status_code == 429:
            return {'total': 0, 'cases': [], 'error': 'CourtListener rate limit — try again later'}
        if resp.status_code != 200:
            logger.warning(f"CourtListener API returned {resp.status_code}")
            return {'total': 0, 'cases': [], 'error': f'API returned {resp.status_code}'}

        data = resp.json()
        total = data.get('count', 0)
        results = data.get('results', [])

        cases = []
        for result in results[:limit]:
            cases.append(_normalize_case(result))

        return {'total': total, 'cases': cases}

    except requests.Timeout:
        return {'total': 0, 'cases': [], 'error': 'CourtListener request timed out'}
    except Exception as e:
        logger.error(f"CourtListener search error: {e}")
        return {'total': 0, 'cases': [], 'error': str(e)}


def _normalize_case(result):
    """Map CourtListener search result to our standard format."""
    return {
        'case_name': result.get('caseName', ''),
        'case_name_short': result.get('caseNameShort', ''),
        'court': result.get('court', ''),
        'court_citation_string': result.get('court_citation_string', ''),
        'date_filed': result.get('dateFiled', ''),
        'citation': _extract_citation(result),
        'snippet': result.get('snippet', ''),
        'cluster_id': result.get('cluster_id', ''),
        'docket_id': result.get('docket_id', ''),
        'url': f"https://www.courtlistener.com/opinion/{result.get('cluster_id', '')}/",
        'status': result.get('status', ''),
        'judge': result.get('judge', ''),
    }


def _extract_citation(result):
    """Extract the best available citation string."""
    citation = result.get('citation', [])
    if isinstance(citation, list) and citation:
        return citation[0]
    if isinstance(citation, str):
        return citation
    # Fallback: construct from court + date
    court = result.get('court_citation_string', result.get('court', ''))
    date = (result.get('dateFiled') or '')[:4]
    name = result.get('caseNameShort', '')
    if court and date and name:
        return f"{name} ({court} {date})"
    return ''


def search_fcra_cases(company_name=None, inaccuracy_type=None, limit=5,
                      date_after='2015-01-01'):
    """
    Search for FCRA case law relevant to a specific dispute.

    Args:
        company_name: Creditor/collector name (optional)
        inaccuracy_type: Type from _detect_inaccuracies() (optional)
        limit: Max results
        date_after: Only cases after this date

    Returns:
        dict with 'total', 'cases', 'landmark_cases', and optional 'error'
    """
    # Build search queries based on inaccuracy type
    queries = []

    # Company-specific FCRA search
    if company_name:
        queries.append(f'FCRA "{company_name}"')

    # Inaccuracy-type-specific searches
    if inaccuracy_type and inaccuracy_type in FCRA_SEARCH_TERMS:
        queries.extend(FCRA_SEARCH_TERMS[inaccuracy_type])
    else:
        # Generic FCRA accuracy search
        queries.append('FCRA "inaccurate" "credit report" "furnisher"')

    # Run searches and deduplicate
    all_cases = []
    seen_ids = set()
    total = 0

    for query in queries:
        result = search_case_law(query, limit=limit, date_after=date_after)

        if result.get('error') and result.get('landmark_only'):
            # No API token — return landmarks only
            return _landmark_only_result(inaccuracy_type)

        if result.get('error'):
            continue

        total = max(total, result.get('total', 0))
        for case in result.get('cases', []):
            cid = case.get('cluster_id')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_cases.append(case)

    # Sort by date (newest first)
    all_cases.sort(key=lambda c: c.get('date_filed', ''), reverse=True)

    # Get relevant landmark cases
    landmarks = _get_relevant_landmarks(inaccuracy_type)

    return {
        'total': total,
        'cases': all_cases[:limit],
        'landmark_cases': landmarks,
    }


def _landmark_only_result(inaccuracy_type):
    """Return landmark cases when API isn't available."""
    landmarks = _get_relevant_landmarks(inaccuracy_type)
    return {
        'total': len(landmarks),
        'cases': [],
        'landmark_cases': landmarks,
        'error': 'CourtListener API token not configured — showing landmark cases only',
    }


def _get_relevant_landmarks(inaccuracy_type):
    """Filter landmark cases relevant to the inaccuracy type."""
    if not inaccuracy_type:
        return LANDMARK_CASES

    return [
        case for case in LANDMARK_CASES
        if inaccuracy_type in case.get('relevance', [])
    ]


def get_opinion_text(cluster_id):
    """
    Fetch the full text of a court opinion by cluster ID.
    Useful for extracting specific holdings to cite in dispute letters.
    """
    token = _get_token()
    if not token:
        return {'error': 'CourtListener API token not configured'}

    url = f"{CL_API_BASE}/clusters/{cluster_id}/"
    headers = {'Authorization': f'Token {token}'}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return {'error': f'API returned {resp.status_code}'}

        cluster = resp.json()

        # Get the first sub_opinion (usually the lead opinion)
        sub_opinions = cluster.get('sub_opinions', [])
        if not sub_opinions:
            return {
                'case_name': cluster.get('case_name', ''),
                'text': '',
                'error': 'No opinion text available',
            }

        # Fetch the opinion text
        opinion_url = sub_opinions[0] if isinstance(sub_opinions[0], str) else sub_opinions[0].get('resource_uri', '')
        if opinion_url:
            op_resp = requests.get(
                opinion_url if opinion_url.startswith('http') else f"https://www.courtlistener.com{opinion_url}",
                headers=headers, timeout=30
            )
            if op_resp.status_code == 200:
                op_data = op_resp.json()
                # Prefer html_with_citations, fall back to plain_text
                text = (op_data.get('html_with_citations') or
                        op_data.get('plain_text') or
                        op_data.get('html') or '')
                return {
                    'case_name': cluster.get('case_name', ''),
                    'date_filed': cluster.get('date_filed', ''),
                    'text': text,
                }

        return {'case_name': cluster.get('case_name', ''), 'text': '', 'error': 'Could not fetch opinion text'}

    except Exception as e:
        logger.error(f"CourtListener opinion fetch error: {e}")
        return {'error': str(e)}
