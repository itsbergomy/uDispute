#!/usr/bin/env python3
"""
Credit Report Parser Test Harness
Runs the parser against labeled PDFs and scores precision/recall.

Usage:
  python test_parser_harness.py --regex-only          # Test regex tier only (no API cost)
  python test_parser_harness.py --with-vision          # Full pipeline with Vision API
  python test_parser_harness.py --file "somefile.pdf"   # Test single file
  python test_parser_harness.py --regex-only --verbose  # Show detailed account matching
"""

import os
import sys
import json
import argparse
import re
from difflib import SequenceMatcher

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfplumber
from services.pdf_parser import (
    _parse_experian, _parse_annual_experian, _is_annual_credit_report,
    _parse_transunion_native, _is_transunion_native,
    detect_bureau, extract_negative_items_from_pdf
)


# === Configuration ===
CORPUS_DIR = os.path.expanduser("~/Desktop/PDF Corpus")
LABELS_DIR = os.path.join(CORPUS_DIR, "labels")


def load_labels():
    """Load all ground truth label files."""
    labels = []
    for fname in sorted(os.listdir(LABELS_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(LABELS_DIR, fname)) as f:
            label = json.load(f)
        label["_label_file"] = fname
        labels.append(label)
    return labels


def normalize_name(name):
    """Normalize account name for fuzzy matching."""
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name


def normalize_number(num):
    """Normalize account number for matching."""
    if not num:
        return ""
    return re.sub(r'[^a-z0-9]', '', num.lower().strip())


def accounts_match(extracted, ground_truth):
    """Check if an extracted account matches a ground truth account."""
    ext_name = normalize_name(extracted.get("account_name", ""))
    gt_name = normalize_name(ground_truth.get("account_name", ""))

    # Direct name match
    if ext_name and gt_name:
        # Exact match
        if ext_name == gt_name:
            return True
        # Substring match (either direction)
        if ext_name in gt_name or gt_name in ext_name:
            return True
        # Fuzzy match (high threshold)
        ratio = SequenceMatcher(None, ext_name, gt_name).ratio()
        if ratio > 0.75:
            return True

    # Account number match (last 4+ chars)
    ext_num = normalize_number(extracted.get("account_number", ""))
    gt_num = normalize_number(ground_truth.get("account_number", ""))
    if ext_num and gt_num and len(ext_num) >= 4 and len(gt_num) >= 4:
        if ext_num[-6:] == gt_num[-6:]:
            return True
        if ext_num == gt_num:
            return True

    return False


def run_parser_regex_only(pdf_path):
    """Run only the regex parser (no Vision API calls)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"    ERROR reading PDF: {e}")
        return [], "error"

    bureau = detect_bureau(full_text)

    if bureau == "experian":
        if _is_annual_credit_report(full_text):
            items = _parse_annual_experian(full_text)
            return items, bureau + " (annual)"
        else:
            items = _parse_experian(full_text)
            return items, bureau
    elif _is_transunion_native(full_text):
        items = _parse_transunion_native(full_text)
        return items, "transunion (native)"
    else:
        # Unknown formats return empty in regex-only mode (would need Vision)
        return [], bureau


def run_parser_full(pdf_path):
    """Run the full parser pipeline including Vision API."""
    try:
        items = extract_negative_items_from_pdf(pdf_path)
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        bureau = detect_bureau(full_text)
        return items, bureau
    except Exception as e:
        print(f"    ERROR: {e}")
        return [], "error"


def score_report(extracted, ground_truth_negatives, verbose=False):
    """Score extracted items against ground truth. Returns (matched, false_neg, false_pos)."""
    gt_accounts = list(ground_truth_negatives)  # copy
    matched = []
    false_positives = []

    for ext in extracted:
        found_match = False
        for i, gt in enumerate(gt_accounts):
            if accounts_match(ext, gt):
                matched.append((ext, gt))
                gt_accounts.pop(i)
                found_match = True
                break
        if not found_match:
            false_positives.append(ext)

    false_negatives = gt_accounts  # remaining unmatched ground truth

    return matched, false_negatives, false_positives


def print_report_result(label, extracted, matched, false_neg, false_pos, bureau_detected, verbose=False):
    """Print results for a single report."""
    expected = label.get("expected_negative_count", len(label.get("negative_accounts", [])))
    n_extracted = len(extracted)
    n_matched = len(matched)

    precision = (n_matched / n_extracted * 100) if n_extracted > 0 else 100.0
    recall = (n_matched / expected * 100) if expected > 0 else 100.0

    status = "PASS" if recall == 100 and precision == 100 else "FAIL" if recall < 50 else "PARTIAL"
    status_icon = {"PASS": "✅", "FAIL": "❌", "PARTIAL": "⚠️"}[status]

    print(f"\n{status_icon}  {label['file']}")
    print(f"   Bureau: {label.get('bureau', '?')} (detected: {bureau_detected}) | Format: {label.get('format', '?')}")
    print(f"   Expected: {expected} | Extracted: {n_extracted} | Matched: {n_matched}")
    print(f"   Precision: {precision:.0f}% | Recall: {recall:.0f}%")

    if verbose or false_neg or false_pos:
        for ext, gt in matched:
            print(f"   ✅ {gt['account_name']} → matched '{ext.get('account_name', '?')}'")

        for gt in false_neg:
            print(f"   ❌ MISSED: {gt['account_name']} ({gt.get('status', '?')})")

        for ext in false_pos:
            print(f"   ⚠️  FALSE POS: {ext.get('account_name', '?')} (issue: {ext.get('issue', '?')})")

    return {
        "file": label["file"],
        "expected": expected,
        "extracted": n_extracted,
        "matched": n_matched,
        "precision": precision,
        "recall": recall,
        "false_neg_count": len(false_neg),
        "false_pos_count": len(false_pos),
        "status": status,
        "bureau": bureau_detected,
    }


def main():
    parser = argparse.ArgumentParser(description="Credit Report Parser Test Harness")
    parser.add_argument("--regex-only", action="store_true",
                        help="Test regex parser only (no Vision API cost)")
    parser.add_argument("--with-vision", action="store_true",
                        help="Run full pipeline with Vision API")
    parser.add_argument("--file", type=str,
                        help="Test a single PDF file (filename only, not path)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed account matching")
    args = parser.parse_args()

    if not args.with_vision:
        args.regex_only = True  # Default to regex-only

    print("=" * 60)
    print("CREDIT REPORT PARSER TEST HARNESS")
    print("=" * 60)
    mode = "REGEX-ONLY (no API cost)" if args.regex_only else "FULL PIPELINE (Vision API)"
    print(f"Mode: {mode}")

    labels = load_labels()
    if not labels:
        print(f"ERROR: No label files found in {LABELS_DIR}")
        sys.exit(1)

    print(f"Loaded {len(labels)} label files")

    # Filter to single file if specified
    if args.file:
        labels = [l for l in labels if args.file.lower() in l["file"].lower()]
        if not labels:
            print(f"ERROR: No label found matching '{args.file}'")
            sys.exit(1)

    results = []
    skipped = []

    for label in labels:
        pdf_path = os.path.join(CORPUS_DIR, label["file"])

        if not os.path.exists(pdf_path):
            print(f"\n⏭️  SKIPPED: {label['file']} (PDF not found)")
            skipped.append(label["file"])
            continue

        gt_negatives = label.get("negative_accounts", [])

        if args.regex_only:
            extracted, bureau = run_parser_regex_only(pdf_path)
        else:
            extracted, bureau = run_parser_full(pdf_path)

        matched, false_neg, false_pos = score_report(extracted, gt_negatives, args.verbose)
        result = print_report_result(label, extracted, matched, false_neg, false_pos, bureau, args.verbose)
        results.append(result)

    # Aggregate results
    print("\n" + "=" * 60)
    print("AGGREGATE RESULTS")
    print("=" * 60)

    if not results:
        print("No results to aggregate.")
        return

    total = len(results)
    avg_precision = sum(r["precision"] for r in results) / total
    avg_recall = sum(r["recall"] for r in results) / total
    perfect_recall = sum(1 for r in results if r["recall"] == 100)
    perfect_precision = sum(1 for r in results if r["precision"] == 100)
    total_expected = sum(r["expected"] for r in results)
    total_matched = sum(r["matched"] for r in results)
    total_extracted = sum(r["extracted"] for r in results)
    total_false_neg = sum(r["false_neg_count"] for r in results)
    total_false_pos = sum(r["false_pos_count"] for r in results)

    # Separate by bureau detection
    experian_results = [r for r in results if r["bureau"] == "experian"]
    non_experian = [r for r in results if r["bureau"] != "experian"]

    print(f"  Total files tested: {total}")
    if skipped:
        print(f"  Skipped (not found): {len(skipped)}")
    print(f"  Total expected negatives: {total_expected}")
    print(f"  Total extracted: {total_extracted}")
    print(f"  Total matched: {total_matched}")
    print(f"  Total false negatives (missed): {total_false_neg}")
    print(f"  Total false positives (junk): {total_false_pos}")
    print()
    print(f"  Avg Precision: {avg_precision:.1f}%")
    print(f"  Avg Recall: {avg_recall:.1f}%")
    print(f"  Perfect recall (100%): {perfect_recall}/{total}")
    print(f"  Perfect precision (100%): {perfect_precision}/{total}")

    if experian_results:
        exp_recall = sum(r["recall"] for r in experian_results) / len(experian_results)
        exp_precision = sum(r["precision"] for r in experian_results) / len(experian_results)
        print(f"\n  --- Experian (regex tier) ---")
        print(f"  Files: {len(experian_results)}")
        print(f"  Avg Precision: {exp_precision:.1f}% | Avg Recall: {exp_recall:.1f}%")

    if non_experian:
        ne_recall = sum(r["recall"] for r in non_experian) / len(non_experian)
        ne_precision = sum(r["precision"] for r in non_experian) / len(non_experian)
        print(f"\n  --- Non-Experian (Vision fallback) ---")
        print(f"  Files: {len(non_experian)}")
        if args.regex_only:
            print(f"  (Skipped in regex-only mode — all show 0 extracted)")
        print(f"  Avg Precision: {ne_precision:.1f}% | Avg Recall: {ne_recall:.1f}%")

    print()


if __name__ == "__main__":
    main()
