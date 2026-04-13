"""
Dispute letter generation service.
Extracted from dispute_ui.py — handles prompt packs and GPT letter generation.
"""

import os
import asyncio
import tempfile
from openai import OpenAI, AsyncOpenAI
from dotenv import load_dotenv
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.utils import ImageReader
from PyPDF2 import PdfMerger
from PIL import Image

load_dotenv()

openai_client = OpenAI()
async_openai_client = AsyncOpenAI()

# ─── FCRA Inaccuracy Mapping ───

FCRA_INACCURACY_MAP = {
    "status_contradicts_history": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Duty of Furnishers — Accuracy",
        "explanation": (
            "Under {section}, furnishers of information are prohibited from reporting "
            "information to consumer reporting agencies if they know or have reasonable cause "
            "to believe the information is inaccurate. The account status reported to the bureau "
            "directly contradicts the payment history data on the same report."
        ),
    },
    "account_type_mismatch": {
        "section": "15 U.S.C. § 1681e(b)",
        "title": "CRA Accuracy Procedures",
        "explanation": (
            "Under {section}, consumer reporting agencies must follow reasonable procedures "
            "to assure maximum possible accuracy of consumer information. This account is "
            "classified with an incorrect account type that misrepresents the nature of the debt."
        ),
    },
    "original_creditor_not_reflected": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Duty of Furnishers — Accuracy",
        "explanation": (
            "Under {section}, the presence of an original creditor indicates this debt was "
            "sold or transferred to a collection entity, yet the account type does not reflect "
            "this — constituting inaccurate reporting of the account's nature."
        ),
    },
    "closed_account_with_balance": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Duty of Furnishers — Accuracy",
        "explanation": (
            "Under {section}, a closed account that has been paid should report a $0 balance. "
            "Reporting a balance on a closed/paid account is inaccurate and misleading to "
            "potential creditors reviewing this consumer's file."
        ),
    },
    "chargeoff_not_in_status": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Duty of Furnishers — Accuracy",
        "explanation": (
            "Under {section}, the payment history grid shows charge-off entries but the "
            "account status text does not reflect this adverse information. This internal "
            "inconsistency within the same report constitutes inaccurate reporting."
        ),
    },
    "balance_exceeds_limit": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
        "title": "Duty of Furnishers — Incomplete Reporting",
        "explanation": (
            "Under {section}, furnishers must not report information they know to be "
            "incomplete. The reported balance exceeds the original credit limit, suggesting "
            "unauthorized fees or interest were added after the account became delinquent "
            "without proper disclosure."
        ),
    },
    "double_reporting": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
        "title": "Duty of Furnishers — Incomplete/Duplicative Reporting",
        "explanation": (
            "Under {section}, the same debt appears to be reported by both the original "
            "creditor and a collection agency. This duplicative reporting inflates the "
            "consumer's apparent delinquent debt and is both inaccurate and misleading."
        ),
    },
    "missing_due_date": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
        "title": "Duty of Furnishers — Incomplete Reporting",
        "explanation": (
            "Under {section}, furnishers must not report information they know to be "
            "incomplete. The account is missing a due date, which is a required data "
            "field for accurate credit reporting. Without a due date, consumers and "
            "potential creditors cannot properly evaluate the account's status."
        ),
    },
    "missing_payment_amount": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
        "title": "Duty of Furnishers — Incomplete Reporting",
        "explanation": (
            "Under {section}, furnishers must not report information they know to be "
            "incomplete. The account is missing the scheduled monthly payment amount, "
            "which is essential for accurate debt-to-income calculations and creditworthiness "
            "assessment. This omission renders the reporting incomplete and potentially misleading."
        ),
    },
    "unverified_late_payments": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Duty of Furnishers — Accuracy of Late Payment Reporting",
        "explanation": (
            "Under {section}, furnishers must not report information they know or have "
            "reasonable cause to believe is inaccurate. Each late payment entry must be "
            "independently verifiable by the original creditor — including the exact date "
            "the payment was due, the date it was received, and the specific delinquency "
            "bucket (30/60/90/120 days). The furnisher must produce documentation proving "
            "each reported delinquency. A blanket e-OSCAR Response Code 01 ('verified') "
            "without field-level verification does not constitute a reasonable investigation "
            "as established in CFPB v. Experian (2025)."
        ),
    },
    "unvalidated_collection": {
        "section": "15 U.S.C. § 1692g",
        "title": "Debt Validation — Fair Debt Collection Practices Act",
        "explanation": (
            "Under {section} (FDCPA) and 15 U.S.C. § 1681s-2(b) (FCRA), a collection "
            "account requires debt validation. The collector must produce: (1) the amount "
            "of the debt and how it was calculated, (2) the name of the original creditor, "
            "(3) the date of first delinquency, (4) the signed credit application or "
            "agreement, and (5) a complete payment ledger. If the collector cannot produce "
            "this documentation within 30 days, the account must be deleted per "
            "15 U.S.C. § 1681i(a)(5)(A). Collection accounts are particularly vulnerable "
            "to DNR (Did Not Respond) deletions in the e-OSCAR system because collectors "
            "frequently lack original documentation from the creditor."
        ),
    },
    "unverified_chargeoff": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Duty of Furnishers — Charge-Off Verification",
        "explanation": (
            "Under {section}, a charged-off account requires verification of: (1) the "
            "exact charge-off date, (2) the charge-off balance vs. the original debt amount, "
            "(3) whether post-charge-off interest or fees were added (which may violate state "
            "usury laws), and (4) the date of first delinquency (which determines the 7-year "
            "reporting window). Furnishers are prohibited from "
            "resetting the date of first delinquency after a dispute. If the reported charge-off "
            "data cannot be independently verified with original creditor records, the account "
            "must be deleted."
        ),
    },
    # ── Cross-Bureau Discrepancy Categories ──
    "cross_bureau_balance_mismatch": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Cross-Bureau Inconsistency — Balance Discrepancy",
        "explanation": (
            "Under {section}, furnishers are prohibited from reporting information they know "
            "or have reasonable cause to believe is inaccurate. This account reports different "
            "balance amounts across credit bureaus. At minimum, one bureau is furnishing an "
            "inaccurate balance. The furnisher has a duty to report identical, accurate balance "
            "information to all bureaus simultaneously. This cross-bureau inconsistency "
            "constitutes prima facie evidence of inaccurate reporting."
        ),
    },
    "cross_bureau_status_conflict": {
        "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
        "title": "Cross-Bureau Inconsistency — Status Conflict",
        "explanation": (
            "Under {section}, this account reports contradictory statuses across credit bureaus. "
            "An account cannot simultaneously be in different states of delinquency at different "
            "bureaus — this demonstrates that at least one CRA is reporting inaccurate status "
            "information. The furnisher must reconcile and correct the status across all bureaus."
        ),
    },
    "cross_bureau_missing_account": {
        "section": "15 U.S.C. § 1681i(a)",
        "title": "Cross-Bureau Inconsistency — Selective Reporting",
        "explanation": (
            "Under {section}, this account appears on one bureau but not others. Selective "
            "reporting — where a negative tradeline is furnished to some bureaus but not all — "
            "raises questions about the account's verifiability. If the furnisher cannot produce "
            "documentation that this account belongs to the consumer across all bureaus where "
            "it is reported, it must be deleted from the reporting bureau."
        ),
    },
    "cross_bureau_date_mismatch": {
        "section": "15 U.S.C. § 1681e(b)",
        "title": "Cross-Bureau Inconsistency — Date Discrepancy",
        "explanation": (
            "Under {section}, consumer reporting agencies must follow reasonable procedures to "
            "assure maximum possible accuracy. This account reports different date-opened values "
            "across bureaus, which affects credit age calculations and the 7-year reporting window. "
            "The furnisher must correct the date to match the actual origination records."
        ),
    },
}


def classify_inaccuracy(inaccuracy_text):
    """Classify an inaccuracy string into an FCRA category."""
    text = inaccuracy_text.lower()
    # Cross-bureau findings (prefixed with [CROSS-BUREAU])
    if '[cross-bureau]' in text:
        if 'balance' in text:
            return "cross_bureau_balance_mismatch"
        if 'status' in text:
            return "cross_bureau_status_conflict"
        if 'not reported' in text or 'does not appear' in text or 'selective' in text:
            return "cross_bureau_missing_account"
        if 'date' in text:
            return "cross_bureau_date_mismatch"
        return "cross_bureau_balance_mismatch"  # default cross-bureau
    # Standard intra-report inaccuracies
    if 'status' in text and ('contradict' in text or 'paying as agreed' in text or 'current' in text):
        return "status_contradicts_history"
    if 'account type' in text and ('mismatch' in text or 'open account' in text or 'collection' in text):
        return "account_type_mismatch"
    if 'original creditor' in text:
        return "original_creditor_not_reflected"
    if 'closed' in text and 'balance' in text:
        return "closed_account_with_balance"
    if 'charge-off' in text and 'status' in text and ('not reflect' in text or 'inconsistent' in text):
        return "chargeoff_not_in_status"
    if 'exceeds' in text and ('limit' in text or 'credit limit' in text):
        return "balance_exceeds_limit"
    if 'double' in text or 'duplicat' in text:
        return "double_reporting"
    if 'missing' in text and 'due date' in text:
        return "missing_due_date"
    if 'missing' in text and ('payment amount' in text or 'monthly payment' in text):
        return "missing_payment_amount"
    if 'late payment entries' in text and 'demand verification' in text:
        return "unverified_late_payments"
    if 'collection' in text and 'validate' in text:
        return "unvalidated_collection"
    if 'charge-off status' in text and 'demand verification' in text:
        return "unverified_chargeoff"
    return None


def build_inaccuracy_context(account):
    """
    Build structured dispute context from a parsed account's inaccuracies.

    Takes an account dict (from the parser, with 'inaccuracies' list) and returns
    a formatted string with FCRA citations and specific dispute language that can
    be injected into prompt templates.

    Args:
        account: Dict with keys: account_name, account_number, status, issue,
                 inaccuracies (list of strings), balance, account_type, etc.

    Returns:
        A formatted string ready for injection into dispute letter prompts.
        Returns empty string if no inaccuracies found.
    """
    inaccuracies = account.get('inaccuracies', [])
    if not inaccuracies:
        return ""

    acct_name = account.get('account_name', 'Unknown')
    acct_number = account.get('account_number', 'Unknown')

    sections = []
    sections.append(
        f"PARSER-DETECTED REPORTING INACCURACIES FOR {acct_name} (#{acct_number}):\n"
        f"The following specific inaccuracies were identified by automated analysis "
        f"of the credit report data. Each represents a potential FCRA violation that "
        f"must be investigated and corrected.\n"
    )

    for i, inac_text in enumerate(inaccuracies, 1):
        category = classify_inaccuracy(inac_text)
        fcra = FCRA_INACCURACY_MAP.get(category)

        if fcra:
            section_ref = fcra['section']
            explanation = fcra['explanation'].format(section=section_ref)
            sections.append(
                f"INACCURACY #{i}: {inac_text}\n"
                f"FCRA VIOLATION: {fcra['title']} — {section_ref}\n"
                f"LEGAL BASIS: {explanation}\n"
            )
        else:
            sections.append(
                f"INACCURACY #{i}: {inac_text}\n"
                f"FCRA VIOLATION: 15 U.S.C. § 1681s-2(a)(1)(A) — Duty to Report Accurate Information\n"
                f"LEGAL BASIS: Under 15 U.S.C. § 1681s-2(a)(1)(A), furnishers are prohibited from "
                f"reporting information they know or have reasonable cause to believe is inaccurate.\n"
            )

    sections.append(
        f"REQUESTED ACTION: Pursuant to 15 U.S.C. § 1681i, I demand that you conduct "
        f"a reasonable investigation of each inaccuracy identified above within 30 days. "
        f"As established in CFPB v. Experian (2025) and CFPB v. Equifax (2025), this "
        f"investigation must go beyond forwarding a dispute code through the e-OSCAR system — "
        f"you must forward all consumer-provided documentation to the data furnisher and "
        f"independently verify the disputed fields. "
        f"If the furnisher cannot verify the accuracy of each specific data point listed "
        f"above, the account must be deleted from my credit file per "
        f"15 U.S.C. § 1681i(a)(5)(A). A blanket e-OSCAR Response Code 01 ('verified as "
        f"accurate') without field-level verification will be treated as a failure to "
        f"conduct a reasonable investigation and may result in legal action under "
        f"15 U.S.C. § 1681i and § 1681n."
    )

    return "\n".join(sections)


def build_inaccuracy_context_multi(accounts):
    """
    Build dispute context for multiple accounts at once.

    Args:
        accounts: List of account dicts from the parser.

    Returns:
        Formatted string covering all accounts with inaccuracies.
    """
    parts = []
    for account in accounts:
        ctx = build_inaccuracy_context(account)
        if ctx:
            parts.append(ctx)

    if not parts:
        return ""

    header = (
        "═══ AUTOMATED CREDIT REPORT ANALYSIS ═══\n"
        "The following inaccuracies were detected through automated parsing of the "
        "consumer's credit report. Each inaccuracy is mapped to the specific FCRA "
        "provision it violates. These findings should be cited in the dispute letter.\n\n"
    )

    return header + "\n---\n\n".join(parts)


# ─── Prompt Packs ───

PACKS = {
    "default": [
        (
            "Write a formal dispute letter to {entity}.\n\n"
            "Account: {account_name} | #{account_number}\n"
            "Reported Status: {marks}\n"
            "Issues: {issue}\n"
            "Requested Action: {action}\n\n"
            "Follow all e-OSCAR RULES from the system prompt. Additionally:\n"
            "- Weave legal citations into dispute points (§ 1681i(a), § 1681s-2(b), "
            "§ 1681i(a)(5)(A), Cushman, Gorman, CFPB v. Experian/Equifax)\n"
            "- Direct furnisher document demands THROUGH the CRA, not at the CRA itself"
        ),
        (
            "Write a dispute letter to {entity} challenging account "
            "{account_name} (#{account_number}).\n\n"
            "Current Status: {marks}\n"
            "Dispute Issue: {issue}\n"
            "Demanded Resolution: {action}\n\n"
            "Follow all e-OSCAR RULES. Cite Cushman, Gorman, CFPB v. Experian (2025). "
            "Demand CRA direct furnisher to produce original agreement, payment ledger, "
            "chain of assignment — deletion mandatory if unproduced per § 1681i(a)(5)(A)."
        ),
    ],
    "arbitration": [
        (
            "Draft a formal arbitration demand letter to {entity} regarding account "
            "{account_name} (#{account_number}).\n\n"
            "Reported Status: {marks}\n"
            "Issue: {issue}\n"
            "Demanded Action: {action}\n\n"
            "DUAL-TRACK STRUCTURE:\n"
            "Track 1 — DISPUTE: Follow all e-OSCAR RULES (3+ dispute points, Metro 2 fields).\n"
            "Track 2 — ARBITRATION DEMAND: Demand binding arbitration grounded in the CRA's "
            "specific failure to investigate THIS dispute properly.\n\n"
            "ARBITRATION LAW: § 1681e(b) (reasonable procedures), § 1681n ($100-$1,000 + "
            "punitive per violation), § 1681o (actual damages + attorney fees). "
            "Cushman — punitive damages for knowing/reckless noncompliance. "
            "Direct furnisher document demands THROUGH the CRA."
        ),
        (
            "Compose a formal arbitration demand for {entity} — account "
            "{account_name} (#{account_number}).\n\n"
            "Current Status: {marks}\n"
            "Dispute: {issue}\n"
            "Demanded Resolution: {action}\n\n"
            "Follow all e-OSCAR RULES. Add arbitration demand section quantifying "
            "statutory damages ($100-$1,000 per Metro 2 field violation under § 1681n). "
            "Cite Cushman (punitive damages) and CFPB v. Equifax ($15M penalty). "
            "Direct furnisher document demands THROUGH the CRA."
        ),
    ],
    "consumer_law": [
        (
            "Write a multi-statute demand letter to {entity} regarding account "
            "{account_name} (#{account_number}).\n\n"
            "Reported Status: {marks}\n"
            "Issue: {issue}\n"
            "Demanded Action: {action}\n\n"
            "Follow all e-OSCAR RULES (3+ dispute points, Metro 2 fields, MOV demand). "
            "Additionally, layer MULTIPLE statutes — each dispute point should cite the "
            "most applicable statute:\n"
            "- FCRA § 1681i(a) / § 1681s-2(b) — accuracy + investigation duties\n"
            "- FDCPA § 1692g — debt validation (if collection account)\n"
            "- FDCPA § 1692e — false/misleading representation (if reported value is wrong)\n"
            "- FCBA § 1666 — billing error resolution (if credit card)\n"
            "- Cite Cushman, Gorman, CFPB v. Experian (2025), CFPB v. Equifax (2025)\n\n"
            "The multi-statute approach creates liability under MULTIPLE laws simultaneously, "
            "making it legally dangerous to rubber-stamp. Demand original documentation."
        ),
        (
            "Craft a multi-statute demand letter to {entity} for account {account_name} "
            "(#{account_number}).\n\n"
            "Status: {marks} | Dispute: {issue} | Resolution: {action}\n\n"
            "Follow all e-OSCAR RULES. Layer FCRA (§ 1681i, § 1681s-2), FDCPA (§ 1692g, "
            "§ 1692e), and FCBA (§ 1666) as applicable. Each dispute point targets a "
            "different Metro 2 field AND a different statute. Cite Cushman, Gorman, "
            "CFPB v. Experian (2025). Demand original documentation. Under 650 words."
        ),
    ],
    "ACDV_response": [
        (
            "Compose a formal ACDV enforcement demand to {entity} regarding account "
            "{account_name} (#{account_number}).\n\n"
            "Original Dispute Date: {dispute_date}\n"
            "Deadline: {days} business days\n\n"
            "This is a FOLLOW-UP letter — the consumer already disputed and the CRA "
            "responded with a verified result. Now demand the ACDV record to expose "
            "whether a real investigation occurred.\n\n"
            "DEMAND PRODUCTION OF:\n"
            "- The specific e-OSCAR Response Code received (01/02/07/13)\n"
            "- Method of Verification per § 1681i(a)(7)\n"
            "- Whether consumer documents were forwarded to the furnisher\n"
            "- Furnisher investigation file and contact logs\n\n"
            "Cite Cushman v. Trans Union, 115 F.3d 220 — CRA cannot parrot furnisher. "
            "Cite Gillespie v. Equifax, 484 F.3d 938 — CRA liable for unreasonable process. "
            "Cite CFPB v. Experian (2025) — 'sham investigations' via e-OSCAR. "
            "If the 'investigation' was just forwarding a code and accepting Code 01 back, "
            "that is not a reasonable investigation under § 1681i. Under 650 words."
        ),
        (
            "Write a formal ACDV records demand to {entity} for account "
            "{account_name} (#{account_number}). Dispute Date: {dispute_date}. "
            "Deadline: {days} business days.\n\n"
            "Follow-up to prior dispute. Demand: e-OSCAR Response Code, MOV, "
            "furnisher contact logs, whether documents were forwarded. "
            "Cite Cushman (cannot parrot), Gillespie v. Equifax, 484 F.3d 938 "
            "(unreasonable process), CFPB v. Experian (2025). Code-only verification "
            "violates § 1681i. Under 650 words."
        ),
    ],
    "furnisher_direct": [
        (
            "Write a direct dispute letter to the data furnisher {entity} regarding "
            "account {account_name} (#{account_number}).\n\n"
            "Reported Status: {marks}\n"
            "Issue: {issue}\n"
            "Demanded Action: {action}\n\n"
            "This goes DIRECTLY to the furnisher — bypasses e-OSCAR entirely.\n"
            "Use 3+ labeled DISPUTE POINTS with Metro 2 field references.\n\n"
            "CITE FURNISHER LAW (not CRA law):\n"
            "- 12 C.F.R. § 1022.43(a) — must investigate direct disputes\n"
            "- § 1022.43(e) — must report results to ALL CRAs\n"
            "- § 1681s-2(a)(8)(E) — must modify/delete/block if inaccurate\n"
            "- § 1681s-2(a)(1)(A) — duty not to report known inaccuracies\n"
            "- Gorman v. Wolpoff — 'fairly searching inquiry'\n"
            "- Boggio v. USAA, 696 F.3d 611 (6th Cir. 2012) — must consider consumer evidence\n\n"
            "Demand original agreement, payment ledger, chain of assignment. "
            "30-day deadline, § 1681n consequences. No frivolous exception for direct disputes. "
            "DO NOT reference e-OSCAR/ACDV or § 1681i. Keep under 650 words."
        ),
        (
            "Compose a direct dispute letter to furnisher {entity} for account "
            "{account_name} (#{account_number}).\n\n"
            "Status: {marks} | Dispute: {issue} | Resolution: {action}\n\n"
            "Bypasses e-OSCAR. 3+ DISPUTE POINTS with Metro 2 fields. "
            "Cite § 1022.43 (Regulation V), § 1681s-2(a)(8)(E), Gorman v. Wolpoff. "
            "Demand documentation. 30-day deadline, § 1681n consequences. "
            "No frivolous exception. DO NOT cite § 1681i or e-OSCAR. Under 650 words."
        ),
    ],
}

# Preamble injected before every prompt so GPT uses real client data
CLIENT_CONTEXT_PREAMBLE = (
    "Write this dispute letter on behalf of the client below. "
    "If client name/address are provided, use them. If any field is blank, "
    "use a placeholder like [YOUR NAME], [YOUR ADDRESS], [CITY, STATE ZIP] "
    "so the consumer can fill it in before mailing. Always write the full "
    "letter regardless — never refuse or ask for more information.\n\n"
    "Client: {client_full_name}\n"
    "Address: {client_address}\n"
    "{client_address_line2_section}"
    "City/State/ZIP: {client_city_state_zip}\n"
    "Date: {today_date}\n\n"
    "Recipient: {entity}\n"
    "{recipient_address_section}"
    "---\n\n"
)

PACK_INFO = [
    {"key": "default", "name": "Default Pack", "description": "Your go-to dispute templates — clean, direct, and effective for first-round disputes."},
    {"key": "arbitration", "name": "Arbitration Pack", "description": "Heavy hitters. Arbitration demands under 15 U.S.C. §1681e(b) — for when bureaus won't budge."},
    {"key": "consumer_law", "name": "Consumer Law Pack", "description": "Cite the FCBA, FDCPA, and more — full statutory firepower for stubborn creditors."},
    {"key": "ACDV_response", "name": "ACDV Enforcement Pack", "description": "Demand the full ACDV record — method of verification, submission procedure, and FCRA compliance docs. Make them prove it."},
]


# ─── e-OSCAR Intelligence Block (injected into all system prompts) ───
# This block ensures GPT understands how the dispute system actually works
# and writes letters designed to survive the e-OSCAR automation pipeline.

_E_OSCAR_INTELLIGENCE = (
    "e-OSCAR CONTEXT: CRA employees have ~4 min to reduce disputes to a 2-3 digit "
    "ACDV code. The furnisher sees ONLY that code, not the letter. Response codes: "
    "01=verified, 02=modify, 07=DELETE (target), 13=deleted, DNR=auto-delete 30 days.\n\n"
    "KEY PRECEDENT: CFPB v. Experian (1:25-cv-00001, C.D. Cal. 2025) — 'sham investigations' "
    "via e-OSCAR code distortion. CFPB v. Equifax (2025-CFPB-0002, $15M) — ignored consumer "
    "docs, deferred to furnishers. Cushman v. Trans Union, 115 F.3d 220 (3d Cir. 1997) — "
    "cannot parrot furnisher. Gorman v. Wolpoff, 584 F.3d 1147 (9th Cir. 2009) — 'fairly "
    "searching inquiry' required.\n\n"
    "METRO 2 FIELDS: Account Status (Fld 17), Payment Rating (Fld 18), Current Balance "
    "(Fld 21), Credit Limit (Fld 22), Payment History Profile (Fld 25), DOFD (Fld 26).\n\n"
    "RULES:\n"
    "1. Use Metro 2 field names + numbers in every dispute point — forces escalation.\n"
    "2. Structure as 3+ labeled DISPUTE POINTS targeting different field categories "
    "(status/balance/payment history/dates) so no single code covers the dispute.\n"
    "3. State REPORTED value and WHY it is factually wrong — never generic.\n"
    "4. Include MOV request: 'Pursuant to § 1681i(a)(7), provide investigation procedure, "
    "furnisher name/address/phone, within 15 days.'\n"
    "5. DNR trap: demand CRA direct furnisher to produce original agreement, payment "
    "ledger, chain of assignment. Non-response = delete per § 1681i(a)(5)(A).\n"
    "6. Anti-compression warning: single-code reduction = willful noncompliance § 1681n.\n"
    "7. Instruct consumer to enclose ID + utility bill as 'Enclosures.'\n"
    "8. Keep under 650 words. No generic language ('please investigate').\n"
    "9. STATUTE TARGETING — Only cite FDCPA (15 U.S.C. § 1692) when the account is a "
    "collection agency or debt buyer (LVNV, Midland, Portfolio Recovery, etc.). "
    "NEVER cite FDCPA against original creditors (Capital One, Bank of America, Chase, "
    "Wells Fargo, etc.) — they are NOT 'debt collectors' under the FDCPA. For original "
    "creditors, cite FCRA § 1681s-2 (furnisher duties) instead.\n"
    "10. EVIDENCE INTEGRITY — NEVER reference documents the consumer has not provided "
    "(no 1099-C, chat logs, receipts, or statements unless explicitly listed in the "
    "prompt data). The 'WHY it is wrong' must come ONLY from: (a) contradictions "
    "WITHIN the credit report itself (e.g., Closed status but non-zero balance), "
    "(b) Metro 2 compliance violations (e.g., DOFD older than 7 years), "
    "(c) cross-bureau discrepancies if provided, or (d) parser-detected inaccuracies. "
    "If no specific reason is available, frame as: 'I dispute the accuracy of this "
    "value and demand field-level verification with documentary proof from the "
    "furnisher.' NEVER fabricate evidence or claim enclosures that do not exist.\n\n"
)

_EDUCATIONAL_NOTE = (
    "EDUCATIONAL NOTE: Write the letter in a way that helps the consumer understand "
    "WHY each inaccuracy is a violation and WHAT their rights are. This is not just "
    "a legal document — it's a learning tool that empowers the consumer to understand "
    "the credit reporting system and the e-OSCAR process that works against them."
)


SYSTEM_PROMPT_BASE = (
    "You are uDispute, a professional credit dispute letter generator. "
    "You write precise, legally grounded dispute letters that address inaccuracies "
    "and potential FCRA violations by creditors and credit reporting agencies.\n\n"
    + _E_OSCAR_INTELLIGENCE
    + _EDUCATIONAL_NOTE
)

SYSTEM_PROMPT_WITH_INACCURACIES = (
    "You are uDispute, a professional credit dispute letter generator backed by "
    "automated credit report analysis.\n\n"
    + _E_OSCAR_INTELLIGENCE
    + "PARSER-DETECTED INACCURACIES: Specific reporting errors have been auto-detected "
    "with FCRA violations. Incorporate each finding as a labeled DISPUTE POINT — cite "
    "the Metro 2 field, the REPORTED value, and WHY it is factually wrong. These are "
    "demonstrable errors (not 'unverified'), so a Code 01 response would itself violate "
    "the FCRA.\n\n"
    + _EDUCATIONAL_NOTE
)

SYSTEM_PROMPT_WITH_LEGAL_RESEARCH = (
    "You are uDispute, a professional credit dispute letter generator backed by "
    "CFPB complaint data and federal case law.\n\n"
    + _E_OSCAR_INTELLIGENCE
    + "THREE-LAYER EVIDENCE: (1) Parser-detected inaccuracies — cite Metro 2 field + "
    "FCRA section. (2) CFPB complaint data — shows systemic pattern against this creditor. "
    "(3) Federal case law — cite provided case names/holdings naturally. Do NOT fabricate "
    "cases. Only cite what is provided in the prompt.\n\n"
    + _EDUCATIONAL_NOTE
)


SYSTEM_PROMPT_NOTICE_OF_DISPUTE = (
    "You are uDispute, a professional credit dispute letter generator. "
    "You are generating a NOTICE OF DISPUTE — a formal notification letter "
    "sent to a credit bureau informing them that the consumer is disputing specific "
    "accounts on their credit report.\n\n"
    "STRATEGIC PURPOSE: This letter starts the 30-day investigation clock under "
    "15 U.S.C. § 1681i WITHOUT revealing the consumer's specific legal arguments. "
    "This is how consumer attorneys operate — put the bureau on notice first, then "
    "follow up with the detailed Bureau Assault letter 15-30 days later.\n\n"
    "IMPORTANT RULES FOR THIS LETTER TYPE:\n"
    "1. List every disputed account by name, account number, and account type in a "
    "clear, numbered format\n"
    "2. State that the consumer formally disputes the accuracy of these accounts "
    "under 15 U.S.C. § 1681i\n"
    "3. Demand investigation within 30 days per FCRA requirements\n"
    "4. Reference CFPB v. Experian (2025) — the CFPB found CRAs conducting 'sham "
    "investigations' via e-OSCAR, establishing that code-only processing is unlawful\n"
    "5. Do NOT include specific inaccuracies, detailed FCRA citations, or legal "
    "arguments — those come in the follow-up Bureau Assault letter\n"
    "6. Keep the tone professional, direct, and confident\n"
    "7. Include a statement that failure to investigate within 30 days constitutes a "
    "violation of 15 U.S.C. § 1681i(a)(1)\n"
    "8. Request written confirmation of receipt and investigation results\n"
    "9. State that the consumer is aware their dispute will be processed through the "
    "e-OSCAR/ACDV system and that a code-only verification does not constitute a "
    "'reasonable investigation' under the FCRA\n\n"
    "This letter puts the bureau on notice AND signals that the consumer understands "
    "the system. The detailed Bureau Assault follows 15-30 days later."
)


# ─── Dual-Letter Strategy (CRA + Furnisher) ───

SYSTEM_PROMPT_FURNISHER_DIRECT = (
    "You are uDispute, a professional credit dispute letter generator. "
    "You are generating a DIRECT FURNISHER DISPUTE LETTER — sent directly to the "
    "data furnisher (creditor or debt collector), completely bypassing the CRA and "
    "the e-OSCAR system.\n\n"
    "LEGAL FRAMEWORK FOR DIRECT DISPUTES:\n"
    "Under 12 C.F.R. § 1022.43 (Regulation V), consumers have the right to dispute "
    "information directly with furnishers. When a furnisher receives a direct dispute, "
    "it must:\n"
    "1. Conduct an investigation (§ 1022.43(a))\n"
    "2. Review all relevant information provided by the consumer (§ 1022.43(d))\n"
    "3. Report the results to ALL CRAs to which it furnishes (§ 1022.43(e))\n"
    "4. Modify, delete, or permanently block disputed information if inaccurate "
    "(15 U.S.C. § 1681s-2(a)(8)(E))\n\n"
    "KEY ADVANTAGE: Direct furnisher disputes bypass e-OSCAR entirely. The furnisher "
    "receives the consumer's ACTUAL letter with ACTUAL evidence — not a 2-digit code. "
    "There is no 'frivolous' exception for direct disputes as there is for CRA disputes "
    "under § 1681i(a)(3).\n\n"
    "IMPORTANT DISTINCTIONS:\n"
    "- DO NOT cite CRA duties (§ 1681i) — cite FURNISHER duties (§ 1681s-2, § 1022.43)\n"
    "- DO NOT reference e-OSCAR or ACDV codes — this letter bypasses that system\n"
    "- DO reference Regulation V, the furnisher's duty under § 1681s-2(a)(1)(A), and "
    "the consequences of willful noncompliance under § 1681n\n\n"
    + _EDUCATIONAL_NOTE
)


# ─── Notice of Dispute (Tier 1) ───

NOTICE_OF_DISPUTE_TEMPLATE = (
    "Generate a formal Notice of Dispute letter to {bureau_name}.\n\n"
    "The following accounts are being formally disputed:\n\n"
    "{account_table}\n\n"
    "This is a Dispute Prep notice under the Fair Credit Reporting Act, "
    "15 U.S.C. § 1681i. Demand a 30-day investigation of each account listed. "
    "State that the consumer is aware this dispute will be processed through the "
    "e-OSCAR system and that a code-only ACDV verification does not constitute "
    "a 'reasonable investigation' under 15 U.S.C. § 1681i. The detailed "
    "Bureau Assault letter with specific inaccuracies and legal citations will follow."
)


def build_notice_of_dispute_prompt(bureau, accounts, client_context):
    """
    Build a Notice of Dispute prompt for a single bureau.

    Args:
        bureau: Bureau name (e.g., 'Experian', 'TransUnion', 'Equifax')
        accounts: List of account dicts for this bureau (from parser)
        client_context: Dict with client_full_name, client_address, etc.

    Returns:
        Tuple of (prompt_string, False, False) — no inaccuracies, no legal research.
    """
    # Build account table
    rows = []
    for i, acct in enumerate(accounts, 1):
        name = acct.get('account_name', 'Unknown')
        number = acct.get('account_number', 'Unknown')
        acct_type = acct.get('account_type', 'Unknown')
        rows.append(f"{i}. {name} | Account #: {number} | Type: {acct_type}")

    account_table = "\n".join(rows)

    # Build preamble with client context
    ctx = {
        'entity': bureau,
        'client_full_name': '',
        'client_address': '',
        'client_address_line2': '',
        'client_city_state_zip': '',
        'today_date': '',
        'creditor_address': '',
        'creditor_city_state_zip': '',
        'bureau_address': '',
    }
    ctx.update(client_context)

    addr2 = ctx.get('client_address_line2', '').strip()
    ctx['client_address_line2_section'] = f"{addr2}\n" if addr2 else ''

    recip_addr = ctx.get('bureau_address', '')
    if recip_addr:
        ctx['recipient_address_section'] = f"Address: {recip_addr}\n"
    else:
        ctx['recipient_address_section'] = ''

    preamble = CLIENT_CONTEXT_PREAMBLE.format(**ctx)

    body = NOTICE_OF_DISPUTE_TEMPLATE.format(
        bureau_name=bureau,
        account_table=account_table
    )

    return preamble + body, False, False


def generate_letter(prompt, model="o3", has_inaccuracies=False,
                    has_legal_research=False, is_notice=False):
    """
    Generate a dispute letter using GPT.

    Args:
        prompt: The filled-in prompt template.
        model: OpenAI model to use.
        has_inaccuracies: If True, uses enhanced system prompt that instructs
                          GPT to incorporate parsed inaccuracy findings.
        has_legal_research: If True, uses the full legal research system prompt
                            (includes CFPB data + case law citation instructions).
        is_notice: If True, uses the Notice of Dispute system prompt (Tier 1).

    Returns:
        The generated letter text.
    """
    if is_notice:
        system_prompt = SYSTEM_PROMPT_NOTICE_OF_DISPUTE
    elif has_legal_research:
        system_prompt = SYSTEM_PROMPT_WITH_LEGAL_RESEARCH
    elif has_inaccuracies:
        system_prompt = SYSTEM_PROMPT_WITH_INACCURACIES
    else:
        system_prompt = SYSTEM_PROMPT_BASE

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content


def generate_letter_with_quality_gate(
    prompt, model="o3", has_inaccuracies=False, has_legal_research=False,
    is_notice=False, quality_context=None, max_retries=2,
):
    """
    Generate a letter and run it through the quality gate.
    If the gate fails, regenerate with failure feedback (up to max_retries).

    Args:
        prompt: The filled-in prompt template.
        model: OpenAI model to use.
        quality_context: Dict with keys for check_letter_quality():
            account_name, account_number, bureau, prompt_pack,
            round_number, client_full_name, client_address,
            parsed_balance, parsed_dofd, user_provided_docs
        max_retries: Max regeneration attempts on quality failure.

    Returns:
        (letter_text, quality_result) tuple
    """
    from services.letter_quality_gate import check_letter_quality, format_failures_for_retry

    ctx = quality_context or {}
    current_prompt = prompt

    for attempt in range(1 + max_retries):
        letter_text = generate_letter(
            current_prompt, model=model,
            has_inaccuracies=has_inaccuracies,
            has_legal_research=has_legal_research,
            is_notice=is_notice,
        )

        result = check_letter_quality(
            letter_text=letter_text,
            account_name=ctx.get('account_name', ''),
            account_number=ctx.get('account_number', ''),
            bureau=ctx.get('bureau', ''),
            prompt_pack=ctx.get('prompt_pack', 'default'),
            round_number=ctx.get('round_number', 1),
            client_full_name=ctx.get('client_full_name', ''),
            client_address=ctx.get('client_address', ''),
            parsed_balance=ctx.get('parsed_balance'),
            parsed_dofd=ctx.get('parsed_dofd'),
            user_provided_docs=ctx.get('user_provided_docs', []),
        )

        if result.passed:
            return letter_text, result

        # If last attempt, return what we have with failures attached
        if attempt >= max_retries:
            return letter_text, result

        # Inject failure feedback into prompt for retry
        failure_feedback = format_failures_for_retry(result)
        current_prompt = f"{failure_feedback}\n\n{prompt}"

    return letter_text, result


# ═══════════════════════════════════════════════════════════
#  Async / Parallel Letter Generation
# ═══════════════════════════════════════════════════════════

async def generate_letter_async(prompt, model="o3", has_inaccuracies=False,
                                has_legal_research=False, is_notice=False):
    """Async version of generate_letter — uses AsyncOpenAI client."""
    if is_notice:
        system_prompt = SYSTEM_PROMPT_NOTICE_OF_DISPUTE
    elif has_legal_research:
        system_prompt = SYSTEM_PROMPT_WITH_LEGAL_RESEARCH
    elif has_inaccuracies:
        system_prompt = SYSTEM_PROMPT_WITH_INACCURACIES
    else:
        system_prompt = SYSTEM_PROMPT_BASE

    response = await async_openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content


async def generate_letter_with_quality_gate_async(
    prompt, model="o3", has_inaccuracies=False, has_legal_research=False,
    is_notice=False, quality_context=None, max_retries=2,
):
    """Async version of generate_letter_with_quality_gate."""
    from services.letter_quality_gate import check_letter_quality, format_failures_for_retry

    ctx = quality_context or {}
    current_prompt = prompt

    for attempt in range(1 + max_retries):
        letter_text = await generate_letter_async(
            current_prompt, model=model,
            has_inaccuracies=has_inaccuracies,
            has_legal_research=has_legal_research,
            is_notice=is_notice,
        )

        result = check_letter_quality(
            letter_text=letter_text,
            account_name=ctx.get('account_name', ''),
            account_number=ctx.get('account_number', ''),
            bureau=ctx.get('bureau', ''),
            prompt_pack=ctx.get('prompt_pack', 'default'),
            round_number=ctx.get('round_number', 1),
            client_full_name=ctx.get('client_full_name', ''),
            client_address=ctx.get('client_address', ''),
            parsed_balance=ctx.get('parsed_balance'),
            parsed_dofd=ctx.get('parsed_dofd'),
            user_provided_docs=ctx.get('user_provided_docs', []),
        )

        if result.passed or attempt >= max_retries:
            return letter_text, result

        failure_feedback = format_failures_for_retry(result)
        current_prompt = f"{failure_feedback}\n\n{prompt}"

    return letter_text, result


async def generate_letters_batch(tasks):
    """
    Generate multiple letters concurrently using asyncio.gather().

    Args:
        tasks: List of dicts, each with keys:
            - prompt (str): The filled prompt
            - has_inaccuracies (bool)
            - has_legal_research (bool)
            - is_notice (bool)
            - quality_context (dict, optional)
            - account_id: Passed through for tracking

    Returns:
        List of dicts with keys:
            - account_id: From input
            - letter_text: Generated letter
            - quality_result: Quality gate result object
            - error: Error message if generation failed, else None
    """
    async def _run_one(task):
        try:
            letter_text, qr = await generate_letter_with_quality_gate_async(
                prompt=task['prompt'],
                has_inaccuracies=task.get('has_inaccuracies', False),
                has_legal_research=task.get('has_legal_research', False),
                is_notice=task.get('is_notice', False),
                quality_context=task.get('quality_context'),
            )
            return {
                'account_id': task.get('account_id'),
                'account_ids': task.get('account_ids', []),
                'letter_text': letter_text,
                'quality_result': qr,
                'error': None,
            }
        except Exception as e:
            return {
                'account_id': task.get('account_id'),
                'account_ids': task.get('account_ids', []),
                'letter_text': None,
                'quality_result': None,
                'error': str(e),
            }

    results = await asyncio.gather(*[_run_one(t) for t in tasks])
    return list(results)


async def generate_dual_letters_async(cra_prompt, furnisher_prompt, model="o3",
                                      has_inaccuracies=False, has_legal_research=False):
    """Async version of generate_dual_letters — both letters generated concurrently."""
    if has_legal_research:
        cra_system = SYSTEM_PROMPT_WITH_LEGAL_RESEARCH
    elif has_inaccuracies:
        cra_system = SYSTEM_PROMPT_WITH_INACCURACIES
    else:
        cra_system = SYSTEM_PROMPT_BASE

    cra_task = async_openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": cra_system},
            {"role": "user", "content": cra_prompt}
        ]
    )
    furnisher_task = async_openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_FURNISHER_DIRECT},
            {"role": "user", "content": furnisher_prompt}
        ]
    )

    cra_response, furnisher_response = await asyncio.gather(cra_task, furnisher_task)
    return cra_response.choices[0].message.content, furnisher_response.choices[0].message.content


def build_prompt(template_pack, template_index, context, parsed_accounts=None,
                 legal_research_context=None):
    """
    Build a filled prompt from a template pack, prepended with client context.

    If parsed_accounts are provided (from the credit report parser), their
    inaccuracies are automatically mapped to FCRA violations and injected
    into the prompt so GPT generates a case-specific dispute letter.

    If legal_research_context is provided (from the Legal Research Agent),
    CFPB complaint data and case law citations are appended to the prompt.

    Args:
        template_pack: Key from PACKS dict (e.g., 'default', 'arbitration').
        template_index: Index of the template within the pack.
        context: Dict with keys: entity, account_name, account_number, marks, action, issue,
                 and optionally client_full_name, client_address, client_city_state_zip,
                 today_date, dispute_date, days, etc.
        parsed_accounts: Optional list of account dicts from the credit report parser.
                         If provided, inaccuracies are extracted and injected into the prompt.
        legal_research_context: Optional string from legal_research.research_for_prompt().
                                If provided, CFPB data and case law are injected into the prompt.

    Returns:
        Tuple of (filled_prompt_string, has_inaccuracies_bool, has_legal_research_bool).
    """
    templates = PACKS.get(template_pack, PACKS['default'])
    idx = min(template_index, len(templates) - 1)

    # Ensure all expected keys have defaults
    ctx = {
        'entity': '',
        'account_name': '',
        'account_number': '',
        'marks': '',
        'action': '',
        'issue': '',
        'dispute_date': '',
        'days': '',
        'client_full_name': '',
        'client_address': '',
        'client_address_line2': '',
        'client_city_state_zip': '',
        'today_date': '',
        'creditor_address': '',
        'creditor_city_state_zip': '',
        'bureau_address': '',
    }
    ctx.update(context)

    # Build optional sections for preamble (only show if data exists)
    addr2 = ctx.get('client_address_line2', '').strip()
    ctx['client_address_line2_section'] = f"{addr2}\n" if addr2 else ''

    recip_addr = ctx.get('creditor_address') or ctx.get('bureau_address', '')
    recip_csz = ctx.get('creditor_city_state_zip', '').strip(', ')
    if recip_addr:
        ctx['recipient_address_section'] = f"Address: {recip_addr}\n{recip_csz}\n" if recip_csz else f"Address: {recip_addr}\n"
    else:
        ctx['recipient_address_section'] = ''

    # Prepend client context preamble + the template body
    preamble = CLIENT_CONTEXT_PREAMBLE.format(**ctx)
    body = templates[idx].format(**ctx)

    # Build inaccuracy details from parsed accounts if available
    has_inaccuracies = False
    inaccuracy_section = ""
    if parsed_accounts:
        # If context specifies a specific account, filter to that one
        target_name = ctx.get('account_name', '').upper()
        target_number = ctx.get('account_number', '').upper()

        relevant_accounts = []
        for acct in parsed_accounts:
            acct_name = (acct.get('account_name') or '').upper()
            acct_num = (acct.get('account_number') or '').upper()
            # Match if the context account matches, or include all if no specific target
            if not target_name or target_name in acct_name or acct_name in target_name:
                if acct.get('inaccuracies'):
                    relevant_accounts.append(acct)

        if relevant_accounts:
            inaccuracy_section = "\n\n" + build_inaccuracy_context_multi(relevant_accounts)
            has_inaccuracies = True

    # Append legal research context if provided
    has_legal_research = False
    legal_section = ""
    if legal_research_context and legal_research_context.strip():
        legal_section = "\n\n--- LEGAL RESEARCH FINDINGS ---\n\n" + legal_research_context
        has_legal_research = True

    return preamble + body + inaccuracy_section + legal_section, has_inaccuracies, has_legal_research


# ═══════════════════════════════════════════════════════════
#  Round 2+ Consolidated Letter (1 per bureau, N accounts)
# ═══════════════════════════════════════════════════════════

CONSOLIDATED_ROUND2_TEMPLATE = (
    "Write a formal ESCALATION dispute letter to {entity}.\n\n"
    "This is Round {round_number}. The accounts listed below were previously disputed "
    "and came back verified, received no response within 30 days, or received a generic "
    "stall letter that failed to address the specific dispute points.\n\n"
    "{accounts_block}\n\n"
    "Follow all e-OSCAR RULES from the system prompt. Additionally:\n"
    "- Address EACH account with its own clearly labeled DISPUTE POINT section\n"
    "- For each account, cite the specific Metro 2 fields that are inaccurate or unverified\n"
    "- Reference the prior dispute and the bureau's failure to conduct a reasonable investigation "
    "under § 1681i(a)\n"
    "- Cite Cushman v. Trans Union, Gorman v. Wolpoff & Abramson, CFPB v. Experian (2025), "
    "CFPB v. Equifax (2025)\n"
    "- Include a single Method of Verification (MOV) demand covering all accounts — "
    "demand the name, address, and telephone number of each furnisher contacted, the specific "
    "documents reviewed, and the employee who conducted the investigation\n"
    "- Direct all furnisher document demands THROUGH the CRA under § 1681i(a)(2), "
    "not at the CRA itself\n"
    "- End with a unified demand: if any account cannot be verified with documentary evidence "
    "within 30 days, it must be deleted per § 1681i(a)(5)(A), and failure to comply will "
    "constitute willful noncompliance under § 1681n with statutory damages of $100-$1,000 per violation"
)


def build_prompt_multi(template_pack, context, accounts_list,
                       parsed_accounts=None, legal_research_context=None):
    """
    Build a single prompt referencing MULTIPLE accounts for a consolidated bureau letter.

    Used in Round 2+ where all unresolved accounts for one bureau are bundled
    into a single escalation letter.

    Args:
        template_pack: Key from PACKS dict (for escalation-specific language).
        context: Dict with client info + entity (bureau name) + round_number.
        accounts_list: List of dicts, each with:
            account_name, account_number, status, issue, dispute_reason, prev_outcome
        parsed_accounts: Optional list of account dicts from the credit report parser.
        legal_research_context: Optional string with CFPB + case law data.

    Returns:
        Tuple of (filled_prompt_string, has_inaccuracies_bool, has_legal_research_bool).
    """
    # Build the multi-account block
    account_lines = []
    for i, acct in enumerate(accounts_list, 1):
        prev = acct.get('prev_outcome', 'verified')
        outcome_desc = {
            'verified': 'Bureau claimed verification without adequate investigation',
            'no_response': 'No response received within the 30-day statutory window (FCRA violation)',
            'stall': 'Received generic form response that did not address dispute points',
        }.get(prev, 'Unresolved from prior round')

        account_lines.append(
            f"Account {i}:\n"
            f"  Name: {acct.get('account_name', 'Unknown')}\n"
            f"  Account #: {acct.get('account_number', 'N/A')}\n"
            f"  Reported Status: {acct.get('status', 'Negative')}\n"
            f"  Dispute Issues: {acct.get('issue', 'Inaccurate reporting')}\n"
            f"  Prior Round Result: {outcome_desc}"
        )

    accounts_block = "\n\n".join(account_lines)

    # Defaults for context
    ctx = {
        'entity': '',
        'client_full_name': '',
        'client_address': '',
        'client_address_line2': '',
        'client_city_state_zip': '',
        'today_date': '',
        'round_number': 2,
        'creditor_address': '',
        'creditor_city_state_zip': '',
        'bureau_address': '',
    }
    ctx.update(context)

    # Build address sections
    addr2 = ctx.get('client_address_line2', '').strip()
    ctx['client_address_line2_section'] = f"{addr2}\n" if addr2 else ''
    recip_addr = ctx.get('creditor_address') or ctx.get('bureau_address', '')
    recip_csz = ctx.get('creditor_city_state_zip', '').strip(', ')
    if recip_addr:
        ctx['recipient_address_section'] = f"Address: {recip_addr}\n{recip_csz}\n" if recip_csz else f"Address: {recip_addr}\n"
    else:
        ctx['recipient_address_section'] = ''

    preamble = CLIENT_CONTEXT_PREAMBLE.format(**ctx)
    body = CONSOLIDATED_ROUND2_TEMPLATE.format(
        entity=ctx['entity'],
        round_number=ctx.get('round_number', 2),
        accounts_block=accounts_block,
    )

    # Inaccuracy context from parsed accounts
    has_inaccuracies = False
    inaccuracy_section = ""
    if parsed_accounts:
        # Collect all accounts that match any of the disputed account names
        target_names = {(a.get('account_name') or '').upper() for a in accounts_list}
        relevant = [
            pa for pa in parsed_accounts
            if (pa.get('account_name') or '').upper() in target_names
            and pa.get('inaccuracies')
        ]
        if relevant:
            inaccuracy_section = "\n\n" + build_inaccuracy_context_multi(relevant)
            has_inaccuracies = True

    # Legal research context
    has_legal_research = False
    legal_section = ""
    if legal_research_context and legal_research_context.strip():
        legal_section = "\n\n--- LEGAL RESEARCH FINDINGS ---\n\n" + legal_research_context
        has_legal_research = True

    return preamble + body + inaccuracy_section + legal_section, has_inaccuracies, has_legal_research


# ─── Dual-Letter Generation Functions ───

def generate_dual_letters(cra_prompt, furnisher_prompt, model="o3",
                          has_inaccuracies=False, has_legal_research=False):
    """
    Generate TWO dispute letters for the dual-letter strategy:
    1. A CRA letter (through e-OSCAR, preserving § 1681s-2(b) rights)
    2. A direct furnisher letter (bypassing e-OSCAR under 12 CFR § 1022.43)

    Args:
        cra_prompt: The filled prompt for the CRA letter.
        furnisher_prompt: The filled prompt for the furnisher letter.
        model: OpenAI model to use.
        has_inaccuracies: If True, uses enhanced system prompt for CRA letter.
        has_legal_research: If True, uses legal research system prompt for CRA letter.

    Returns:
        Tuple of (cra_letter_text, furnisher_letter_text).
    """
    # Determine CRA system prompt
    if has_legal_research:
        cra_system = SYSTEM_PROMPT_WITH_LEGAL_RESEARCH
    elif has_inaccuracies:
        cra_system = SYSTEM_PROMPT_WITH_INACCURACIES
    else:
        cra_system = SYSTEM_PROMPT_BASE

    # Generate CRA letter
    cra_response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": cra_system},
            {"role": "user", "content": cra_prompt}
        ]
    )
    cra_letter = cra_response.choices[0].message.content

    # Generate furnisher letter
    furnisher_response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_FURNISHER_DIRECT},
            {"role": "user", "content": furnisher_prompt}
        ]
    )
    furnisher_letter = furnisher_response.choices[0].message.content

    return cra_letter, furnisher_letter


def build_dual_prompts(cra_pack, context, parsed_accounts=None,
                       legal_research_context=None):
    """
    Build prompts for both CRA and furnisher letters in the dual-letter strategy.

    Uses the specified CRA pack for the bureau letter, and the furnisher_direct
    pack for the direct furnisher letter.

    Args:
        cra_pack: Pack key for the CRA letter (e.g., 'default', 'arbitration').
        context: Dict with entity, account_name, account_number, marks, etc.
        parsed_accounts: Optional list of parsed account dicts with inaccuracies.
        legal_research_context: Optional legal research string.

    Returns:
        Tuple of (cra_prompt, furnisher_prompt, has_inaccuracies, has_legal_research).
    """
    # Build CRA letter prompt using the selected pack
    cra_prompt, has_inaccuracies, has_legal = build_prompt(
        cra_pack, 0, context,
        parsed_accounts=parsed_accounts,
        legal_research_context=legal_research_context
    )

    # Build furnisher letter prompt — swap entity to furnisher name
    furnisher_ctx = dict(context)
    # The entity for the furnisher letter is the account_name (creditor/collector)
    furnisher_ctx['entity'] = context.get('account_name', context.get('entity', ''))

    furnisher_prompt, _, _ = build_prompt(
        'furnisher_direct', 0, furnisher_ctx,
        parsed_accounts=parsed_accounts,
        legal_research_context=legal_research_context
    )

    return cra_prompt, furnisher_prompt, has_inaccuracies, has_legal


def letter_to_pdf(letter_text, output_path=None):
    """
    Convert letter text to a PDF file.

    Args:
        letter_text: The letter content.
        output_path: Where to save the PDF. If None, uses a temp file.

    Returns:
        Path to the generated PDF.
    """
    if output_path is None:
        output_path = os.path.join(tempfile.gettempdir(), 'letter.pdf')

    doc = SimpleDocTemplate(
        output_path, pagesize=LETTER,
        leftMargin=inch, rightMargin=inch,
        topMargin=inch, bottomMargin=inch
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='Body', parent=styles['Normal'],
        fontSize=11, leading=14
    ))
    story = []
    for line in letter_text.split('\n'):
        if not line.strip():
            story.append(Spacer(1, 12))
        else:
            story.append(Paragraph(line.strip(), styles['Body']))
    doc.build(story)
    return output_path


def image_to_pdf(image_path, output_path=None, field_type='other'):
    """
    Convert an image file to a single-page PDF with proper scaling.

    Args:
        image_path: Path to the image file.
        output_path: Where to save the PDF. If None, derives from image path.
        field_type: 'ssn_file' allows 130% upscale, others cap at 100%.

    Returns:
        Path to the generated PDF.
    """
    if output_path is None:
        output_path = os.path.splitext(image_path)[0] + '.pdf'

    page_w, page_h = LETTER
    margin = inch
    max_w = page_w - 2 * margin
    max_h = page_h - 2 * margin

    img = Image.open(image_path).convert('RGB')
    orig_w, orig_h = img.size

    raw_scale = min(max_w / orig_w, max_h / orig_h)

    if field_type == 'ssn_file':
        scale = min(raw_scale, 1.3)
    else:
        scale = min(raw_scale, 1.0)

    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)

    resized = img.resize((new_w, new_h), Image.LANCZOS)
    reader = ImageReader(resized)

    c = pdfcanvas.Canvas(output_path, pagesize=LETTER)
    x = (page_w - new_w) / 2
    y = (page_h - new_h) / 2
    c.drawImage(reader, x, y, width=new_w, height=new_h)
    c.showPage()
    c.save()

    return output_path


def merge_dispute_package(pdf_paths, output_path=None):
    """
    Merge multiple PDFs into a single DisputePackage.pdf.

    Args:
        pdf_paths: List of PDF file paths to merge.
        output_path: Where to save the merged PDF. If None, uses temp dir.

    Returns:
        Path to the merged PDF.
    """
    if output_path is None:
        output_path = os.path.join(tempfile.gettempdir(), 'DisputePackage.pdf')

    merger = PdfMerger()
    for p in pdf_paths:
        merger.append(p)
    merger.write(output_path)
    merger.close()

    return output_path
