"""
Autoresearch Loop for Vision Prompt Optimization

Tests different Vision prompt strategies against ground truth labels
and finds the approach that maximizes recall on non-regex-parseable reports.

Usage:
    python autoresearch_vision.py
"""

import sys
import json
import os
import time
import re
import pdfplumber
import fitz
import base64
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI()

PDF_DIR = os.path.expanduser('~/Desktop/PDF Corpus/')
LABELS_DIR = os.path.join(PDF_DIR, 'labels/')

# Files that need Vision (regex can't handle them)
VISION_TEST_FILES = [
    ('Your Report _ TransUnion Credit Report.pdf', 'transunion_kenroy_campbell.json'),
    ('View Your Report _ TransUnion Credit Report.pdf', 'transunion_yana_freeman.json'),
    ('Olivia Fragoso - Credit Report - MyScoreIQ.pdf', 'myscoreiq_olivia.json'),
]


def classify_page(text):
    """Classify a PDF page type."""
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


def get_account_pages(pdf_path, max_pages=30):
    """Get base64 images of account-relevant pages only."""
    images = []
    page_indices = []

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            if classify_page(text) == 'account':
                page_indices.append(i)

    if not page_indices:
        page_indices = list(range(min(max_pages, 50)))

    doc = fitz.open(pdf_path)
    for i in page_indices[:max_pages]:
        if i >= len(doc):
            continue
        pix = doc[i].get_pixmap(dpi=150)
        b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
        images.append(f"data:image/png;base64,{b64}")

    return images, page_indices[:max_pages]


def run_vision_prompt(images, prompt_text, model="gpt-4o"):
    """Send images + prompt to Vision API and parse JSON response."""
    vision_inputs = (
        [{"type": "image_url", "image_url": {"url": img, "detail": "high"}} for img in images]
        + [{"type": "text", "text": prompt_text}]
    )

    resp = client.chat.completions.create(
        model=model,
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


def score_results(extracted, label):
    """Score extracted items against ground truth label."""
    expected = label.get('negative_accounts', [])
    expected_names = [a['account_name'].upper().strip() for a in expected]
    extracted_names = [a.get('account_name', '').upper().strip() for a in extracted]

    matched = 0
    for exp_name in expected_names:
        # Fuzzy match: check if expected name is a substring of any extracted name or vice versa
        for ext_name in extracted_names:
            if exp_name in ext_name or ext_name in exp_name or (
                len(exp_name) > 5 and exp_name[:8] == ext_name[:8]
            ):
                matched += 1
                break

    expected_count = len(expected)
    precision = matched / len(extracted) * 100 if extracted else 100
    recall = matched / expected_count * 100 if expected_count > 0 else 100

    return {
        'expected': expected_count,
        'extracted': len(extracted),
        'matched': matched,
        'precision': precision,
        'recall': recall,
        'missed': [n for n in expected_names if not any(
            n in e or e in n or (len(n) > 5 and n[:8] == e[:8])
            for e in extracted_names
        )]
    }


# ============================================================
# PROMPT STRATEGIES
# ============================================================

STRATEGY_SINGLE_PASS = """Analyze this credit report and extract ALL negative/derogatory accounts.

An account is negative if ANY of these are true:
- Status contains: charge-off, collection, repossession, past due, delinquent, settlement, written off
- Payment history shows ANY late payments: 30, 60, 90, 120 days late, C/O, RPO, COL
- Loan Type is "COLLECTION AGENCY/ATTORNEY" or "FACTORING COMPANY ACCOUNT"
- Pay Status shows anything adverse (even if account is now paid/closed)
- Remarks include: PLACED FOR COLLECTION, REPOSSESSION, PROFIT AND LOSS, CHARGE OFF
- Account has an Original Creditor listed (indicates debt was sold)

IMPORTANT: Include accounts that are currently paying as agreed but HAVE historical late payments in their payment history. A single 30-day late from years ago still counts.

For each negative account return:
- account_name: The creditor/company name
- account_number: The account number (masked is fine)
- account_type: Type of account
- balance: Current balance with $ sign
- status: Account status text
- issue: Why this account is negative
- original_creditor: If listed (null if not)
- inaccuracies: Array of strings describing any reporting inaccuracies found. Empty array [] if none.

RETURN ONLY valid JSON array. If no negative accounts found, return: []
"""

STRATEGY_TWO_PASS_ADVERSARIAL = """You are analyzing a credit report. Your job is to find EVERY account with ANY negative mark — even subtle ones.

STEP 1: Find obvious negatives
Look for accounts with:
- "Charge-off", "Collection", "Repossession" in Pay Status
- Accounts under "Accounts with Adverse Information" section
- Accounts with Loan Type "COLLECTION AGENCY/ATTORNEY" or "FACTORING COMPANY ACCOUNT"
- Accounts with an Original Creditor listed

STEP 2: CRITICAL — Scan "Satisfactory Accounts" for hidden negatives
Many accounts are listed under "Satisfactory Accounts" but still have HISTORICAL late payments buried in their payment history grids. Look at EVERY account's payment history grid carefully:
- Scan each row of the payment history for any cell showing: 30, 60, 90, 120, C/O, RPO, COL
- Even if the account currently shows "OK" or "Current Account", if there is even ONE late payment anywhere in the history, it is a negative account
- Pay special attention to payment history from 2019 — many accounts had late payments in 2019 but recovered

STEP 3: Check Pay Status for historical marks
Some accounts have Pay Status like:
- "Paid, Closed; was 60 days past due date" — this IS negative
- "Account paid in Full was a Charge-off" — this IS negative
- "Current Account" but with historical lates — this IS negative

For each negative account return:
- account_name: The creditor/company name
- account_number: The account number (masked is fine)
- account_type: Type of account
- balance: Current balance with $ sign
- status: Account status text
- issue: Why this account is negative (be specific about what late payments you found and when)
- original_creditor: If listed (null if not)
- inaccuracies: Array of strings describing any reporting inaccuracies. Empty array [] if none.

RETURN ONLY valid JSON array. If no negative accounts found, return: []
"""

STRATEGY_EXHAUSTIVE_SCAN = """You are a credit report auditor. Your task is to identify EVERY account that has ANY negative mark in its history, no matter how old or how minor.

RULES:
1. A single 30-day late payment from 5+ years ago STILL COUNTS as negative
2. An account listed as "Satisfactory" or "Current" IS negative if its payment history grid contains even ONE entry of: 30, 60, 90, 120, C/O, RPO, COL, VS, FC
3. An account with Pay Status "Paid, Closed; was X days past due" IS negative
4. An account with Pay Status "Account paid in Full was a Charge-off" IS negative
5. An account with "Settled—less than full balance" in Remarks IS negative
6. An account with an Original Creditor IS negative (it's a collection/debt buyer)
7. DO NOT skip any account just because it's currently in good standing

PROCESS:
- Go through EVERY account on EVERY page
- For each account, read its FULL payment history grid row by row
- If you find ANY non-OK, non-X, non-N/R entry (like 30, 60, 90, 120, C/O), flag it
- Check the Pay Status field for historical adverse indicators
- Check Remarks for adverse keywords

For each negative account return JSON:
- account_name: Creditor name
- account_number: Account number
- account_type: Type
- balance: Balance with $
- status: Pay Status text
- issue: Specific reason (include dates of late payments if visible)
- original_creditor: If listed, else null
- inaccuracies: Array of reporting errors found, or []

RETURN ONLY valid JSON array. Be thorough — missing a negative account is worse than including a borderline one.
"""


STRATEGIES = {
    'single_pass': STRATEGY_SINGLE_PASS,
    'two_pass_adversarial': STRATEGY_TWO_PASS_ADVERSARIAL,
    'exhaustive_scan': STRATEGY_EXHAUSTIVE_SCAN,
}


def run_experiment(strategy_name, prompt_text, test_file, label_file, max_pages=30):
    """Run a single experiment: one strategy on one file."""
    pdf_path = os.path.join(PDF_DIR, test_file)
    label_path = os.path.join(LABELS_DIR, label_file)

    with open(label_path) as f:
        label = json.load(f)

    images, page_indices = get_account_pages(pdf_path, max_pages=max_pages)

    start = time.time()
    extracted = run_vision_prompt(images, prompt_text)
    elapsed = time.time() - start

    score = score_results(extracted, label)

    return {
        'strategy': strategy_name,
        'file': test_file,
        'pages_sent': len(images),
        'time': elapsed,
        'score': score,
        'extracted': extracted,
    }


def main():
    print("=" * 80)
    print("AUTORESEARCH LOOP — Vision Prompt Optimization")
    print("=" * 80)
    print()

    # Focus on the hardest file: Kenroy Campbell (130 pages, 6 subtle misses)
    target_file = 'Your Report _ TransUnion Credit Report.pdf'
    target_label = 'transunion_kenroy_campbell.json'

    results = []

    for strategy_name, prompt_text in STRATEGIES.items():
        print(f"\n{'—'*60}")
        print(f"Strategy: {strategy_name}")
        print(f"{'—'*60}")

        result = run_experiment(strategy_name, prompt_text, target_file, target_label)
        results.append(result)

        s = result['score']
        print(f"  Pages sent: {result['pages_sent']}")
        print(f"  Time: {result['time']:.1f}s")
        print(f"  Expected: {s['expected']} | Extracted: {s['extracted']} | Matched: {s['matched']}")
        print(f"  Recall: {s['recall']:.0f}% | Precision: {s['precision']:.0f}%")

        if s['missed']:
            print(f"  Missed: {', '.join(s['missed'])}")

        # Show what it found
        for item in result['extracted']:
            name = item.get('account_name', '?')
            issue = item.get('issue', '?')[:70]
            print(f"    -> {name}: {issue}")

    # Summary
    print(f"\n{'='*80}")
    print("RESULTS SUMMARY")
    print(f"{'='*80}")
    print(f"{'Strategy':<25} {'Recall':>7} {'Precision':>10} {'Extracted':>10} {'Time':>8}")
    print(f"{'-'*60}")

    best = None
    for r in results:
        s = r['score']
        print(f"{r['strategy']:<25} {s['recall']:>6.0f}% {s['precision']:>9.0f}% {s['extracted']:>10} {r['time']:>7.1f}s")
        if best is None or s['recall'] > best['score']['recall']:
            best = r

    print(f"\nBest strategy: {best['strategy']} ({best['score']['recall']:.0f}% recall)")

    # If best is better than current, also test it on the other files
    if best['score']['recall'] > 33:
        print(f"\n{'='*80}")
        print(f"Testing best strategy ({best['strategy']}) on all Vision files")
        print(f"{'='*80}")

        for test_file, label_file in VISION_TEST_FILES:
            if test_file == target_file:
                print(f"\n  {test_file}: {best['score']['recall']:.0f}% recall (already tested)")
                continue

            result = run_experiment(best['strategy'], STRATEGIES[best['strategy']], test_file, label_file)
            s = result['score']
            print(f"\n  {test_file}:")
            print(f"    Recall: {s['recall']:.0f}% | Extracted: {s['extracted']}/{s['expected']}")
            if s['missed']:
                print(f"    Missed: {', '.join(s['missed'])}")


if __name__ == '__main__':
    main()
