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
        f"If you cannot verify the accuracy of this information, it must be deleted from "
        f"my credit file per 15 U.S.C. § 1681i(a)(5)(A)."
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
        "Write a letter {action} for {entity} regarding {issue}. The account is {account_name} with account number {account_number} and has the following account status: {marks}",
        "I need a letter {action} for {entity} about an issue regarding {issue}. The account is {account_name} with account number {account_number} and has the following account status: {marks}"
    ],
    "arbitration": [
        "Draft an arbitration demand under 15 U.S.C. 1681e(b) to {entity}, account {account_number}, stating {issue}. I will {action}.",
        "Compose a formal arbitration request for {entity}, referencing {issue}, account {account_number}, and demanding {action}."
    ],
    "consumer_law": [
        "Write a letter to {entity} invoking the Fair Credit Billing Act regarding {issue} on account {account_number}. I request {action}.",
        "Craft a demand under the Fair Debt Collection Practices Act for {entity}, account {account_number}, disputing {issue} and seeking {action}."
    ],
    "ACDV_response": [
        "Compose a formal demand letter {action} to {entity} about the disputed credit file. File/Account No: {account_number}, Dispute Date: {dispute_date}. Invoke *Cushman v. Trans Union Corp.*, 115 F.3d 220 (3d Cir. 1997), and demand immediate production of the full Automated Consumer Dispute Verification (ACDV) record, including **Method of Verification**, **Submission Procedure**, and **FCRA Compliance Policies**, with delivery required within {days} business days or outline available FCRA remedies.",
        "Compose a formal demand letter {action} to {entity} regarding flawed reinvestigation procedures. File/Account No: {account_number}, Dispute Date: {dispute_date}. Invoke *Giliespie v. Equifax Info. Servs.*, 484 F.3d 938, which held CRAs liable for unreasonable investigation processes, and demand documented proof of each step of your reinvestigation protocol—including source contact logs, verification methodologies, and internal quality-control policies—with delivery required within {days} business days or outline available FCRA remedies for failure to comply.",
        "Write a formal demand letter to {entity} demanding immediate production of the full Automated Consumer Dispute Verification (ACDV) record related to the {account_name} and {account_number} and previous {dispute_date}."
    ]
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
    {"key": "ACDV_response", "name": "ACDV Enforcement Pack", "description": "Demand the full ACDV record — method of verification, submission procedure, and FCRA compliance docs. Make them prove it."}
]


SYSTEM_PROMPT_BASE = (
    "You are uDispute, a bot that creates credit dispute letters. "
    "Use your knowledge of UCC, CFPB regulations, and USC to write compelling "
    "letters that address inaccuracies and potential infringements by creditors."
)

SYSTEM_PROMPT_WITH_INACCURACIES = (
    "You are uDispute, a bot that creates credit dispute letters. "
    "Use your knowledge of UCC, CFPB regulations, and USC to write compelling "
    "letters that address inaccuracies and potential infringements by creditors.\n\n"
    "IMPORTANT: The consumer's credit report has been automatically analyzed and "
    "specific reporting inaccuracies have been identified with their corresponding "
    "FCRA violations. You MUST incorporate these specific findings into the letter — "
    "cite the exact inaccuracies, the specific FCRA sections violated, and demand "
    "investigation/correction of each one. This is what makes each letter unique "
    "to the consumer's situation.\n\n"
    "EDUCATIONAL NOTE: Write the letter in a way that helps the consumer understand "
    "WHY each inaccuracy is a violation and WHAT their rights are. This is not just "
    "a legal document — it's a learning tool that empowers the consumer to understand "
    "the credit reporting system."
)

SYSTEM_PROMPT_WITH_LEGAL_RESEARCH = (
    "You are uDispute, a bot that creates credit dispute letters backed by real "
    "legal research. Use your knowledge of UCC, CFPB regulations, and USC to write "
    "compelling letters that address inaccuracies and potential infringements by creditors.\n\n"
    "IMPORTANT: The consumer's credit report has been automatically analyzed and "
    "specific reporting inaccuracies have been identified. Additionally, CFPB complaint "
    "data and relevant federal case law have been researched for this specific creditor "
    "and violation type. You MUST incorporate ALL of this into the letter:\n"
    "1. Cite the exact inaccuracies found in the report\n"
    "2. Reference the specific FCRA sections violated\n"
    "3. Cite the CFPB complaint data showing a PATTERN of similar complaints against "
    "this creditor (this demonstrates the issue is systemic, not an isolated error)\n"
    "4. Cite relevant court cases where consumers prevailed on similar claims\n"
    "5. Demand investigation/correction with a clear deadline\n\n"
    "The CFPB data and case law citations are provided in the prompt — use them "
    "naturally within the letter. Do NOT fabricate case names or statistics. Only cite "
    "what is provided.\n\n"
    "EDUCATIONAL NOTE: Write the letter in a way that helps the consumer understand "
    "WHY each inaccuracy is a violation, WHAT their rights are, and HOW other consumers "
    "have successfully challenged similar issues. This is not just a legal document — "
    "it's a learning tool that empowers the consumer."
)


SYSTEM_PROMPT_NOTICE_OF_DISPUTE = (
    "You are uDispute, a bot that creates credit dispute letters. "
    "You are generating a NOTICE OF DISPUTE — this is a formal notification letter "
    "sent to a credit bureau informing them that the consumer is disputing specific "
    "accounts on their credit report.\n\n"
    "IMPORTANT RULES FOR THIS LETTER TYPE:\n"
    "1. List every disputed account by name, account number, and account type in a "
    "clear, numbered format\n"
    "2. State that the consumer formally disputes the accuracy of these accounts "
    "under 15 U.S.C. § 1681i (FCRA Right to Dispute)\n"
    "3. Demand investigation within 30 days per FCRA requirements\n"
    "4. Do NOT include specific inaccuracies, legal citations beyond § 1681i, or "
    "detailed arguments — those come in the follow-up letter\n"
    "5. Keep the tone professional and direct — this is a formal notice, not an argument\n"
    "6. Include a statement that failure to investigate within 30 days constitutes a "
    "violation of 15 U.S.C. § 1681i(a)(1)\n"
    "7. Request written confirmation of receipt and investigation results\n\n"
    "This letter puts the bureau on notice. The detailed inaccuracy dispute follows "
    "15-30 days later."
)


# ─── Notice of Dispute (Tier 1) ───

NOTICE_OF_DISPUTE_TEMPLATE = (
    "Generate a formal Notice of Dispute letter to {bureau_name}.\n\n"
    "The following accounts are being formally disputed:\n\n"
    "{account_table}\n\n"
    "This is a Round 1 notice under the Fair Credit Reporting Act, "
    "15 U.S.C. § 1681i. Demand a 30-day investigation of each account listed."
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


def generate_letter(prompt, model="gpt-4o", has_inaccuracies=False,
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
