#!/usr/bin/env python3
"""
Autoresearch Loop — Cloudinary Integration Hardening

Tests every Cloudinary code path against real and adversarial inputs,
scores the results, identifies failure patterns, and proposes code fixes.

Usage:
  python autoresearch_cloudinary.py                  # Full run
  python autoresearch_cloudinary.py --dry-run        # Test only, no fix proposals
  python autoresearch_cloudinary.py --iterations 3   # Run N improvement cycles
  python autoresearch_cloudinary.py --live           # Test against live Cloudinary API
"""

import os
import sys
import json
import argparse
import time
import importlib
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load env
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "autoresearch_results_cloudinary")
client = OpenAI()


# ═══════════════════════════════════════════════════════════
#  Test Corpus — Real and adversarial Cloudinary URLs
# ═══════════════════════════════════════════════════════════

CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', 'dejmfdvv0')

TEST_CORPUS = [
    # ── Standard paths (should PASS) ──
    {
        "name": "standard_raw_pdf",
        "input": f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/v1706123456/udispute/clients/5/report.pdf",
        "expect_signed": True,
        "expect_resource_type": "raw",
        "expect_public_id": "udispute/clients/5/report.pdf",
    },
    {
        "name": "standard_image",
        "input": f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/v1706123456/udispute/clients/5/id_scan.jpg",
        "expect_signed": True,
        "expect_resource_type": "image",
        "expect_public_id": "udispute/clients/5/id_scan.jpg",
    },
    {
        "name": "no_version_prefix",
        "input": f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/udispute/clients/5/report.pdf",
        "expect_signed": True,
        "expect_resource_type": "raw",
        "expect_public_id": "udispute/clients/5/report.pdf",
    },
    {
        "name": "url_with_existing_transform",
        "input": f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/fl_attachment:false/v1706123456/udispute/clients/5/report.pdf",
        "expect_signed": True,
        "expect_resource_type": "raw",
        "expect_public_id": "udispute/clients/5/report.pdf",
    },
    {
        "name": "bare_public_id",
        "input": "udispute/clients/5/report.pdf",
        "expect_signed": True,
        "expect_resource_type": "raw",
        "expect_public_id": "udispute/clients/5/report.pdf",
    },
    {
        "name": "deeply_nested_path",
        "input": f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/v1234/udispute/clients/99/correspondence/20260401_response_from_experian.pdf",
        "expect_signed": True,
        "expect_resource_type": "raw",
        "expect_public_id": "udispute/clients/99/correspondence/20260401_response_from_experian.pdf",
    },
    {
        "name": "filename_with_spaces_encoded",
        "input": f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/v1234/udispute/clients/5/my%20credit%20report.pdf",
        "expect_signed": True,
        "expect_resource_type": "raw",
    },

    # ── Edge cases (should handle gracefully) ──
    {
        "name": "none_input",
        "input": None,
        "expect_signed": False,
        "expect_none": True,
    },
    {
        "name": "empty_string",
        "input": "",
        "expect_signed": False,
        "expect_none": True,
    },
    {
        "name": "local_filename_only",
        "input": "5_pdf_file_credit_report.pdf",
        "expect_signed": True,  # bare public_id path
        "expect_resource_type": "raw",
    },

    # ── SSRF adversarial (should BLOCK) ──
    {
        "name": "ssrf_evil_domain",
        "input": "https://evil.com/hack.pdf",
        "expect_ssrf_blocked": True,
    },
    {
        "name": "ssrf_subdomain_spoof",
        "input": "https://evil.cloudinary.com.attacker.com/raw/upload/v1/file.pdf",
        "expect_ssrf_blocked": True,
    },
    {
        "name": "ssrf_localhost",
        "input": "https://localhost:8080/admin/secrets",
        "expect_ssrf_blocked": True,
    },
    {
        "name": "ssrf_internal_ip",
        "input": "https://169.254.169.254/latest/meta-data/",
        "expect_ssrf_blocked": True,
    },
    {
        "name": "ssrf_redirect_trick",
        "input": "https://res.cloudinary.com@evil.com/raw/upload/v1/file.pdf",
        "expect_ssrf_blocked": True,
    },
]


# ═══════════════════════════════════════════════════════════
#  Scoring Rubric
# ═══════════════════════════════════════════════════════════

SCORING_PROMPT = """You are a senior security engineer and cloud infrastructure expert.

Score the following Cloudinary integration test results on these 7 dimensions (1-10 each):

1. **URL SIGNING** (1-10): Are ALL returned URLs signed (contain `s--` hash)?
   1 = unsigned URLs returned, 10 = every URL is properly signed.

2. **INLINE DISPLAY** (1-10): When inline=True, do URLs contain fl_attachment:false?
   1 = PDFs would download instead of displaying, 10 = all inline URLs correct.

3. **PUBLIC_ID EXTRACTION** (1-10): Are public_ids correctly extracted from all URL
   formats (with version, without version, with transforms, encoded chars)?
   1 = extraction fails on edge cases, 10 = handles all formats.

4. **SSRF PROTECTION** (1-10): Are all adversarial URLs blocked? Including subdomain
   spoofing, localhost, internal IPs, and @ symbol tricks?
   1 = vulnerable to SSRF, 10 = all attack vectors blocked.

5. **NULL SAFETY** (1-10): Does the code handle None, empty strings, and missing
   fields without crashing? 1 = throws exceptions, 10 = graceful handling.

6. **DELETE CAPABILITY** (1-10): Can files be deleted using only the stored URL
   (since only URLs are stored in the DB, not bare public_ids)?
   1 = delete broken for URL inputs, 10 = handles both URLs and public_ids.

7. **DOWNLOAD RELIABILITY** (1-10): Does download_to_temp use signed URLs so it
   works even when unsigned raw access is restricted?
   1 = uses unsigned URLs, 10 = always signed.

IMPORTANT: Identify the TOP 3 remaining vulnerabilities or failure modes.

Respond in this exact JSON format:
{
  "scores": {
    "url_signing": <int>,
    "inline_display": <int>,
    "public_id_extraction": <int>,
    "ssrf_protection": <int>,
    "null_safety": <int>,
    "delete_capability": <int>,
    "download_reliability": <int>
  },
  "composite_score": <float>,
  "vulnerabilities": [
    {"issue": "<description>", "severity": "<critical|high|medium|low>", "fix": "<specific fix>"},
    {"issue": "<description>", "severity": "<critical|high|medium|low>", "fix": "<specific fix>"},
    {"issue": "<description>", "severity": "<critical|high|medium|low>", "fix": "<specific fix>"}
  ],
  "verdict": "<one sentence overall assessment>"
}

TEST RESULTS TO SCORE:
"""

IMPROVEMENT_PROMPT = """You are a senior Python engineer specializing in cloud storage security.

Based on the scoring results below, propose specific code changes to cloud_storage.py
to address the identified vulnerabilities.

CURRENT CODE:
{current_code}

SCORING RESULTS:
{scoring_summary}

VULNERABILITIES:
{vulnerabilities}

REQUIREMENTS:
1. Address each vulnerability with a specific code change.
2. Do NOT break existing functionality.
3. Prefer defensive coding — handle every edge case.
4. Return ONLY the proposed code changes as unified diff format — no explanation.
"""


# ═══════════════════════════════════════════════════════════
#  Test Runner
# ═══════════════════════════════════════════════════════════

def reload_module():
    """Hot-reload cloud_storage so code changes take effect between iterations."""
    import services.cloud_storage as cs
    importlib.reload(cs)
    return cs


def run_tests():
    """Run all test cases against the current cloud_storage module."""
    cs = reload_module()
    results = []

    for test in TEST_CORPUS:
        name = test["name"]
        input_val = test["input"]
        result = {"name": name, "input": repr(input_val), "passed": True, "details": {}}

        try:
            # ── _parse_cloudinary_url ──
            if input_val and isinstance(input_val, str) and input_val.startswith('http'):
                pid, rtype = cs._parse_cloudinary_url(input_val)
                result["details"]["parsed_public_id"] = pid
                result["details"]["parsed_resource_type"] = rtype

                if test.get("expect_public_id") and pid != test["expect_public_id"]:
                    result["passed"] = False
                    result["details"]["parse_error"] = f"Expected pid={test['expect_public_id']}, got {pid}"

                if test.get("expect_resource_type") and rtype != test["expect_resource_type"]:
                    result["passed"] = False
                    result["details"]["type_error"] = f"Expected type={test['expect_resource_type']}, got {rtype}"

            # ── is_cloudinary_url (SSRF guard) ──
            if test.get("expect_ssrf_blocked"):
                is_safe = cs.is_cloudinary_url(input_val)
                result["details"]["is_cloudinary_url"] = is_safe
                if is_safe:
                    result["passed"] = False
                    result["details"]["ssrf_failure"] = f"SSRF: {input_val} was NOT blocked"

            # ── get_file_url (signing) ──
            if not test.get("expect_none") and not test.get("expect_ssrf_blocked"):
                url = cs.get_file_url(input_val)
                result["details"]["get_file_url"] = url
                if test.get("expect_signed") and url:
                    if "s--" not in url:
                        result["passed"] = False
                        result["details"]["signing_error"] = "URL is NOT signed"

            # ── get_signed_url (inline) ──
            if not test.get("expect_none") and not test.get("expect_ssrf_blocked"):
                signed = cs.get_signed_url(input_val, inline=True)
                result["details"]["get_signed_url_inline"] = signed
                if signed and test.get("expect_signed"):
                    if "fl_attachment" not in signed:
                        result["passed"] = False
                        result["details"]["inline_error"] = "Missing fl_attachment:false"

            # ── Null safety ──
            if test.get("expect_none"):
                for fn_name in ['get_file_url', 'get_signed_url', 'download_to_temp', 'delete_file']:
                    fn = getattr(cs, fn_name)
                    try:
                        out = fn(input_val)
                        result["details"][f"{fn_name}_null"] = repr(out)
                    except Exception as e:
                        result["passed"] = False
                        result["details"][f"{fn_name}_crash"] = str(e)

            # ── delete_file with URL ──
            if input_val and isinstance(input_val, str) and input_val.startswith('http') and not test.get("expect_ssrf_blocked"):
                # Don't actually delete — just verify it doesn't crash
                # and that it extracted a public_id (will fail gracefully since file doesn't exist)
                try:
                    del_result = cs.delete_file(input_val)
                    result["details"]["delete_file_result"] = del_result
                    # Should return False (file doesn't exist) but NOT crash
                except Exception as e:
                    result["passed"] = False
                    result["details"]["delete_crash"] = str(e)

        except Exception as e:
            result["passed"] = False
            result["details"]["exception"] = str(e)

        results.append(result)

    return results


def format_results_for_scoring(results):
    """Format test results into a readable string for LLM scoring."""
    lines = []
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    lines.append(f"PASS RATE: {passed}/{total} ({100*passed/total:.0f}%)\n")

    for r in results:
        status = "✓ PASS" if r["passed"] else "✗ FAIL"
        lines.append(f"\n{status}: {r['name']}")
        lines.append(f"  Input: {r.get('input', 'N/A')}")
        for key, val in r["details"].items():
            # Truncate long URLs for readability
            val_str = str(val)
            if len(val_str) > 120:
                val_str = val_str[:120] + "..."
            lines.append(f"  {key}: {val_str}")

    return "\n".join(lines)


def score_results(results_text):
    """Score test results using LLM judge."""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a security engineer. Respond only in valid JSON."},
            {"role": "user", "content": SCORING_PROMPT + results_text}
        ],
        response_format={"type": "json_object"},
    )

    try:
        return json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return None


def print_summary(scores):
    """Print formatted scoring summary."""
    if not scores:
        print("  ⚠ Scoring failed")
        return

    print(f"\n{'═' * 60}")
    print("AUTORESEARCH — CLOUDINARY INTEGRATION SCORES")
    print(f"{'═' * 60}")
    print(f"\n  Composite: {scores.get('composite_score', '?')}/10\n")

    for dim, score in sorted(scores.get('scores', {}).items(), key=lambda x: x[1], reverse=True):
        bar = '█' * int(score) + '░' * (10 - int(score))
        flag = ''
        if score <= 5:
            flag = ' ← CRITICAL'
        elif score <= 7:
            flag = ' ← NEEDS WORK'
        print(f"  {dim:.<32s} {score}/10  {bar}{flag}")

    vulns = scores.get('vulnerabilities', [])
    if vulns:
        print(f"\n  Vulnerabilities:")
        for v in vulns:
            sev = v.get('severity', 'unknown').upper()
            print(f"    [{sev}] {v.get('issue', '')}")
            print(f"           Fix: {v.get('fix', '')}")

    print(f"\n  Verdict: {scores.get('verdict', 'N/A')}")
    print(f"{'═' * 60}")


def propose_improvements(scores, current_code):
    """Use LLM to propose code fixes for identified vulnerabilities."""
    vulns = scores.get('vulnerabilities', [])
    vuln_text = "\n".join(
        f"  [{v.get('severity', '?').upper()}] {v.get('issue', '')}\n    Fix: {v.get('fix', '')}"
        for v in vulns
    )

    scoring_text = "\n".join(
        f"  {dim}: {score}/10"
        for dim, score in scores.get('scores', {}).items()
    )

    prompt = IMPROVEMENT_PROMPT.format(
        current_code=current_code[:8000],
        scoring_summary=scoring_text,
        vulnerabilities=vuln_text,
    )

    response = client.chat.completions.create(
        model="o3",
        messages=[
            {"role": "system", "content": "You are a senior Python security engineer."},
            {"role": "user", "content": prompt}
        ],
    )

    return response.choices[0].message.content


def save_results(test_results, scores, iteration=0):
    """Save results to JSON."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    output = {
        "timestamp": timestamp,
        "iteration": iteration,
        "test_results": test_results,
        "scores": scores,
    }

    filepath = os.path.join(RESULTS_DIR, f'cloudinary_iter{iteration}_{timestamp}.json')
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✓ Results saved to {filepath}")
    return filepath


# ═══════════════════════════════════════════════════════════
#  Live Tests (optional — hits actual Cloudinary API)
# ═══════════════════════════════════════════════════════════

def run_live_tests():
    """Upload a test file, retrieve it, verify signed URL works, then clean up."""
    cs = reload_module()

    if not cs.is_configured():
        print("  ⚠ Cloudinary not configured — skipping live tests")
        return []

    results = []
    import tempfile

    # Create a tiny test PDF
    test_content = b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(test_content)
    tmp.close()

    try:
        # Upload
        print("  Uploading test file...")
        upload_result = cs.upload_file(
            tmp.name, folder="test/autoresearch", resource_type="raw"
        )

        if not upload_result:
            results.append({"name": "live_upload", "passed": False, "details": {"error": "Upload returned None"}})
            return results

        results.append({
            "name": "live_upload",
            "passed": True,
            "details": {
                "public_id": upload_result["public_id"],
                "secure_url": upload_result["secure_url"],
            }
        })

        stored_url = upload_result["secure_url"]

        # get_file_url from stored URL
        signed_url = cs.get_file_url(stored_url)
        is_signed = "s--" in (signed_url or "")
        results.append({
            "name": "live_get_file_url_from_stored",
            "passed": is_signed,
            "details": {"signed_url": signed_url, "is_signed": is_signed}
        })

        # get_signed_url with inline
        inline_url = cs.get_signed_url(stored_url, inline=True)
        has_inline = "fl_attachment" in (inline_url or "")
        results.append({
            "name": "live_inline_url",
            "passed": has_inline and "s--" in (inline_url or ""),
            "details": {"inline_url": inline_url, "has_inline_flag": has_inline}
        })

        # download_to_temp
        print("  Downloading via signed URL...")
        temp_path = cs.download_to_temp(stored_url)
        download_ok = temp_path is not None and os.path.exists(temp_path)
        dl_size = os.path.getsize(temp_path) if download_ok else 0
        results.append({
            "name": "live_download",
            "passed": download_ok and dl_size > 0,
            "details": {"temp_path": temp_path, "size_bytes": dl_size}
        })
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

        # delete_file from stored URL
        print("  Deleting via URL...")
        del_ok = cs.delete_file(stored_url)
        results.append({
            "name": "live_delete_from_url",
            "passed": del_ok,
            "details": {"delete_result": del_ok}
        })

    finally:
        os.unlink(tmp.name)

    return results


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Autoresearch — Cloudinary Integration Hardening')
    parser.add_argument('--iterations', type=int, default=1, help='Number of improvement cycles (default: 1)')
    parser.add_argument('--dry-run', action='store_true', help='Test and score only, no fix proposals')
    parser.add_argument('--live', action='store_true', help='Include live upload/download/delete tests')
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════╗")
    print("║  AUTORESEARCH — Cloudinary Integration Hardening ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Iterations: {args.iterations:<35d} ║")
    print(f"║  Live tests: {'ON' if args.live else 'OFF':<35s} ║")
    print("╚══════════════════════════════════════════════════╝")

    for iteration in range(args.iterations):
        print(f"\n{'=' * 55}")
        print(f"  ITERATION {iteration + 1}/{args.iterations}")
        print(f"{'=' * 55}")

        # Step 1: Run unit tests
        print(f"\n[1/4] Running test corpus ({len(TEST_CORPUS)} cases)...")
        test_results = run_tests()

        passed = sum(1 for r in test_results if r["passed"])
        total = len(test_results)
        print(f"  Result: {passed}/{total} passed")
        for r in test_results:
            status = "✓" if r["passed"] else "✗"
            print(f"    {status} {r['name']}")

        # Step 1b: Live tests if requested
        if args.live:
            print(f"\n[1b] Running live Cloudinary tests...")
            live_results = run_live_tests()
            test_results.extend(live_results)
            for r in live_results:
                status = "✓" if r["passed"] else "✗"
                print(f"    {status} {r['name']}")

        # Step 2: Score with LLM judge
        print(f"\n[2/4] Scoring with LLM judge...")
        results_text = format_results_for_scoring(test_results)
        scores = score_results(results_text)

        # Step 3: Analyze
        print(f"\n[3/4] Analysis:")
        print_summary(scores)

        # Save
        save_results(test_results, scores, iteration)

        # Step 4: Propose improvements
        if not args.dry_run and scores:
            composite = scores.get('composite_score', 10)
            if composite >= 9.5:
                print(f"\n[4/4] Score is {composite}/10 — no improvements needed. Ship it.")
                break

            print(f"\n[4/4] Proposing code improvements...")
            code_path = os.path.join(os.path.dirname(__file__), "services", "cloud_storage.py")
            with open(code_path) as f:
                current_code = f.read()

            improved = propose_improvements(scores, current_code)

            improvement_path = os.path.join(RESULTS_DIR, f'proposed_fix_iter{iteration + 1}.txt')
            os.makedirs(RESULTS_DIR, exist_ok=True)
            with open(improvement_path, 'w') as f:
                f.write(improved)
            print(f"  ✓ Proposed changes saved to {improvement_path}")
            print(f"\n{'─' * 50}")
            print(improved[:3000])
            if len(improved) > 3000:
                print(f"\n... ({len(improved) - 3000} more characters)")

        elif args.dry_run:
            print(f"\n[4/4] Dry run — skipping fix proposals")

    # Final
    print(f"\n{'═' * 55}")
    print("AUTORESEARCH COMPLETE")
    print(f"{'═' * 55}")
    if scores:
        print(f"  Final Composite: {scores.get('composite_score', '?')}/10")
    print(f"  Results saved to: {RESULTS_DIR}/")


if __name__ == '__main__':
    main()
