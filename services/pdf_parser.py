"""
PDF parsing and negative item extraction service.
Extracted from dispute_ui.py — all credit report PDF processing lives here.
"""

import re
import json
import hashlib
import base64
import pdfplumber
import fitz
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

openai_client = OpenAI()


def compute_pdf_hash(file_path):
    """Compute SHA-256 hash of a PDF file for deduplication."""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while True:
            data = f.read(8192)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()


def _classify_page(text):
    """Classify a PDF page as account data, inquiry, legal, or other."""
    low = text[:500].lower()
    if any(kw in low for kw in [
        'summary of rights', 'fair credit reporting', 'fraud victim',
        'fcra', 'identity theft', 'security freeze', 'consumer financial protection'
    ]):
        return 'legal'
    if any(kw in low for kw in [
        'regular inquiries', 'promotional inquiries', 'account review inquiries',
        'requested on', 'inquiry type'
    ]) and 'account name' not in low and 'pay status' not in low:
        return 'inquiry'
    if any(kw in low for kw in [
        'supplemental consumer', 'chex systems', 'teletrack',
        'checking account and demand', 'dda inquiries', 'nsfs in the last'
    ]):
        return 'supplemental'
    if any(kw in low for kw in [
        'account name', 'account info', 'pay status', 'payment history',
        'balance', 'charge-off', 'collection', 'charge off', 'past due',
        'date opened', 'account type', 'loan type', 'high balance',
        'credit limit', 'monthly payment', 'adverse'
    ]):
        return 'account'
    return 'other'


def pdf_to_base64_images(pdf_path, max_pages=5, smart_filter=False):
    """Convert PDF pages to base64-encoded PNG images for vision analysis.

    If smart_filter=True, uses text classification to skip non-account pages
    (inquiries, legal disclaimers, supplemental data) and focuses on pages
    containing actual account information.
    """
    images = []
    try:
        doc = fitz.open(pdf_path)

        if smart_filter:
            # First pass: classify pages using text extraction
            import pdfplumber
            account_pages = []
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ''
                    page_type = _classify_page(text)
                    if page_type == 'account':
                        account_pages.append(i)

            # If classification found account pages, use those; otherwise fall back to first N
            if account_pages:
                pages_to_render = account_pages[:max_pages]
            else:
                pages_to_render = list(range(min(max_pages, len(doc))))
        else:
            pages_to_render = list(range(min(max_pages, len(doc))))

        for i in pages_to_render:
            if i >= len(doc):
                continue
            page = doc[i]
            pix = page.get_pixmap(dpi=150)
            image_bytes = pix.tobytes("png")
            b64_image = base64.b64encode(image_bytes).decode("utf-8")
            images.append(f"data:image/png;base64,{b64_image}")
    except Exception as e:
        raise ValueError(f"Failed to open PDF: {e}")
    return images


def detect_bureau(full_text):
    """Detect which credit bureau generated this report from header text."""
    header = full_text[:1000].lower()
    if 'experian' in header:
        return 'experian'
    elif 'transunion' in header:
        return 'transunion'
    elif 'equifax' in header:
        return 'equifax'
    return 'unknown'


def vision_filter_accounts(negative_items, file_path, max_pages=5):
    """Use GPT-4o Vision to validate which accounts are truly negative."""
    images = pdf_to_base64_images(file_path, max_pages=max_pages)

    accounts_summary = [
        {
            "account_name": acct["account_name"],
            "account_number": acct["account_number"],
            "status": acct["status"],
            "payment_history": acct.get("raw_payment_lines", [])
        }
        for acct in negative_items
    ]

    vision_prompt = f"""
We have extracted these accounts from a credit report PDF:

{json.dumps(accounts_summary, indent=2)}

IMPORTANT DEFINITIONS:
- A "late bucket" is any entry in the payment-history grid showing:
    • "30" (30 days past due)
    • "60" (60 days past due)
    • "90" (90 days past due)
    • "120" (120 days past due)
    • the words "Charge-off" (or "CO" when used to mean charge-off)
    • "C" (collection)

- A "clean" history line is one showing only:
    • a check-mark ✓
    • a dash "–"

- "CLS" means "closed in good standing" and is normally positive but IF you see any late bucket (30/60/90/120/CO/C) in the same grid, YOU MUST treat that whole account as negative.

TASK:
For each account above, look at both:
  1. Its status text (e.g. "Paid, Closed/Never Late", "Current", "Collection Account")
  2. Its payment-history grid (using the definitions above)

Mark an account as "skip" ONLY IF:
  • The status is positive (e.g. "Paid", "Never Late", "Closed", "Current"),
  • and its payment history grid shows **only** clean buckets (✓, or –),
  • and you see no late buckets (30, 60, 90, 120, CO, C).

Otherwise mark it as "keep".

RETURN ONLY valid JSON in this format:

[
  {{ "account_number": "12345", "action": "keep" }},
  {{ "account_number": "67890", "action": "skip" }},
  ...
]
"""

    vision_inputs = (
        [{"type": "image_url", "image_url": {"url": img, "detail": "high"}} for img in images]
        + [{"type": "text", "text": vision_prompt}]
    )
    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": vision_inputs}],
        temperature=0
    )

    raw = resp.choices[0].message.content or ""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    m = re.search(r"\[\s*\{.*\}\s*\]", raw, re.S)
    if not m:
        m = re.match(r"\[.*\]$", raw, re.S)
    if not m:
        return negative_items

    json_str = m.group(0)
    try:
        decisions = json.loads(json_str)
    except json.JSONDecodeError:
        return negative_items

    keep_set = {d["account_number"] for d in decisions if d.get("action") == "keep"}
    return [acct for acct in negative_items if acct["account_number"] in keep_set]


def _parse_experian(full_text):
    """Parse Experian credit report format using regex."""
    blocks = full_text.split("Account name")
    negative_items = []

    # Match late-payment indicators in the payment grid.
    # \bC\b matches standalone "C" (collection marker) but NOT "CLS", "CO", "Current".
    # CO (charge-off) is matched separately.
    grid_regex = re.compile(r'\b(?:30|60|90|120|150|180)\b|(?<!\w)CO(?!\w)|(?<!\w)C(?!\w)(?!LS)(?!urrent)(?!losed)(?!ontact)')
    # Lines that signal end of payment history grid
    ph_stop_regex = re.compile(
        r'contact\s+info|address\s+\S|phone\s+number|'
        r'https?://|\.com/|prepared\s+for',
        re.IGNORECASE
    )
    # Lines that are valid payment grid content (years, month headers, legends)
    ph_grid_line_regex = re.compile(
        r'^\d{4}\b|^Jan\b|^Month\b|Current|Terms\s+met|Past\s+due|'
        r'Charge\s+off|On\s+Time|Days?\s+Late|CLS|ND\b|Data\s+Unavailable|'
        r'No\s+data|Repossession|Collection\b|^\s*[-\s\ue902]+$',
        re.IGNORECASE
    )
    clean_regex = re.compile(
        r'\b(?:open|current|(?:pays|paid)(?:\s+\w+)*\s+as\s+agreed|closed|never\s+late|'
        r'exceptional\s+payment\s+history)\b',
        re.IGNORECASE
    )
    status_regex = re.compile(
        r'\b(charged\s+off|charge-off|repossession|collection(?:\s+account)?|'
        r'past\s+due|delinquent|settlement|written\s+off)\b',
        re.IGNORECASE
    )

    seen_accounts = set()  # Track (account_name, account_number) to deduplicate

    for block in blocks[1:]:
        lines = block.strip().splitlines()
        data = {
            "account_name": None,
            "account_number": None,
            "account_type": None,
            "balance": None,
            "status": None,
            "issue": None,
            "comments": None,
            "date_opened": None
        }

        first = lines[0].strip()
        m = re.match(r"(.+?)\s+Balance", first)
        data["account_name"] = m.group(1).strip() if m else first

        payment_history = []
        in_ph = False
        in_comments = False
        comments_lines = []
        for line in lines:
            low = line.lower()

            if "payment history" in low and "self-reported" not in low:
                in_ph = True
                in_comments = False
                continue
            if in_ph:
                stripped = line.strip()
                # Hard stop at section boundaries (Contact info, URLs, etc.)
                if not stripped or ph_stop_regex.search(stripped):
                    in_ph = False
                    continue
                # Keep lines that look like grid content (year rows, legends)
                if ph_grid_line_regex.search(stripped):
                    payment_history.append(stripped)
                continue

            # Collect comments section (may have unicode icon prefix like \ue972)
            clean_low = re.sub(r'[\ue000-\uf8ff]', '', low).strip()
            if clean_low == 'comments':
                in_comments = True
                continue
            if in_comments:
                stripped = line.strip()
                if not stripped or ph_stop_regex.search(stripped):
                    in_comments = False
                    continue
                comments_lines.append(stripped)
                continue

            if "account number" in low and not data["account_number"]:
                mm = re.search(r"account number[:\s-]*(\S+)", line, re.I)
                if mm:
                    data["account_number"] = mm.group(1).strip()

            if "original creditor" in low and "_orig_cred_seen" not in data:
                data["_orig_cred_seen"] = True  # Mark field as seen even if value is '-'
                mm = re.search(r"original creditor[:\s]*(.+)", line, re.I)
                if mm:
                    val = mm.group(1).strip()
                    # Clean trailing two-column text (e.g., "CREDITOR NAME Paid off 0%")
                    val = re.sub(r'\s+(Paid off|Credit limit|Original balance|Monthly payment|Credit usage|Balance updated).*', '', val, flags=re.I).strip()
                    if val and val != '-':
                        data["original_creditor"] = val

            if "account type" in low and not data["account_type"]:
                mm = re.search(r"account type[:\s]*(.+)", line, re.I)
                if mm:
                    data["account_type"] = mm.group(1).strip()

            if "balance" in low and not data["balance"]:
                mm = re.search(r"balance[:\s-]*\$?([\d,]+)", line, re.I)
                if mm:
                    data["balance"] = f"${mm.group(1).strip()}"

            if "date opened" in low and not data["date_opened"]:
                mm = re.search(r"date opened[:\s]*(\S+(?:\s+\S+)?)", line, re.I)
                if mm:
                    data["date_opened"] = mm.group(1).strip()

            if "status" in low and not data["status"]:
                mm = re.search(r"status[:\s]*(.+?)(?:\.|$)", line, re.I)
                if mm:
                    data["status"] = mm.group(1).strip()

        if comments_lines:
            data["comments"] = " ".join(comments_lines)

        status_text = (data["status"] or "").strip()
        grid_text = " ".join(payment_history)

        if clean_regex.search(status_text) and not grid_regex.search(grid_text):
            continue

        grid_issue = bool(grid_regex.search(grid_text))
        status_issue = bool(status_regex.search(status_text))
        acct_issue = "collection" in (data["account_type"] or "").lower()
        # Account with an original creditor listed is a collection/debt buyer
        orig_creditor_issue = bool(data.get("original_creditor"))
        comments_text = (data.get("comments") or "").upper()
        comments_issue = bool(re.search(
            r'COLLECTION\s+ACCOUNT|PLACED\s+FOR\s+COLLECTION|CHARGED?\s+OFF\s+ACCOUNT|'
            r'PROFIT\s+AND\s+LOSS|REPOSSESSION|SERIOUSLY\s+PAST\s+DUE',
            comments_text
        ))

        if not (grid_issue or status_issue or acct_issue or orig_creditor_issue or comments_issue):
            continue

        if grid_issue:
            data["issue"] = "Late payments / Charge-off in payment history"
        elif status_issue:
            data["issue"] = status_text
        elif comments_issue:
            # Determine specific issue from comments
            if "COLLECTION" in comments_text:
                data["issue"] = "Collection account (from comments)"
            elif "CHARGE" in comments_text:
                data["issue"] = "Charged off account (from comments)"
            else:
                data["issue"] = "Negative account (from comments)"
        elif orig_creditor_issue:
            data["issue"] = f"Collection/debt buyer (original creditor: {data['original_creditor']})"
        else:
            data["issue"] = "Collection account"

        # Deduplicate: use name + number + date_opened to distinguish
        # different loans from the same creditor with same masked number
        acct_name_upper = (data["account_name"] or "").strip().upper()
        acct_num_upper = (data["account_number"] or "").strip().upper()
        acct_date = (data["date_opened"] or "").strip()
        acct_key = (acct_name_upper, acct_num_upper, acct_date)
        base_key = (acct_name_upper, acct_num_upper)

        if acct_key in seen_accounts:
            continue
        seen_accounts.add(acct_key)

        # Check if another account with same name+number but different date
        # already exists — if so, add disambiguation notes to both
        same_base = [item for item in negative_items
                     if (item["account_name"] or "").strip().upper() == acct_name_upper
                     and (item["account_number"] or "").strip().upper() == acct_num_upper]
        if same_base:
            data["disambiguation_note"] = (
                f"Multiple {data['account_name']} accounts detected with same masked number. "
                f"This account was opened {acct_date or 'unknown date'}."
            )
            for prev in same_base:
                if not prev.get("disambiguation_note"):
                    prev["disambiguation_note"] = (
                        f"Multiple {prev['account_name']} accounts detected with same masked number. "
                        f"This account was opened {prev.get('date_opened') or 'unknown date'}."
                    )

        data.pop("_orig_cred_seen", None)  # Remove internal tracking flag
        data["raw_payment_lines"] = payment_history
        negative_items.append(data)

    # Post-process: detect inaccuracies on each negative account
    for item in negative_items:
        item["inaccuracies"] = _detect_inaccuracies(item)

    return negative_items


def _detect_inaccuracies(account):
    """Cross-reference fields within a negative account to find reportable inaccuracies."""
    inaccuracies = []
    status_text = (account.get("status") or "").lower()
    account_type = (account.get("account_type") or "").lower()
    comments_text = (account.get("comments") or "").upper()
    balance = account.get("balance") or ""
    grid_lines = account.get("raw_payment_lines") or []
    grid_text = " ".join(grid_lines).upper()
    original_creditor = account.get("original_creditor") or ""

    # 1. Status says positive but payment history shows lates
    positive_status = bool(re.search(
        r'pays?\s+(?:account\s+)?as\s+agreed|current|open|never\s+late|paid\s+or\s+paying',
        status_text
    ))
    has_lates = bool(re.search(r'\b(?:30|60|90|120|150|180)\b|(?<!\w)CO(?!\w)', grid_text))
    if positive_status and has_lates:
        inaccuracies.append(
            "Status reports account as current/paying as agreed, but payment history "
            "shows late payments — status should reflect the delinquency history"
        )

    # 2. Account type says "Open Account" but comments indicate collection
    if "open account" in account_type and (
        "COLLECTION" in comments_text or "PLACED FOR COLLECTION" in comments_text
    ):
        inaccuracies.append(
            "Account type listed as 'Open Account' but comments indicate this is a "
            "collection account — account type is inaccurately reported"
        )

    # 3. Original creditor present but account type doesn't say collection
    if original_creditor and original_creditor != '-':
        clean_orig = re.sub(r'\s*(Paid off|Credit limit|Monthly payment).*', '', original_creditor, flags=re.I).strip()
        if clean_orig and clean_orig != '-' and "collection" not in account_type:
            inaccuracies.append(
                f"Account has original creditor '{clean_orig}' listed (indicating debt was sold) "
                f"but account type does not reflect this as a collection"
            )

    # 4. Charge-off with balance that matches credit limit exactly (possible failure to update)
    if "charge" in status_text or "CO" in grid_text:
        # Extract credit limit from account_type trailing text
        limit_match = re.search(r'credit\s+limit\s*\$?([\d,]+)', account_type, re.I)
        if limit_match and balance:
            limit_val = limit_match.group(1).replace(',', '')
            balance_val = balance.replace('$', '').replace(',', '')
            try:
                if int(balance_val) > int(limit_val) and int(limit_val) > 0:
                    inaccuracies.append(
                        f"Balance (${balance_val}) exceeds credit limit (${limit_val}) — "
                        f"balance may include unauthorized fees or interest added after charge-off"
                    )
            except ValueError:
                pass

    # 5. Account shows CO (charge-off) in grid but status doesn't mention charge-off
    if re.search(r'(?<!\w)CO(?!\w)', grid_text) and "charge" not in status_text and "charged" not in status_text:
        if "collection" not in status_text and "past due" not in status_text:
            inaccuracies.append(
                "Payment history shows charge-off (CO) entries but status text does not "
                "reflect this — status is inconsistent with payment history"
            )

    # 6. Closed account still showing a balance (potential reporting error)
    if "closed" in status_text or "closed" in comments_text.lower():
        if balance and balance not in ("$0", "$0.00", ""):
            balance_val = balance.replace('$', '').replace(',', '')
            try:
                if int(balance_val) > 0 and "charge" not in status_text:
                    inaccuracies.append(
                        f"Account is reported as closed but still shows balance of {balance} — "
                        f"closed accounts paid in full should report $0 balance"
                    )
            except ValueError:
                pass

    # 7. Double reporting check: collection has original creditor that matches
    #    another account name in the same report (handled at report level, not here)

    # 8. Missing due date (only check if parser actually extracts this field)
    payment_due_date = account.get("payment_due_date")
    if payment_due_date is not None and not payment_due_date.strip():
        # Field was extracted but is empty — genuinely missing from report
        if balance and balance not in ("$0", "$0.00", ""):
            balance_val = balance.replace('$', '').replace(',', '')
            try:
                if int(balance_val) > 0 and "closed" not in status_text:
                    inaccuracies.append(
                        "Account is missing a due date — this is a required data field for "
                        "accurate credit reporting"
                    )
            except ValueError:
                pass

    # 9. Missing monthly payment amount (only check if parser actually extracts this field)
    monthly_payment = account.get("monthly_payment", account.get("scheduled_payment"))
    if monthly_payment is not None and not str(monthly_payment).strip():
        # Field was extracted but is empty — genuinely missing from report
        if balance and balance not in ("$0", "$0.00", ""):
            balance_val = balance.replace('$', '').replace(',', '')
            try:
                if int(balance_val) > 0 and "closed" not in status_text and "collection" not in account_type:
                    inaccuracies.append(
                        "Account is missing the scheduled monthly payment amount — essential "
                        "for accurate debt-to-income calculations"
                    )
            except ValueError:
                pass

    return inaccuracies


def _is_transunion_native(full_text):
    """Check if this is a TransUnion native format (direct or annual credit report)."""
    header = full_text[:5000]
    # TransUnion native reports have these distinctive markers
    if 'transunion.com/dispute' in header.lower():
        return True
    if 'annualcreditreport.transunion.com' in header.lower():
        return True
    if 'Personal Credit Report for:' in header and 'File Number:' in header:
        return True
    # TransUnion uses "Pay Status" and "Loan Type" fields (Experian uses "Status" and "Account type")
    if 'Pay Status' in header and 'Loan Type' in header:
        return True
    return False


def _parse_transunion_native(full_text):
    """Parse TransUnion native credit report format using regex.

    Handles both transunion.com/dispute and annualcreditreport.transunion.com formats.
    These use 'ACCOUNT_NAME NUMBER****' blocks with 'Pay Status', 'Loan Type',
    and Rating-based payment history (OK, 30, 60, 90, 120, C/O, RPO, COL, N/R, X).
    """
    # Split on account header pattern: NAME followed by masked number
    # Account numbers can start with letters (e.g., RD122****) or digits
    blocks = re.split(r'\n(?=[A-Z][A-Z &/\.\'\-]{2,}\s+[\dA-Z][\d\w\*]+\*{2,})', full_text)
    negative_items = []

    # Rating codes that indicate a negative mark
    late_regex = re.compile(r'\b(30|60|90|120|C/O|RPO|COL|VS|FC)\b')

    # Pay Status patterns that indicate adverse history
    adverse_status_regex = re.compile(
        r'charge[\s-]?off|collection|repossession|past\s+due|'
        r'was\s+\d+\s+days?\s+past\s+due|was\s+a\s+charge|'
        r'settled|written\s+off|profit\s+and\s+loss',
        re.IGNORECASE
    )

    # Loan types that indicate collection
    collection_loan_types = {'collection agency/attorney', 'factoring company account'}

    seen_accounts = set()

    for block in blocks[1:]:
        lines = block.strip().split('\n')
        if not lines:
            continue

        # First line: "ACCOUNT_NAME NUMBER****"
        first = lines[0].strip()
        name_match = re.match(r'(.+?)\s+([\dA-Z][\d\w\*]+\*{2,})', first)
        if not name_match:
            continue

        data = {
            "account_name": name_match.group(1).strip(),
            "account_number": name_match.group(2).strip(),
            "account_type": None,
            "balance": None,
            "status": None,
            "issue": None,
            "date_opened": None,
            "original_creditor": None,
            "comments": None,
        }

        # Extract fields from the block
        rating_lines = []
        remarks_parts = []

        for line in lines[1:]:
            stripped = line.strip()

            # Pay Status (may have >brackets< around adverse items)
            if 'Pay Status' in stripped and not data["status"]:
                val = re.sub(r'Pay Status\s*', '', stripped).strip()
                val = val.replace('>', '').replace('<', '')
                data["status"] = val

            # Loan Type
            elif 'Loan Type' in stripped and not data["account_type"]:
                val = re.sub(r'Loan Type\s*', '', stripped).strip()
                data["account_type"] = val

            # Balance
            elif stripped.startswith('Balance') and 'Balance (Hist' not in stripped and not data["balance"]:
                mm = re.search(r'\$[\d,]+', stripped)
                if mm:
                    data["balance"] = mm.group(0)

            # Date Opened
            elif 'Date Opened' in stripped and not data["date_opened"]:
                mm = re.search(r'Date Opened\s+(\d{2}/\d{2}/\d{4})', stripped)
                if mm:
                    data["date_opened"] = mm.group(1)

            # Original Creditor
            elif 'Original Creditor' in stripped and not data.get("original_creditor"):
                val = re.sub(r'Original Creditor\s*', '', stripped).strip()
                if val and val != '-':
                    data["original_creditor"] = val

            # Remarks
            elif 'Remarks' in stripped:
                val = re.sub(r'Remarks\s*', '', stripped).strip()
                if val and val != '- - -':
                    remarks_parts.append(val)

            # Estimated removal date (indicates adverse item)
            elif 'Estimated month and year this item will be removed' in stripped:
                data["_has_removal_date"] = True

            # Rating lines: look for rows that have Rating codes
            elif re.match(r'^(OK|30|60|90|120|C/O|RPO|COL|N/R|X|VS|FC)(\s+(OK|30|60|90|120|C/O|RPO|COL|N/R|X|VS|FC))*\s*$', stripped):
                rating_lines.append(stripped)

        if remarks_parts:
            data["comments"] = ' '.join(remarks_parts)

        # Determine if this account is negative
        status_text = (data["status"] or "").strip()
        ratings_text = " ".join(rating_lines)
        loan_type = (data["account_type"] or "").lower()
        comments = (data.get("comments") or "").upper()

        has_late_ratings = bool(late_regex.search(ratings_text))
        has_adverse_status = bool(adverse_status_regex.search(status_text))
        is_collection_type = loan_type in collection_loan_types
        has_collection_remarks = bool(re.search(
            r'PLACED\s+FOR\s+COLLECTION|COLLECTION|CHARGE\s+OFF|REPOSSESSION|PROFIT\s+AND\s+LOSS',
            comments
        ))

        # Original creditor is a negative signal UNLESS the account is clearly positive
        # (e.g., "Paid as agreed" with no lates — original creditor is a lending partner, not a debt buyer)
        has_original_creditor = bool(data.get("original_creditor"))
        if has_original_creditor and not has_late_ratings and not has_adverse_status:
            clean_status = bool(re.search(
                r'paid\s+as\s+agreed|paying\s+as\s+agreed|current\s+account|paid,?\s+closed',
                status_text, re.IGNORECASE
            ))
            if clean_status:
                has_original_creditor = False  # Don't flag clean accounts just for having an original creditor
        has_removal_date = data.pop("_has_removal_date", False)
        # Removal date alone isn't sufficient if account is current with all OK ratings
        if has_removal_date and not has_late_ratings and not has_adverse_status:
            has_removal_date = False

        if not (has_late_ratings or has_adverse_status or is_collection_type or
                has_original_creditor or has_collection_remarks or has_removal_date):
            continue

        # Determine issue
        if has_adverse_status:
            data["issue"] = status_text
        elif has_late_ratings:
            # Extract specific late months from rating lines
            late_codes = late_regex.findall(ratings_text)
            data["issue"] = f"Late payments in payment history ({', '.join(late_codes)})"
        elif is_collection_type:
            data["issue"] = f"Collection account (Loan Type: {data['account_type']})"
        elif has_collection_remarks:
            data["issue"] = f"Collection/adverse (from remarks: {data.get('comments', '')[:60]})"
        elif has_original_creditor:
            data["issue"] = f"Collection/debt buyer (original creditor: {data['original_creditor']})"
        elif has_removal_date:
            data["issue"] = "Account has estimated removal date (indicates adverse history)"
        else:
            data["issue"] = "Negative account"

        # Dedup using name + number + date_opened
        acct_name_upper = (data["account_name"] or "").strip().upper()
        acct_num_upper = (data["account_number"] or "").strip().upper()
        acct_date = (data["date_opened"] or "").strip()
        acct_key = (acct_name_upper, acct_num_upper, acct_date)

        if acct_key in seen_accounts:
            continue
        seen_accounts.add(acct_key)

        data["raw_payment_lines"] = rating_lines
        negative_items.append(data)

    # Post-process: detect inaccuracies
    for item in negative_items:
        item["inaccuracies"] = _detect_tu_inaccuracies(item)

    return negative_items


def _detect_tu_inaccuracies(account):
    """Detect reporting inaccuracies in TransUnion native format accounts."""
    inaccuracies = []
    status_text = (account.get("status") or "").lower()
    loan_type = (account.get("account_type") or "").lower()
    comments = (account.get("comments") or "").upper()
    balance = account.get("balance") or ""
    ratings_text = " ".join(account.get("raw_payment_lines") or [])
    original_creditor = account.get("original_creditor") or ""

    late_regex = re.compile(r'\b(30|60|90|120|C/O|RPO|COL)\b')
    has_lates = bool(late_regex.search(ratings_text))

    # 1. Status says current/paying but history shows lates
    if any(kw in status_text for kw in ['current', 'paying as agreed', 'paid as agreed']):
        if has_lates:
            inaccuracies.append(
                "Pay Status reports account as current/paying as agreed, but payment history "
                "shows late payments — status should reflect the delinquency history"
            )

    # 2. Loan type is collection but account type doesn't reflect it
    if original_creditor and 'collection' not in loan_type and 'factoring' not in loan_type:
        inaccuracies.append(
            f"Account has original creditor '{original_creditor}' listed (indicating debt was sold) "
            f"but Loan Type does not reflect this as a collection"
        )

    # 3. Account type mismatch: Open Account but remarks say collection
    if 'open account' in loan_type and ('COLLECTION' in comments or 'PLACED FOR COLLECTION' in comments):
        inaccuracies.append(
            "Loan Type listed as 'Open Account' but remarks indicate this is a "
            "collection account — Loan Type is inaccurately reported"
        )

    # 4. Closed account with balance
    if 'closed' in status_text or 'paid' in status_text:
        if balance and balance not in ("$0", "$0.00", ""):
            bal_val = balance.replace('$', '').replace(',', '')
            try:
                if int(bal_val) > 0 and 'charge' not in status_text and 'settlement' not in status_text:
                    inaccuracies.append(
                        f"Account is reported as closed/paid but still shows balance of {balance}"
                    )
            except ValueError:
                pass

    # 5. Charge-off in ratings but not in status
    if re.search(r'\bC/O\b', ratings_text) and 'charge' not in status_text:
        if 'collection' not in status_text:
            inaccuracies.append(
                "Payment history shows charge-off (C/O) entries but Pay Status does not "
                "reflect this — status is inconsistent with payment history"
            )

    return inaccuracies


def _parse_with_vision_only(file_path):
    """Fallback parser: use GPT-4o Vision to extract accounts and detect inaccuracies."""
    images = pdf_to_base64_images(file_path, max_pages=30, smart_filter=True)

    prompt = """Analyze this credit report and extract ALL negative/derogatory accounts.

An account is negative if ANY of these are true:
- Status contains: charge-off, collection, repossession, past due, delinquent, settlement, written off
- Payment history shows ANY late payments: 30, 60, 90, 120 days late, C/O, RPO, COL
- Loan Type is "COLLECTION AGENCY/ATTORNEY" or "FACTORING COMPANY ACCOUNT"
- Pay Status shows anything adverse (even if account is now paid/closed)
- Remarks include: PLACED FOR COLLECTION, REPOSSESSION, PROFIT AND LOSS, CHARGE OFF
- Account has an Original Creditor listed (indicates debt was sold)

IMPORTANT: Include accounts that are currently paying as agreed but HAVE historical late payments in their payment history. A single 30-day late from years ago still counts.

For each negative account, also CHECK FOR INACCURACIES by cross-referencing:
1. Does the balance match the past due amount? (e.g., balance $0 but past due shows $500)
2. Does the status contradict the payment history? (e.g., status says "Paid as agreed" but history shows lates)
3. Is the same debt reported by both the original creditor AND a collection agency? (double reporting)
4. Does the account type match the loan type? (e.g., listed as "Open Account" but is clearly a collection)
5. Are there missing or inconsistent dates? (e.g., date closed before date opened)
6. Is there a balance on a closed/charged-off account that seems wrong?
7. Is the account showing as Individual when remarks suggest Joint, or vice versa?

For each negative account return:
- account_name: The creditor/company name
- account_number: The account number (masked is fine)
- account_type: Type of account
- balance: Current balance with $ sign
- status: Account status text
- issue: Why this account is negative
- original_creditor: If listed (null if not)
- inaccuracies: Array of strings describing any inaccuracies found. Empty array [] if none detected.

RETURN ONLY valid JSON array:
[
  {
    "account_name": "...",
    "account_number": "...",
    "account_type": "...",
    "balance": "$...",
    "status": "...",
    "issue": "...",
    "original_creditor": "..." or null,
    "inaccuracies": ["Description of inaccuracy 1", "Description of inaccuracy 2"]
  }
]

If no negative accounts found, return: []
"""

    vision_inputs = (
        [{"type": "image_url", "image_url": {"url": img, "detail": "high"}} for img in images]
        + [{"type": "text", "text": prompt}]
    )

    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": vision_inputs}],
        temperature=0
    )

    raw = resp.choices[0].message.content or ""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items
    except json.JSONDecodeError:
        pass

    return []


def _is_annual_credit_report(full_text):
    """Check if this is an Annual Credit Report format (annualcreditreport.com)."""
    header = full_text[:5000].lower()
    # Annual reports have distinctive URL patterns or titles
    if 'acr/printreport' in header or 'annualcreditreport.com' in header:
        return True
    if 'annual credit report' in header and 'at a glance' not in header:
        return True
    # Annual uses "Account Name" (capital N), direct uses "Account name" (lowercase n)
    if 'Account Name' in full_text[:5000] and 'At a glance' not in full_text[:3000]:
        return True
    return False


def _parse_annual_experian(full_text):
    """Parse Annual Credit Report (Experian) format using regex.

    Annual reports use 'Account Info' blocks instead of 'Account name' blocks,
    and have a different payment history grid format (J F M A M J J A S O N D).
    """
    blocks = re.split(r'(?=Account Info\n)', full_text)
    negative_items = []

    # Annual format uses different grid markers
    grid_regex = re.compile(
        r'\b(?:30|60|90|120|150|180)\b|(?<!\w)CO(?!\w)|(?<!\w)C(?!\w)(?!LS)(?!urrent)(?!losed)(?!ontact)(?!redit)'
    )
    status_regex = re.compile(
        r'\b(charged?\s+off|charge-off|repossession|collection(?:\s+account)?|'
        r'past\s+due|delinquent|settlement|written\s+off)\b',
        re.IGNORECASE
    )
    clean_status_regex = re.compile(
        r'\b(?:open.{0,5}never\s+late|(?:pays|paid)(?:\s+\w+)*\s+as\s+agreed|'
        r'closed.{0,5}never\s+late|transferred.{0,10}never\s+late)\b',
        re.IGNORECASE
    )

    seen_accounts = set()

    for block in blocks[1:]:
        lines = block.strip().splitlines()
        data = {
            "account_name": None,
            "account_number": None,
            "account_type": None,
            "balance": None,
            "status": None,
            "issue": None
        }

        # Extract fields from "Field Name VALUE" pattern
        for line in lines:
            stripped = line.strip()

            if stripped.startswith("Account Name") and not data["account_name"]:
                val = stripped.replace("Account Name", "", 1).strip()
                if val:
                    data["account_name"] = val

            elif stripped.startswith("Account Number") and not data["account_number"]:
                val = stripped.replace("Account Number", "", 1).strip()
                if val:
                    data["account_number"] = val

            elif stripped.startswith("Account Type") and not data["account_type"]:
                val = stripped.replace("Account Type", "", 1).strip()
                if val:
                    data["account_type"] = val

            elif stripped.startswith("Balance") and not stripped.startswith("Balance Updated") and not stripped.startswith("Balance Histories") and not data["balance"]:
                mm = re.search(r'\$[\d,]+', stripped)
                if mm:
                    data["balance"] = mm.group(0)

            elif stripped.startswith("Status ") and not data["status"]:
                val = stripped.replace("Status", "", 1).strip()
                if val:
                    data["status"] = val
            elif data["status"] and not data["balance"] and re.match(r'^(due|written|off)', stripped.lower()):
                # Status continuation line (wraps across lines in annual format)
                data["status"] += " " + stripped

        # Collect payment history lines
        payment_history = []
        in_ph = False
        for line in lines:
            stripped = line.strip()
            low = stripped.lower()
            if 'payment history' in low and 'guide' not in low:
                in_ph = True
                continue
            if in_ph:
                if not stripped or low.startswith(('payment history guide', 'balance histor', 'contact info', 'creditor', 'address')):
                    in_ph = False
                    continue
                # Keep grid data rows and legend lines
                if re.match(r'^[\d]{4}\b|^J F M|^Current|^Past\s+due|^Charge|^CO\b|^ND\b|^No data|^R\b', stripped, re.I):
                    payment_history.append(stripped)
                continue

        # Deduplicate
        acct_key = (
            (data["account_name"] or "").strip().upper(),
            (data["account_number"] or "").strip().upper()
        )
        if acct_key in seen_accounts:
            continue
        seen_accounts.add(acct_key)

        status_text = (data["status"] or "").strip()
        grid_text = " ".join(payment_history)

        # Skip clean accounts
        if clean_status_regex.search(status_text) and not grid_regex.search(grid_text):
            continue

        grid_issue = bool(grid_regex.search(grid_text))
        status_issue = bool(status_regex.search(status_text))
        acct_issue = "collection" in (data["account_type"] or "").lower()

        if not (grid_issue or status_issue or acct_issue):
            continue

        if status_issue:
            data["issue"] = status_text
        elif grid_issue:
            data["issue"] = "Late payments / Charge-off in payment history"
        else:
            data["issue"] = "Collection account"

        data["raw_payment_lines"] = payment_history
        negative_items.append(data)

    return negative_items


def extract_negative_items_from_pdf(file_path):
    """
    Extract negative/derogatory items from a credit report PDF.
    Auto-detects bureau format and uses appropriate parser.
    Falls back to vision-only extraction for unknown formats.
    """
    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    bureau = detect_bureau(full_text)

    if bureau == 'experian':
        if _is_annual_credit_report(full_text):
            items = _parse_annual_experian(full_text)
        else:
            items = _parse_experian(full_text)
    elif _is_transunion_native(full_text):
        items = _parse_transunion_native(full_text)
    else:
        # Unknown formats use vision-based extraction
        items = _parse_with_vision_only(file_path)

    # Run vision filter on all results for validation
    items = vision_filter_accounts(items, file_path)

    return items


def extract_pdf_metrics(pdf_path):
    """Extract high-level metrics from a credit report PDF."""
    try:
        items = extract_negative_items_from_pdf(pdf_path)
    except Exception:
        return {'negative_count': 0, 'total_collections': 0}

    negative_count = len(items)
    total_collections = sum(1 for item in items if 'collection' in (item.get('issue') or '').lower())

    return {
        'negative_count': negative_count,
        'total_collections': total_collections
    }
