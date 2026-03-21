"""
Dispute letter generation service.
Extracted from dispute_ui.py — handles prompt packs and GPT letter generation.
"""

import os
import tempfile
from openai import OpenAI
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
}


def classify_inaccuracy(inaccuracy_text):
    """Classify an inaccuracy string into an FCRA category."""
    text = inaccuracy_text.lower()
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
            "Write a formal dispute letter to {entity} demanding investigation and correction "
            "of the following account:\n\n"
            "Account: {account_name}\n"
            "Account Number: {account_number}\n"
            "Reported Status: {marks}\n"
            "Issues: {issue}\n"
            "Requested Action: {action}\n\n"
            "CRITICAL STRUCTURE — MULTI-CATEGORY DISPUTE FORMAT:\n"
            "Structure the letter as 3+ separately labeled dispute points. Each point must "
            "target a DIFFERENT Metro 2 data field category so no single e-OSCAR code covers "
            "the entire dispute:\n"
            "  DISPUTE POINT 1 — ACCOUNT STATUS: [Cite the Account Status field by name. "
            "State the reported value and why it is factually wrong.]\n"
            "  DISPUTE POINT 2 — BALANCE/FINANCIAL DATA: [Cite the Current Balance field "
            "and/or Credit Limit field. State the reported dollar amount and why it is wrong.]\n"
            "  DISPUTE POINT 3 — PAYMENT HISTORY/DATES: [Cite the Payment History Profile "
            "and/or Date of First Delinquency field. State the reported dates and why they "
            "are wrong.]\n"
            "This structure forces the CRA employee to choose between Code 002 (balance), "
            "Code 103 (status), Code 023 (account info), and Code 105 (payment history) — "
            "they cannot select one code, so they must escalate.\n\n"
            "METHOD OF VERIFICATION DEMAND (INCLUDE VERBATIM):\n"
            "'Pursuant to 15 U.S.C. § 1681i(a)(7), I hereby request that upon completion "
            "of your investigation, you provide me with a description of the procedure used "
            "to determine the accuracy of the disputed information, including the name, "
            "address, and telephone number of each furnisher contacted. You have 15 days "
            "to comply.'\n\n"
            "LEGAL CITATIONS (weave into dispute points, not a separate block):\n"
            "- 15 U.S.C. § 1681i(a) — CRA duty to investigate within 30 days\n"
            "- 15 U.S.C. § 1681s-2(b) — Furnisher duty to investigate\n"
            "- 15 U.S.C. § 1681i(a)(5)(A) — Mandatory deletion if unverifiable\n"
            "- *Cushman v. Trans Union Corp.*, 115 F.3d 220 (3d Cir. 1997) — CRA cannot "
            "parrot furnisher response\n"
            "- *Gorman v. Wolpoff & Abramson*, 584 F.3d 1147 (9th Cir. 2009) — furnisher "
            "must conduct 'fairly searching inquiry'\n"
            "- Reference CFPB v. Experian, Case No. 1:25-cv-00001 (C.D. Cal. filed Jan. "
            "2025, pending) and CFPB v. Equifax, Consent Order 2025-CFPB-0002 ($15M penalty) "
            "for conducting 'sham investigations' via e-OSCAR\n"
            "- Include anti-compression warning: 'Any attempt to compress this multi-field "
            "dispute into a single e-OSCAR ACDV code will constitute willful noncompliance "
            "under 15 U.S.C. § 1681n.'\n\n"
            "DOCUMENTARY SUPPORT:\n"
            "- Instruct consumer to enclose driver's license (front & back) and recent "
            "utility bill as 'Enclosures' to pre-empt frivolous claim under § 1681i(a)(3)\n"
            "- Direct furnisher document demands THROUGH the CRA, not at the CRA itself\n\n"
            "CONSTRAINTS:\n"
            "- Keep under 650 words\n"
            "- Every disputed field must state the REPORTED value and WHY it is wrong\n"
            "- Use Metro 2 field names with segment/field numbers (Account Status – Fld 17, "
            "Current Balance – Fld 21, Payment History Profile – Fld 25, DOFD – Fld 26)"
        ),
        (
            "Write a dispute letter to {entity} challenging the accuracy and completeness "
            "of reporting on account {account_name} (#{account_number}).\n\n"
            "Current Status: {marks}\n"
            "Dispute Issue: {issue}\n"
            "Demanded Resolution: {action}\n\n"
            "FORMAT: Structure as 3+ labeled DISPUTE POINTS, each targeting a DIFFERENT "
            "Metro 2 field category (status, balance, payment history, dates) so no single "
            "e-OSCAR code can capture the entire dispute.\n\n"
            "REQUIRED ELEMENTS:\n"
            "1. Reference Metro 2 field names in each dispute point (Account Status field, "
            "Current Balance field, Payment History Profile, Date of First Delinquency, etc.)\n"
            "2. State the REPORTED value and WHY it is factually incorrect — never generic\n"
            "3. Cite *Cushman v. Trans Union* (cannot parrot furnisher) and *Gorman v. Wolpoff* "
            "(must conduct 'fairly searching inquiry')\n"
            "4. Reference CFPB v. Experian, Case No. 1:25-cv-00001 (C.D. Cal. 2025) for "
            "conducting 'sham investigations'\n"
            "5. Include verbatim method of verification request per § 1681i(a)(7)\n"
            "6. Demand CRA direct furnisher to produce original credit agreement, payment "
            "ledger, and chain of assignment — deletion mandatory if unproduced per "
            "§ 1681i(a)(5)(A)\n"
            "7. Include anti-compression warning: compressing into a single code = willful "
            "noncompliance under § 1681n\n"
            "8. Instruct consumer to enclose driver's license and utility bill as 'Enclosures'\n"
            "9. Use Metro 2 field numbers (Fld 17, 21, 25, 26) alongside field names\n"
            "10. Keep under 650 words"
        ),
    ],
    "arbitration": [
        (
            "Draft a formal arbitration demand letter to {entity} regarding account "
            "{account_name} (#{account_number}).\n\n"
            "Reported Status: {marks}\n"
            "Issue: {issue}\n"
            "Demanded Action: {action}\n\n"
            "STRUCTURE — DUAL-TRACK LETTER:\n"
            "This letter serves TWO purposes simultaneously:\n"
            "Track 1 — DISPUTE: Challenge specific Metro 2 data fields as factually "
            "inaccurate using 4+ labeled DISPUTE POINTS, each targeting a different field "
            "category (Account Status – Base Seg. Fld 17, Current Balance – Base Seg. Fld 21, "
            "Payment History Profile – Base Seg. Fld 25, Date of First Delinquency – "
            "Base Seg. Fld 26). State the REPORTED value and WHY it is wrong — never "
            "generic 'please verify.'\n"
            "Track 2 — ARBITRATION DEMAND: Demand binding arbitration as the consumer's "
            "elected remedy for the CRA's structurally unreasonable investigation procedures.\n\n"
            "ANTI-CODE-REDUCTION WARNING (include in letter body):\n"
            "'Any attempt to compress this multi-field dispute into a single e-OSCAR ACDV "
            "code (001, 103, or 106) will constitute willful noncompliance under 15 U.S.C. "
            "§ 1681n, as established in CFPB v. Experian, Case No. 1:25-cv-00001 (C.D. Cal. "
            "filed Jan. 2025, pending), where the CFPB found the CRA conducted sham "
            "investigations by distorting consumer disputes into e-OSCAR codes.'\n\n"
            "ARBITRATION LEGAL FRAMEWORK:\n"
            "- 15 U.S.C. § 1681e(b) — CRA must follow reasonable procedures to assure "
            "maximum possible accuracy. The e-OSCAR system's 4-minute code reduction is "
            "inherently unreasonable for complex, multi-field disputes.\n"
            "- 15 U.S.C. § 1681n — Willful noncompliance: statutory damages of $100-$1,000 "
            "per violation PLUS punitive damages.\n"
            "- 15 U.S.C. § 1681o — Negligent noncompliance: actual damages plus attorney fees\n"
            "- *Cushman v. Trans Union Corp.*, 115 F.3d 220 (3d Cir. 1997) — punitive "
            "damages available when CRA adopts reinvestigation policy 'either knowing that "
            "policy to be in contravention of consumer rights or in reckless disregard'\n"
            "- *Gorman v. Wolpoff*, 584 F.3d 1147 (9th Cir. 2009) — furnisher must conduct "
            "'fairly searching inquiry,' not superficial verification\n"
            "- CFPB v. Equifax, Consent Order 2025-CFPB-0002 ($15M penalty) — CRA fined "
            "for ignoring consumer documents and excessively deferring to furnishers\n\n"
            "DNR TRAP — FURNISHER-DIRECTED DEMANDS:\n"
            "Demand that {entity} FORWARD to the data furnisher a request to produce: "
            "(a) the signed original credit agreement, (b) a complete payment ledger, "
            "(c) chain of assignment if debt was sold. Do NOT demand {entity} produce "
            "these directly — direct the demand THROUGH the CRA to the furnisher. "
            "Non-production within 30 days mandates deletion per § 1681i(a)(5)(A).\n\n"
            "METHOD OF VERIFICATION DEMAND (INCLUDE VERBATIM):\n"
            "'Pursuant to 15 U.S.C. § 1681i(a)(7) and 12 C.F.R. § 1022.43, provide a "
            "detailed description of the procedure used, the name/address/phone of every "
            "furnisher contacted, and the specific documentary evidence relied upon, within "
            "15 days of completing your reinvestigation.'\n\n"
            "IDENTITY & DOCUMENTARY SUPPORT:\n"
            "Instruct the consumer to enclose: (1) copy of driver's license (front & back), "
            "(2) recent utility bill or bank statement confirming address, and reference "
            "these as 'Enclosures' in the letter to pre-empt § 1681i(a)(3) frivolous claims.\n\n"
            "CONSTRAINTS:\n"
            "- Keep under 650 words\n"
            "- Use Metro 2 segment/field references in every dispute point\n"
            "- The arbitration demand must be grounded in the CRA's SPECIFIC failure to "
            "investigate THIS dispute properly — not a generic threat"
        ),
        (
            "Compose a formal arbitration demand for {entity} concerning account "
            "{account_name} (#{account_number}).\n\n"
            "Current Status: {marks}\n"
            "Dispute: {issue}\n"
            "Demanded Resolution: {action}\n\n"
            "FORMAT: Structure as 4+ labeled DISPUTE POINTS targeting different Metro 2 "
            "field categories (cite field numbers: Fld 17, Fld 21, Fld 25, Fld 26), PLUS "
            "an arbitration demand section.\n\n"
            "REQUIRED ELEMENTS:\n"
            "1. Each dispute point cites a Metro 2 field by name AND segment/field number "
            "(Account Status – Base Seg. Fld 17, Current Balance – Fld 21, etc.) "
            "with the REPORTED value and WHY it is factually wrong — never generic\n"
            "2. Cite CFPB v. Experian, Case No. 1:25-cv-00001 (C.D. Cal. filed Jan. 2025, "
            "pending) — CRA found conducting 'sham investigations' by distorting consumer "
            "disputes into e-OSCAR codes\n"
            "3. Cite CFPB v. Equifax, Consent Order 2025-CFPB-0002 ($15M penalty) — CRA "
            "fined for ignoring consumer documents and deferring to furnishers\n"
            "4. Demand binding arbitration under § 1681n, quantifying potential statutory "
            "damages: $100-$1,000 per violation for each Metro 2 field that cannot be "
            "independently verified by the furnisher\n"
            "5. Cite *Cushman v. Trans Union* — punitive damages for willful noncompliance\n"
            "6. Include verbatim MOV request per § 1681i(a)(7) and 12 C.F.R. § 1022.43\n"
            "7. Include anti-code-reduction warning: compressing this dispute into a single "
            "code constitutes willful noncompliance under § 1681n\n"
            "8. Direct furnisher document demands THROUGH the CRA, not at the CRA itself\n"
            "9. Instruct consumer to enclose driver's license and utility bill as 'Enclosures'\n"
            "10. Keep under 650 words"
        ),
    ],
    "consumer_law": [
        (
            "Write a statutory demand letter to {entity} invoking multiple consumer protection "
            "statutes regarding account {account_name} (#{account_number}).\n\n"
            "Issue: {issue}\n"
            "Demanded Action: {action}\n\n"
            "Cite the following statutes as applicable:\n"
            "- Fair Credit Reporting Act (15 U.S.C. § 1681 et seq.) — accuracy and investigation duties\n"
            "- Fair Debt Collection Practices Act (15 U.S.C. § 1692g) — debt validation requirements\n"
            "- Fair Credit Billing Act (15 U.S.C. § 1666) — billing error resolution\n"
            "- CFPB v. Experian (2025) — CRA found conducting 'sham investigations' via e-OSCAR\n\n"
            "Demand that the furnisher produce original documentation proving the accuracy of "
            "every reported field. A code-only verification through e-OSCAR does not satisfy "
            "the statutory investigation requirement."
        ),
        (
            "Craft a multi-statute demand letter to {entity} for account {account_name} "
            "(#{account_number}). Dispute: {issue}. Resolution demanded: {action}. "
            "Combine FCRA, FDCPA, and state consumer protection law arguments. Reference "
            "the e-OSCAR system's structural inability to handle complex disputes and "
            "the requirement for genuine investigation under 15 U.S.C. § 1681i."
        ),
    ],
    "ACDV_response": [
        (
            "Compose a formal ACDV enforcement demand to {entity} regarding the disputed "
            "account {account_name} (#{account_number}).\n\n"
            "Original Dispute Date: {dispute_date}\n"
            "Deadline: {days} business days\n\n"
            "Invoke *Cushman v. Trans Union Corp.*, 115 F.3d 220 (3d Cir. 1997), and demand "
            "immediate production of the full ACDV record, including:\n"
            "- Method of Verification used by the furnisher\n"
            "- The specific e-OSCAR Response Code received (01/02/07/13)\n"
            "- Whether consumer-provided documentation was forwarded to the furnisher\n"
            "- The furnisher's investigation file and contact logs\n"
            "- FCRA Compliance Policies governing reinvestigation procedures\n\n"
            "State that if the CRA's 'investigation' consisted solely of forwarding an "
            "e-OSCAR code and accepting a Response Code 01 without independent verification, "
            "this constitutes a failure to conduct a reasonable investigation under "
            "15 U.S.C. § 1681i, as held in Cushman v. Trans Union, 115 F.3d 220."
        ),
        (
            "Compose a formal demand to {entity} regarding flawed reinvestigation procedures "
            "for account {account_name} (#{account_number}). Dispute Date: {dispute_date}. "
            "Invoke *Gillespie v. Equifax Info. Servs.*, 484 F.3d 938, holding CRAs liable "
            "for unreasonable investigation processes. Demand documented proof of each step "
            "of the reinvestigation — including the e-OSCAR ACDV exchange, Response Code "
            "received, source contact logs, and verification methodologies — within {days} "
            "business days."
        ),
        (
            "Write a formal demand to {entity} for immediate production of the complete "
            "ACDV record for account {account_name} (#{account_number}) related to the "
            "dispute filed on {dispute_date}. Demand disclosure of the e-OSCAR Response Code, "
            "the method of verification, and whether consumer documentation was forwarded to "
            "the furnisher. Set a {days} business day deadline."
        ),
    ],
    "furnisher_direct": [
        (
            "Write a formal direct dispute letter to the data furnisher {entity} regarding "
            "account {account_name} (#{account_number}).\n\n"
            "Reported Status: {marks}\n"
            "Issue: {issue}\n"
            "Demanded Action: {action}\n\n"
            "IMPORTANT: This letter is sent DIRECTLY to the data furnisher (creditor or "
            "collector) — NOT to a credit bureau. It bypasses the e-OSCAR system entirely. "
            "The furnisher is legally required to investigate under 12 C.F.R. § 1022.43 "
            "(Regulation V — Direct Disputes) and 15 U.S.C. § 1681s-2(a)(8).\n\n"
            "STRUCTURE — DIRECT FURNISHER DISPUTE:\n"
            "1. IDENTIFY THE ACCOUNT with full detail: creditor name, account number, "
            "reported status, balance, and dates as shown on the consumer's credit report.\n"
            "2. ALLEGE SPECIFIC INACCURACIES using 3+ labeled DISPUTE POINTS, each "
            "referencing the Metro 2 field being disputed (Account Status – Fld 17, "
            "Current Balance – Fld 21, Payment History Profile – Fld 25, DOFD – Fld 26). "
            "State the REPORTED value and WHY it is factually wrong.\n"
            "3. CITE DIRECT DISPUTE AUTHORITY:\n"
            "   - 12 C.F.R. § 1022.43(a) — Furnisher must investigate direct disputes\n"
            "   - 12 C.F.R. § 1022.43(e) — Furnisher must report investigation results "
            "to ALL CRAs to which it furnishes data\n"
            "   - 15 U.S.C. § 1681s-2(a)(8)(E) — Furnisher must modify, delete, or "
            "permanently block reporting of disputed information if found inaccurate\n"
            "   - 15 U.S.C. § 1681s-2(a)(1)(A) — Duty not to report known inaccuracies\n"
            "4. DEMAND DOCUMENTATION: Original signed credit agreement, complete payment "
            "ledger, validation of the debt amount, and chain of assignment if sold.\n"
            "5. SET 30-DAY DEADLINE with consequences: failure to investigate and correct "
            "within 30 days constitutes willful noncompliance under § 1681n (statutory "
            "damages $100-$1,000 per violation plus punitive damages).\n"
            "6. STATE that this letter constitutes a 'direct dispute' under Regulation V "
            "and that the furnisher is prohibited from ignoring it or treating it as a "
            "frivolous complaint — unlike CRA disputes, direct disputes have no "
            "frivolous exception.\n\n"
            "CASE LAW:\n"
            "- *Gorman v. Wolpoff & Abramson*, 584 F.3d 1147 (9th Cir. 2009) — furnisher "
            "investigation must be a 'fairly searching inquiry'\n"
            "- *Boggio v. USAA Federal Savings Bank*, 696 F.3d 611 (6th Cir. 2012) — "
            "furnisher cannot rely solely on its own records; must consider consumer "
            "evidence\n\n"
            "CONSTRAINTS:\n"
            "- Keep under 650 words\n"
            "- Tone: firm, professional, legally precise\n"
            "- DO NOT reference e-OSCAR or ACDV — this letter bypasses that system\n"
            "- DO NOT cite CRA duties (§ 1681i) — cite FURNISHER duties (§ 1681s-2)"
        ),
        (
            "Compose a direct dispute letter to the data furnisher {entity} for account "
            "{account_name} (#{account_number}).\n\n"
            "Current Status: {marks}\n"
            "Dispute: {issue}\n"
            "Demanded Resolution: {action}\n\n"
            "This letter goes DIRECTLY to the furnisher, bypassing the e-OSCAR system.\n\n"
            "REQUIRED ELEMENTS:\n"
            "1. 3+ labeled DISPUTE POINTS with Metro 2 field references (Fld 17, 21, 25, 26) "
            "— state REPORTED value and WHY it is factually wrong\n"
            "2. Cite 12 C.F.R. § 1022.43 (Regulation V — Direct Disputes) as primary authority\n"
            "3. Cite § 1022.43(e) — furnisher must report results to ALL CRAs\n"
            "4. Cite 15 U.S.C. § 1681s-2(a)(8)(E) — duty to modify/delete/block if inaccurate\n"
            "5. Demand original agreement, payment ledger, debt validation, chain of assignment\n"
            "6. Cite *Gorman v. Wolpoff* — 'fairly searching inquiry' standard\n"
            "7. 30-day deadline with § 1681n willful noncompliance consequences\n"
            "8. State this is a direct dispute — no frivolous exception applies\n"
            "9. DO NOT reference e-OSCAR, ACDV, or CRA investigation duties\n"
            "10. Keep under 650 words"
        ),
    ],
}

# Preamble injected before every prompt so GPT uses real client data
CLIENT_CONTEXT_PREAMBLE = (
    "Write this dispute letter on behalf of the client below. "
    "Use their REAL name and address in the letter header, body, and signature. "
    "Do NOT use placeholder text like [YOUR NAME], [ADDRESS], or {{CLIENT_NAME}}.\n\n"
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
    {"key": "dual_letter", "name": "Dual-Letter Strategy", "description": "Two letters per dispute — one to the CRA (preserving lawsuit rights) + one direct to the furnisher under Regulation V (bypassing e-OSCAR entirely)."},
]


# ─── e-OSCAR Intelligence Block (injected into all system prompts) ───
# This block ensures GPT understands how the dispute system actually works
# and writes letters designed to survive the e-OSCAR automation pipeline.

_E_OSCAR_INTELLIGENCE = (
    "CRITICAL CONTEXT — HOW DISPUTES ARE ACTUALLY PROCESSED:\n"
    "When a consumer mails a dispute letter to a credit bureau (CRA), the letter "
    "does NOT go to the creditor. A CRA employee has approximately 4 minutes "
    "to reduce the entire dispute into a 2-3 digit code using the e-OSCAR system "
    "(Automated Consumer Dispute Verification / ACDV). A 2007 Congressional report "
    "found the same 4-5 codes are used for over 90% of all disputes. The creditor "
    "(data furnisher) receives ONLY this code — not the consumer's letter, not their "
    "evidence, not their arguments.\n\n"
    "The furnisher responds with one of these ACDV Response Codes:\n"
    "• Code 01 — 'Verified as accurate' (rubber stamp, no real investigation)\n"
    "• Code 02 — 'Modify account' (updates a field)\n"
    "• Code 07 — 'DELETE' (THIS IS THE TARGET)\n"
    "• Code 13 — 'Deleted per furnisher policy'\n"
    "• DNR (Did Not Respond) — Auto-delete after 30 days\n\n"
    "ENFORCEMENT PRECEDENT:\n"
    "• CFPB v. Experian, Case No. 1:25-cv-00001 (C.D. Cal. filed Jan. 2025, "
    "pending) — CFPB sued Experian for conducting 'sham investigations' by "
    "distorting, truncating, and mischaracterizing consumer disputes when "
    "converting them to e-OSCAR codes, and failing to forward consumer-submitted "
    "documentation to furnishers.\n"
    "• CFPB v. Equifax, Consent Order 2025-CFPB-0002 (final, $15M penalty) — "
    "Equifax fined for ignoring consumer-provided documents, using faulty "
    "software, and excessively deferring to furnishers rather than conducting "
    "independent review.\n"
    "• Cushman v. Trans Union Corp., 115 F.3d 220 (3d Cir. 1997) — CRAs cannot "
    "merely 'parrot back' furnisher responses. A reasonable reinvestigation may "
    "require going BEYOND the original source of information.\n"
    "• Gorman v. Wolpoff & Abramson, 584 F.3d 1147 (9th Cir. 2009) — Furnisher "
    "investigation must be a 'fairly searching inquiry,' not superficial.\n\n"
    "YOUR OBJECTIVE: Write letters that CANNOT be reduced to a simple e-OSCAR code. "
    "Force the CRA to conduct a genuine 'reasonable investigation' under "
    "15 U.S.C. § 1681i(a). The CRA must forward all consumer-provided documentation "
    "to the furnisher — automation alone does not satisfy this statutory duty.\n\n"
    "LETTER ENGINEERING RULES:\n"
    "1. USE METRO 2 FIELD REFERENCES WITH SEGMENT/FIELD NUMBERS — Reference specific "
    "Metro 2 data fields by name AND numeric identifier (e.g., 'Account Status – Base "
    "Seg. Fld 17,' 'Current Balance – Base Seg. Fld 21,' 'Date of First Delinquency – "
    "Base Seg. Fld 26,' 'Payment History Profile – Base Seg. Fld 25,' 'Credit Limit – "
    "Base Seg. Fld 22,' 'Payment Rating – Base Seg. Fld 18'). This technical specificity "
    "cannot be captured by any single e-OSCAR code and forces the CRA employee to "
    "escalate beyond the standard 4-minute processing. A letter that says 'Current "
    "Balance (Fld 21) reports $4,231 but Account Status (Fld 17) shows Closed/Paid' is "
    "impossible to reduce to Code 103.\n"
    "2. DISPUTE MULTIPLE DISTINCT FIELD CATEGORIES SIMULTANEOUSLY — Dispute the balance "
    "AND the account status AND the payment history AND the dates in the SAME letter as "
    "separately labeled dispute points. Each category maps to a different e-OSCAR code. "
    "When a CRA employee must choose between Code 002 (balance), Code 103 (status), "
    "Code 023 (account info), and Code 105 (payment history) for the same dispute, they "
    "cannot select one — they must escalate or process each separately, which exceeds "
    "the 4-minute window.\n"
    "3. PRE-EMPTIVE METHOD OF VERIFICATION REQUEST — Include this in EVERY letter: "
    "'Pursuant to 15 U.S.C. § 1681i(a)(7), I hereby request that upon completion of "
    "your investigation, you provide me with a description of the procedure used to "
    "determine the accuracy of the disputed information, including the name, address, "
    "and telephone number of each furnisher contacted. You have 15 days to comply.' "
    "This creates a SEPARATE legal obligation and exposes e-OSCAR rubber-stamping — if "
    "the CRA's 'method of verification' was simply forwarding a code and accepting "
    "Code 01 back, that admission is evidence of an unreasonable investigation.\n"
    "4. CITE ENFORCEMENT ACTIONS BY NAME — Reference 'CFPB v. Experian (2025)' and "
    "'CFPB v. Equifax (2025)' to signal that the consumer is aware the CFPB has already "
    "found these exact practices unlawful. This is not a threat — it is a statement of "
    "fact that raises the legal stakes for the CRA employee processing the dispute.\n"
    "5. ALLEGE SPECIFIC FACTUAL ERRORS WITH CONCRETE DATA — Never say 'verify the "
    "balance.' Say 'The Current Balance field reports $X, which is inaccurate because "
    "[reason — account was paid, settled, or closed on DATE].' Never say 'verify the "
    "date.' Say 'The Date of First Delinquency field shows MM/YYYY, which is incorrect "
    "because [reason].' Generic verification requests are trivially coded 103. Specific "
    "factual allegations with dollar amounts and dates require field-by-field review.\n"
    "6. SET A TRAP FOR DNR — For collection and charged-off accounts, demand that the "
    "CRA direct the data furnisher to produce: (1) the signed original credit agreement, "
    "(2) a complete payment ledger showing every payment received, (3) the chain of "
    "assignment if the debt was sold. Do NOT demand the CRA itself produce these — they "
    "don't have them. If the furnisher fails to respond within 30 days, deletion is "
    "mandatory per 15 U.S.C. § 1681i(a)(5)(A).\n"
    "7. DO NOT use generic language like 'I believe this account is not mine' or "
    "'please investigate' — these are trivially reduced to e-OSCAR Code 001 or 103.\n"
    "8. EXPLOIT CROSS-BUREAU DISCREPANCIES — If the account reports differently across "
    "bureaus (different balances, DOFD, status), cite this as independent evidence of "
    "inaccuracy under 15 U.S.C. § 1681s-2(a)(1)(A). At least one bureau's data must be "
    "wrong.\n"
    "9. KEEP LETTERS UNDER 650 WORDS — Excessive length and citation density can trigger "
    "a frivolous determination under § 1681i(a)(3). Be precise, not verbose.\n"
    "10. INCLUDE ANTI-COMPRESSION WARNING — Add this statement to every letter: 'Any "
    "attempt to compress this multi-field dispute into a single e-OSCAR ACDV code will "
    "constitute willful noncompliance under 15 U.S.C. § 1681n.' This makes it legally "
    "dangerous for the clerk to reduce the dispute to one code.\n"
    "11. INSTRUCT CONSUMER TO ENCLOSE ID — Direct the consumer to attach copies of "
    "their driver's license (front & back) and a recent utility bill or bank statement. "
    "Reference these as 'Enclosures' in the letter. This pre-empts any § 1681i(a)(3) "
    "frivolous determination and forces the CRA to forward documentation.\n\n"
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
    "You are uDispute, a professional credit dispute letter generator. "
    "You write precise, legally grounded dispute letters backed by automated "
    "credit report analysis.\n\n"
    + _E_OSCAR_INTELLIGENCE
    + "PARSER-DETECTED INACCURACIES: The consumer's credit report has been automatically "
    "analyzed and specific reporting inaccuracies have been identified with their "
    "corresponding FCRA violations. You MUST incorporate these specific findings into "
    "the letter — cite the exact inaccuracies, the specific FCRA sections violated, "
    "and demand field-by-field verification of each one. These detected inaccuracies "
    "are your PRIMARY ammunition — they prove the furnisher is reporting data that "
    "contradicts itself or is incomplete, which means a Response Code 01 ('verified') "
    "would itself be a violation.\n\n"
    "CRITICAL: Each parser-detected inaccuracy represents a DEMONSTRABLE factual error — "
    "not merely 'unverified' data. When citing each inaccuracy:\n"
    "- Reference the Metro 2 field name (e.g., 'Current Balance field,' 'Account Status field')\n"
    "- State the specific REPORTED value from the report\n"
    "- Explain WHY that value is factually wrong\n"
    "- Example: 'The Current Balance field reports $4,231 on an account where the Account "
    "Status field shows Closed/Paid — a closed account must report $0.'\n"
    "Structure these as labeled DISPUTE POINTS, each targeting a different Metro 2 field "
    "category so no single e-OSCAR code can capture the entire dispute.\n\n"
    + _EDUCATIONAL_NOTE
)

SYSTEM_PROMPT_WITH_LEGAL_RESEARCH = (
    "You are uDispute, a professional credit dispute letter generator backed by "
    "real legal research — CFPB complaint data and federal case law.\n\n"
    + _E_OSCAR_INTELLIGENCE
    + "MULTI-LAYER EVIDENCE PACKAGE: The consumer's dispute is backed by three layers:\n"
    "1. PARSER-DETECTED INACCURACIES — Specific data contradictions found in the report. "
    "Cite each one with the exact FCRA section violated.\n"
    "2. CFPB COMPLAINT DATA — Real complaint data showing a PATTERN of similar issues "
    "against this creditor. This demonstrates systemic non-compliance, not an isolated error. "
    "Reference the complaint count and common themes.\n"
    "3. FEDERAL CASE LAW — Court decisions where consumers prevailed on similar claims. "
    "Cite case names and holdings naturally within the letter to establish precedent.\n\n"
    "The CFPB data and case law citations are provided in the prompt — use them naturally. "
    "Do NOT fabricate case names or statistics. Only cite what is provided.\n\n"
    "This three-layer approach makes it legally dangerous for the CRA to simply forward "
    "an e-OSCAR code. They know the consumer has evidence, pattern data, and case law — "
    "a rubber stamp Response Code 01 creates clear § 1681i liability.\n\n"
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
