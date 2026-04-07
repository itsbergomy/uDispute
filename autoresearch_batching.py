#!/usr/bin/env python3
"""
Autoresearch Loop — o3 Letter Batching Optimization

Tests sequential vs parallel letter generation, compares quality,
measures speedup, and scores the results.

Usage:
  python autoresearch_batching.py                    # Full run (3 accounts)
  python autoresearch_batching.py --sample 5         # Test with N accounts
  python autoresearch_batching.py --dry-run          # Parse + build prompts only
  python autoresearch_batching.py --parallel-only    # Skip sequential baseline
"""

import os
import sys
import json
import argparse
import asyncio
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

from services.pdf_parser import extract_negative_items_from_pdf
from services.letter_generator import (
    PACKS, build_prompt, generate_letter_with_quality_gate,
    generate_letters_batch,
)

CORPUS_DIR = os.path.expanduser("~/Desktop/PDF Corpus")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "autoresearch_results_batching")

judge_client = OpenAI()


# ═══════════════════════════════════════════════════════════
#  Scoring Rubric
# ═══════════════════════════════════════════════════════════

SCORING_PROMPT = """You are a senior software engineer evaluating a parallelization optimization.

Score the following test results on these 6 dimensions (1-10 each):

1. **SPEEDUP** (1-10): How much faster is parallel vs sequential?
   1 = no improvement or slower, 5 = 2x faster, 8 = 3x+ faster, 10 = near-linear scaling

2. **CORRECTNESS** (1-10): Are parallel-generated letters identical quality to sequential?
   1 = quality degraded significantly, 10 = quality identical or better

3. **ERROR HANDLING** (1-10): Did any parallel tasks fail? Were failures isolated (not crashing others)?
   1 = cascade failures, 10 = all succeeded or failures were isolated

4. **QUALITY GATE PASS RATE** (1-10): Did letters pass the quality gate?
   1 = most failed, 10 = all passed

5. **CONSISTENCY** (1-10): Are letter lengths, structures, and scores consistent across the batch?
   1 = highly variable, 10 = consistent quality across all letters

6. **COMPLETENESS** (1-10): Were all requested letters generated (no drops, no None values)?
   1 = letters missing, 10 = all letters generated

Respond in this exact JSON format:
{
  "scores": {
    "speedup": <int>,
    "correctness": <int>,
    "error_handling": <int>,
    "quality_gate_pass_rate": <int>,
    "consistency": <int>,
    "completeness": <int>
  },
  "composite_score": <float>,
  "issues": [
    {"issue": "<description>", "severity": "<critical|high|medium|low>"}
  ],
  "verdict": "<one sentence overall assessment>"
}

TEST RESULTS:
"""


def parse_corpus(sample_limit=3):
    """Parse PDFs and return accounts with context for letter generation."""
    accounts = []
    pdf_files = [f for f in os.listdir(CORPUS_DIR) if f.endswith('.pdf')]

    for pdf_file in sorted(pdf_files):
        filepath = os.path.join(CORPUS_DIR, pdf_file)
        try:
            items = extract_negative_items_from_pdf(filepath)
            for item in items:
                item['_source_pdf'] = pdf_file
                accounts.append(item)
        except Exception as e:
            print(f"  ⚠ Failed to parse {pdf_file}: {e}")

    print(f"\n✓ Parsed {len(accounts)} negative accounts from {len(pdf_files)} PDFs")

    # Prefer accounts with inaccuracies
    with_inac = [a for a in accounts if a.get('inaccuracies')]
    without = [a for a in accounts if not a.get('inaccuracies')]
    sampled = (with_inac + without)[:sample_limit]
    print(f"  Using {len(sampled)} accounts for testing")

    return sampled


def build_test_tasks(accounts):
    """Build prompt tasks for each account (no API calls)."""
    tasks = []
    bureaus = ['Experian', 'Equifax', 'TransUnion']

    for i, account in enumerate(accounts):
        bureau = bureaus[i % len(bureaus)]

        context = {
            'entity': bureau,
            'account_name': account.get('account_name', ''),
            'account_number': account.get('account_number', ''),
            'marks': account.get('status', ''),
            'action': 'Remove this account from my credit report',
            'issue': '; '.join(account.get('inaccuracies', ['Inaccurate reporting'])),
            'client_full_name': 'Test Consumer',
            'client_address': '123 Test Street',
            'client_city_state_zip': 'Test City, TS 00000',
            'today_date': datetime.now().strftime('%B %d, %Y'),
            'dispute_date': 'February 15, 2026',
            'days': '15',
        }

        prompt, has_inac, has_legal = build_prompt(
            'default', 0, context,
            parsed_accounts=[account] if account.get('inaccuracies') else None
        )

        tasks.append({
            'account_id': i,
            'account_name': account.get('account_name', 'Unknown'),
            'bureau': bureau,
            'prompt': prompt,
            'has_inaccuracies': has_inac,
            'has_legal_research': has_legal,
            'quality_context': {
                'account_name': account.get('account_name', ''),
                'account_number': account.get('account_number', ''),
                'bureau': bureau,
                'prompt_pack': 'default',
                'round_number': 1,
                'client_full_name': 'Test Consumer',
                'client_address': '123 Test Street',
            },
        })

    return tasks


def run_sequential(tasks):
    """Generate letters sequentially (baseline)."""
    results = []
    t_start = time.time()

    for task in tasks:
        t0 = time.time()
        try:
            letter_text, qr = generate_letter_with_quality_gate(
                prompt=task['prompt'],
                has_inaccuracies=task.get('has_inaccuracies', False),
                has_legal_research=task.get('has_legal_research', False),
                quality_context=task.get('quality_context'),
            )
            elapsed = time.time() - t0
            results.append({
                'account_name': task['account_name'],
                'bureau': task['bureau'],
                'letter_text': letter_text,
                'letter_length': len(letter_text) if letter_text else 0,
                'quality_passed': qr.passed if qr else False,
                'quality_score': qr.score if qr else 0,
                'quality_failures': qr.failures if qr else [],
                'quality_warnings': qr.warnings if qr else [],
                'elapsed_seconds': round(elapsed, 1),
                'error': None,
            })
            print(f"    ✓ {task['account_name']}/{task['bureau']} — {elapsed:.1f}s, quality={qr.score if qr else '?'}")
        except Exception as e:
            elapsed = time.time() - t0
            results.append({
                'account_name': task['account_name'],
                'bureau': task['bureau'],
                'letter_text': None,
                'letter_length': 0,
                'quality_passed': False,
                'quality_score': 0,
                'elapsed_seconds': round(elapsed, 1),
                'error': str(e),
            })
            print(f"    ✗ {task['account_name']}/{task['bureau']} — ERROR: {e}")

    total_time = time.time() - t_start
    return results, round(total_time, 1)


def run_parallel(tasks):
    """Generate letters in parallel (optimized)."""
    t_start = time.time()
    batch_results = asyncio.run(generate_letters_batch(tasks))
    total_time = time.time() - t_start

    results = []
    for br, task in zip(batch_results, tasks):
        qr = br.get('quality_result')
        results.append({
            'account_name': task['account_name'],
            'bureau': task['bureau'],
            'letter_text': br.get('letter_text'),
            'letter_length': len(br['letter_text']) if br.get('letter_text') else 0,
            'quality_passed': qr.passed if qr else False,
            'quality_score': qr.score if qr else 0,
            'quality_failures': qr.failures if qr else [],
            'quality_warnings': qr.warnings if qr else [],
            'error': br.get('error'),
        })
        status = "✓" if not br.get('error') else "✗"
        name = task['account_name']
        bureau = task['bureau']
        score = qr.score if qr else '?'
        print(f"    {status} {name}/{bureau} — quality={score}")

    return results, round(total_time, 1)


def format_results(seq_results, seq_time, par_results, par_time, n_tasks):
    """Format results for LLM scoring."""
    lines = []
    speedup = seq_time / par_time if par_time > 0 else 0

    lines.append(f"TASK COUNT: {n_tasks} letters")
    lines.append(f"SEQUENTIAL TIME: {seq_time}s")
    lines.append(f"PARALLEL TIME: {par_time}s")
    lines.append(f"SPEEDUP: {speedup:.2f}x")
    lines.append("")

    # Sequential results
    lines.append("=== SEQUENTIAL RESULTS ===")
    for r in seq_results:
        status = "PASS" if r['quality_passed'] else "FAIL"
        elapsed = r.get('elapsed_seconds', '?')
        lines.append(f"  {r['account_name']}/{r['bureau']}: {elapsed}s, quality={r['quality_score']}, gate={status}, len={r['letter_length']}")
        if r['error']:
            lines.append(f"    ERROR: {r['error']}")

    lines.append("")
    lines.append("=== PARALLEL RESULTS ===")
    for r in par_results:
        status = "PASS" if r['quality_passed'] else "FAIL"
        lines.append(f"  {r['account_name']}/{r['bureau']}: quality={r['quality_score']}, gate={status}, len={r['letter_length']}")
        if r['error']:
            lines.append(f"    ERROR: {r['error']}")

    # Quality comparison
    lines.append("")
    lines.append("=== QUALITY COMPARISON ===")
    seq_scores = [r['quality_score'] for r in seq_results if not r['error']]
    par_scores = [r['quality_score'] for r in par_results if not r['error']]
    seq_avg = sum(seq_scores) / len(seq_scores) if seq_scores else 0
    par_avg = sum(par_scores) / len(par_scores) if par_scores else 0
    lines.append(f"  Sequential avg quality: {seq_avg:.1f}")
    lines.append(f"  Parallel avg quality: {par_avg:.1f}")
    lines.append(f"  Quality delta: {par_avg - seq_avg:+.1f}")

    seq_pass = sum(1 for r in seq_results if r['quality_passed'])
    par_pass = sum(1 for r in par_results if r['quality_passed'])
    lines.append(f"  Sequential pass rate: {seq_pass}/{len(seq_results)}")
    lines.append(f"  Parallel pass rate: {par_pass}/{len(par_results)}")

    seq_errors = sum(1 for r in seq_results if r['error'])
    par_errors = sum(1 for r in par_results if r['error'])
    lines.append(f"  Sequential errors: {seq_errors}")
    lines.append(f"  Parallel errors: {par_errors}")

    # Completeness
    seq_complete = sum(1 for r in seq_results if r['letter_text'])
    par_complete = sum(1 for r in par_results if r['letter_text'])
    lines.append(f"  Sequential completed: {seq_complete}/{len(seq_results)}")
    lines.append(f"  Parallel completed: {par_complete}/{len(par_results)}")

    return "\n".join(lines)


def score_results(results_text):
    """Score with LLM judge."""
    response = judge_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a software engineering expert. Respond only in valid JSON."},
            {"role": "user", "content": SCORING_PROMPT + results_text}
        ],
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return None


def print_summary(scores, speedup):
    """Print formatted summary."""
    if not scores:
        print("  ⚠ Scoring failed")
        return

    print(f"\n{'═' * 60}")
    print(f"AUTORESEARCH — o3 BATCHING OPTIMIZATION SCORES")
    print(f"{'═' * 60}")
    print(f"\n  Speedup: {speedup:.2f}x")
    print(f"  Composite: {scores.get('composite_score', '?')}/10\n")

    for dim, score in sorted(scores.get('scores', {}).items(), key=lambda x: x[1], reverse=True):
        bar = '█' * int(score) + '░' * (10 - int(score))
        print(f"  {dim:.<32s} {score}/10  {bar}")

    issues = scores.get('issues', [])
    if issues:
        print(f"\n  Issues:")
        for issue in issues:
            print(f"    [{issue.get('severity', '?').upper()}] {issue.get('issue', '')}")

    print(f"\n  Verdict: {scores.get('verdict', 'N/A')}")
    print(f"{'═' * 60}")


def save_results(seq_results, seq_time, par_results, par_time, scores, n_tasks):
    """Save to JSON."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # Don't save full letter text (too large) — save metadata only
    def _strip(results):
        return [{k: v for k, v in r.items() if k != 'letter_text'} for r in results]

    output = {
        "timestamp": timestamp,
        "n_tasks": n_tasks,
        "sequential_time": seq_time,
        "parallel_time": par_time,
        "speedup": round(seq_time / par_time, 2) if par_time > 0 else 0,
        "sequential_results": _strip(seq_results),
        "parallel_results": _strip(par_results),
        "scores": scores,
    }

    filepath = os.path.join(RESULTS_DIR, f'batching_{timestamp}.json')
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✓ Results saved to {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description='Autoresearch — o3 Batching Optimization')
    parser.add_argument('--sample', type=int, default=3, help='Number of accounts to test')
    parser.add_argument('--dry-run', action='store_true', help='Parse + build prompts only')
    parser.add_argument('--parallel-only', action='store_true', help='Skip sequential baseline')
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════╗")
    print("║  AUTORESEARCH — o3 Batching Optimization             ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Sample size: {args.sample:<38d} ║")
    print("╚══════════════════════════════════════════════════════╝")

    # Step 1: Parse corpus
    print("\n[1/5] Parsing PDF corpus...")
    accounts = parse_corpus(sample_limit=args.sample)
    if not accounts:
        print("✗ No accounts found.")
        return

    # Step 2: Build prompts
    print(f"\n[2/5] Building prompts...")
    tasks = build_test_tasks(accounts)
    print(f"  Built {len(tasks)} tasks")

    if args.dry_run:
        print("\n  Dry run — stopping before generation.")
        return

    # Step 3: Sequential baseline
    seq_results, seq_time = [], 0
    if not args.parallel_only:
        print(f"\n[3/5] Sequential baseline ({len(tasks)} letters)...")
        seq_results, seq_time = run_sequential(tasks)
        print(f"  Total: {seq_time}s")
    else:
        print(f"\n[3/5] Skipping sequential baseline")

    # Step 4: Parallel generation
    print(f"\n[4/5] Parallel generation ({len(tasks)} letters)...")
    par_results, par_time = run_parallel(tasks)
    print(f"  Total: {par_time}s")

    if seq_time > 0:
        speedup = seq_time / par_time if par_time > 0 else 0
        print(f"\n  ⚡ SPEEDUP: {speedup:.2f}x ({seq_time}s → {par_time}s)")
    else:
        speedup = 0

    # Step 5: Score
    print(f"\n[5/5] Scoring with LLM judge...")
    if seq_results:
        results_text = format_results(seq_results, seq_time, par_results, par_time, len(tasks))
    else:
        results_text = format_results(par_results, par_time, par_results, par_time, len(tasks))

    scores = score_results(results_text)
    print_summary(scores, speedup)
    save_results(seq_results, seq_time, par_results, par_time, scores, len(tasks))

    print(f"\n{'═' * 55}")
    print("AUTORESEARCH COMPLETE")
    print(f"{'═' * 55}")


if __name__ == '__main__':
    main()
