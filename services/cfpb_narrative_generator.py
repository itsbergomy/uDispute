"""
CFPB AI Narrative Generator — creates personalized CFPB complaint narratives
using parsed inaccuracies, dispute history, CFPB complaint data, and legal research.

Replaces the static/generic narratives in the CFPB 7-Day Wizard with
case-specific, legally-grounded complaint text that CFPB takes seriously.
"""

import os
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    return _client


CFPB_NARRATIVE_SYSTEM_PROMPT = (
    "You are a consumer rights expert writing CFPB complaint narratives. "
    "Generate exactly 3 complaint narratives for a consumer filing on consumerfinance.gov/complaint.\n\n"
    "RULES:\n"
    "1. Each narrative must be 150-300 words — concise but specific.\n"
    "2. Use ACTUAL account details, inaccuracy descriptions, and dates provided.\n"
    "3. Cite specific FCRA sections (15 U.S.C. 1681i, 1681e(b), 1681s-2).\n"
    "4. Reference CFPB complaint volume if provided ('X other consumers reported similar issues').\n"
    "5. If prior dispute history exists, reference dates and outcomes.\n"
    "6. NEVER fabricate documents, dates, or facts not provided in the context.\n"
    "7. Tone: firm, factual, professional. Not threatening.\n"
    "8. Each narrative should take a different angle.\n\n"
    "OUTPUT FORMAT — return exactly 3 sections separated by '---':\n"
    "NARRATIVE 1: Investigation Failure\n"
    "[text]\n"
    "---\n"
    "NARRATIVE 2: Pattern of Violations\n"
    "[text]\n"
    "---\n"
    "NARRATIVE 3: Statutory Damages & Demand\n"
    "[text]"
)


def generate_cfpb_narratives(
    account_name,
    account_number,
    bureau=None,
    inaccuracies=None,
    dispute_history=None,
    cfpb_data=None,
    status=None,
):
    """
    Generate 3 personalized CFPB complaint narratives.

    Args:
        account_name: Creditor/account name
        account_number: Account number (may be masked)
        bureau: Which bureau (Experian, TransUnion, Equifax) or None for all
        inaccuracies: List of inaccuracy description strings from parser
        dispute_history: List of dicts with prior dispute info (dates, outcomes)
        cfpb_data: Dict with CFPB complaint stats (total_complaints, common_issues, etc.)
        status: Account status string

    Returns:
        List of 3 dicts: [{'title': str, 'body': str}, ...]
        Falls back to static narratives on any error.
    """
    try:
        # Build the context block
        context_parts = []

        context_parts.append(f"ACCOUNT: {account_name}")
        context_parts.append(f"ACCOUNT NUMBER: {account_number or 'Unknown'}")
        if status:
            context_parts.append(f"ACCOUNT STATUS: {status}")
        if bureau:
            context_parts.append(f"BUREAU: {bureau}")

        # Inaccuracies from parser
        if inaccuracies:
            context_parts.append("\nDETECTED INACCURACIES:")
            for i, inac in enumerate(inaccuracies[:8], 1):
                context_parts.append(f"  {i}. {inac}")
        else:
            context_parts.append("\nNo specific inaccuracies detected by parser — use general FCRA violation language.")

        # CFPB complaint data
        if cfpb_data and cfpb_data.get('total', 0) > 0:
            context_parts.append(f"\nCFPB COMPLAINT DATA FOR THIS COMPANY:")
            context_parts.append(f"  Total complaints: {cfpb_data['total']}")
            if cfpb_data.get('complaints'):
                issues = set()
                for c in cfpb_data['complaints'][:10]:
                    if c.get('issue'):
                        issues.add(c['issue'])
                if issues:
                    context_parts.append(f"  Common issues: {', '.join(list(issues)[:5])}")

        # Dispute history
        if dispute_history:
            context_parts.append("\nPRIOR DISPUTE HISTORY:")
            for dh in dispute_history[:5]:
                line = f"  - {dh.get('date', 'Unknown date')}: {dh.get('template', 'Dispute letter')}"
                if dh.get('outcome'):
                    line += f" → Outcome: {dh['outcome']}"
                if dh.get('bureau'):
                    line += f" ({dh['bureau']})"
                context_parts.append(line)
        else:
            context_parts.append("\nNo prior dispute history — this is the first complaint.")

        context = "\n".join(context_parts)

        client = _get_client()
        response = client.chat.completions.create(
            model="o3-mini",
            messages=[
                {"role": "developer", "content": CFPB_NARRATIVE_SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
        )

        raw = response.choices[0].message.content.strip()

        # Parse the 3 narratives
        narratives = _parse_narratives(raw, account_name, account_number)
        if len(narratives) == 3:
            return narratives

        logger.warning(f"CFPB narrative parse returned {len(narratives)} narratives, expected 3. Using fallback.")
        return _static_fallback(account_name, account_number)

    except Exception as e:
        logger.error(f"CFPB narrative generation failed: {e}")
        return _static_fallback(account_name, account_number)


def _parse_narratives(raw_text, account_name, account_number):
    """Parse the 3 narratives from the AI response, separated by '---'."""
    sections = [s.strip() for s in raw_text.split('---') if s.strip()]

    narratives = []
    default_titles = ['Investigation Failure', 'Pattern of Violations', 'Statutory Damages & Demand']

    for i, section in enumerate(sections[:3]):
        lines = section.strip().split('\n', 1)
        title = default_titles[i] if i < len(default_titles) else f'Narrative {i+1}'
        body = section

        # Try to extract title from first line if it matches pattern
        if lines[0].upper().startswith('NARRATIVE'):
            # "NARRATIVE 1: Investigation Failure" — title is after the number colon
            import re
            match = re.match(r'NARRATIVE\s*\d+\s*:\s*(.*)', lines[0], re.IGNORECASE)
            if match:
                title = match.group(1).strip()
            body = lines[1].strip() if len(lines) > 1 else ''
        elif ':' in lines[0] and len(lines[0]) < 80:
            title = lines[0].rstrip(':').strip()
            body = lines[1].strip() if len(lines) > 1 else ''

        if body:
            narratives.append({'title': title, 'body': body})

    return narratives


def _static_fallback(account_name, account_number):
    """Return the original static narratives as fallback."""
    acct_num = account_number or '[Account Number]'
    return [
        {
            'title': 'Validation Violation',
            'body': (
                f"This company, {account_name} (Account #{acct_num}), is violating my rights. "
                "They have not provided validation information under 12 CFR 1006.34(b)(5) "
                "yet they have placed a collection on my consumer report recently."
            ),
        },
        {
            'title': 'Deceptive Practices',
            'body': (
                f"This agency, {account_name} (Account #{acct_num}), is violating my consumer "
                "rights by using false, misleading, misrepresentation, and deceptive means."
            ),
        },
        {
            'title': 'Demand / Closing Statement',
            'body': (
                f"I have made previous attempts to fix these issues directly with {account_name} "
                f"(Account #{acct_num}) and they are violating my rights. I'm entitled to "
                "$1,000 for every violation listed. They either pay me or delete these "
                "accounts ASAP."
            ),
        },
    ]
