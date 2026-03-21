#!/usr/bin/env python3
"""
Autoresearch Loop — Prompt Optimization

Generates dispute letters from real parsed accounts, scores them on
e-OSCAR resistance and legal effectiveness, then proposes improvements.

Usage:
  python autoresearch_prompts.py                    # Full run (all PDFs, all packs)
  python autoresearch_prompts.py --pack default     # Single pack
  python autoresearch_prompts.py --sample 3         # Limit to N accounts
  python autoresearch_prompts.py --dry-run          # Score only, no improvement proposals
  python autoresearch_prompts.py --iterations 3     # Run N improvement cycles
"""

import os
import sys
import json
import argparse
import time
from datetime import datetime
from openai import OpenAI

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.pdf_parser import extract_negative_items_from_pdf
from services.letter_generator import (
    PACKS, PACK_INFO, generate_letter, build_prompt,
    SYSTEM_PROMPT_BASE, SYSTEM_PROMPT_WITH_INACCURACIES,
    _E_OSCAR_INTELLIGENCE,
)

CORPUS_DIR = os.path.expanduser("~/Desktop/PDF Corpus")
RESULTS_DIR = os.path.join(CORPUS_DIR, "autoresearch_results")

client = OpenAI()


# ─── Scoring Rubric ───

SCORING_PROMPT = """You are an expert credit dispute attorney and e-OSCAR systems analyst.
Score the following dispute letter on these 7 dimensions (1-10 each):

1. **E-OSCAR RESISTANCE** (1-10): Can this letter be reduced to a single e-OSCAR
   dispute code (001, 103, etc.) by a CRA employee in 4 minutes? 1 = easily reduced
   to a code, 10 = impossible to simplify without losing critical arguments.

2. **FIELD-SPECIFIC VERIFICATION** (1-10): How many specific data fields does the
   letter demand verification of? 1 = generic "verify this account", 10 = demands
   verification of 6+ specific fields (date of first delinquency, balance, payment
   history months, charge-off date, original creditor, credit limit, etc.)

3. **LEGAL CITATION DENSITY** (1-10): How many specific statutes, regulations, CFPB
   circulars, and case law citations are included? 1 = none, 10 = 5+ specific citations
   with section numbers that are correctly applied.

4. **DEADLINE & CONSEQUENCES** (1-10): Does the letter set clear deadlines with
   specific legal consequences for non-compliance? 1 = no deadline, 10 = specific
   day count + specific statutory remedy (deletion under § 1681i(a)(5)(A), damages
   under § 1681n, etc.)

5. **DNR TRAP POTENTIAL** (1-10): Does the letter demand documentation that the
   furnisher is unlikely to have (signed credit application, complete payment ledger,
   chain of assignment for collections)? 1 = no documentation demands, 10 = demands
   multiple documents that force a DNR auto-delete if not produced.

6. **PERSONALIZATION** (1-10): Is the letter specific to THIS account's situation
   (account name, number, specific status, detected inaccuracies) vs generic boilerplate?
   1 = pure template, 10 = deeply personalized with account-specific facts.

7. **PROFESSIONAL TONE** (1-10): Is the letter professionally written, clear, and
   authoritative without being threatening or rambling? 1 = poor quality or threatening,
   10 = reads like a consumer attorney's letter.

IMPORTANT: Also identify the TOP 3 WEAKNESSES of this letter and suggest specific
improvements for each.

Respond in this exact JSON format:
{
  "scores": {
    "e_oscar_resistance": <int>,
    "field_verification": <int>,
    "legal_citations": <int>,
    "deadline_consequences": <int>,
    "dnr_trap_potential": <int>,
    "personalization": <int>,
    "professional_tone": <int>
  },
  "composite_score": <float>,
  "weaknesses": [
    {"weakness": "<description>", "improvement": "<specific fix>"},
    {"weakness": "<description>", "improvement": "<specific fix>"},
    {"weakness": "<description>", "improvement": "<specific fix>"}
  ],
  "e_oscar_code_vulnerability": "<which e-OSCAR code could this be reduced to, or 'NONE'>",
  "verdict": "<one sentence overall assessment>"
}

THE LETTER TO SCORE:
"""

IMPROVEMENT_PROMPT = """You are an expert credit dispute attorney and prompt engineer.

Based on the scoring results below, rewrite the SYSTEM PROMPT to address the identified
weaknesses. The system prompt instructs GPT to generate dispute letters.

CURRENT SYSTEM PROMPT:
{current_prompt}

SCORING RESULTS FROM {num_letters} LETTERS:
{scoring_summary}

TOP RECURRING WEAKNESSES:
{weaknesses}

REQUIREMENTS:
1. Keep the e-OSCAR intelligence block — it's working. Only refine it if scores suggest issues.
2. Address each recurring weakness with specific new instructions.
3. Do NOT make the prompt longer than necessary — be precise.
4. The prompt must work for ALL prompt packs (default, arbitration, consumer_law, ACDV_response).
5. Return ONLY the new system prompt text — no explanation, no markdown, no code blocks.
"""


def parse_corpus(sample_limit=None):
    """Parse all PDFs in corpus and return accounts with inaccuracies."""
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

    # Show inaccuracy stats
    with_inac = sum(1 for a in accounts if a.get('inaccuracies'))
    total_inac = sum(len(a.get('inaccuracies', [])) for a in accounts)
    print(f"  {with_inac}/{len(accounts)} accounts have detected inaccuracies ({total_inac} total)")

    if sample_limit and len(accounts) > sample_limit:
        # Prefer accounts with inaccuracies for better testing
        with_inac_list = [a for a in accounts if a.get('inaccuracies')]
        without = [a for a in accounts if not a.get('inaccuracies')]
        sampled = with_inac_list[:sample_limit]
        if len(sampled) < sample_limit:
            sampled += without[:sample_limit - len(sampled)]
        accounts = sampled
        print(f"  Sampled {len(accounts)} accounts for testing")

    return accounts


def generate_test_letter(account, pack_key, template_idx=0):
    """Generate a dispute letter for scoring."""
    context = {
        'entity': 'Experian',  # Default for testing
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
        pack_key, template_idx, context,
        parsed_accounts=[account] if account.get('inaccuracies') else None
    )

    letter = generate_letter(
        prompt,
        model="o3",
        has_inaccuracies=has_inac,
        has_legal_research=has_legal
    )

    return letter, prompt


def score_letter(letter_text):
    """Score a generated letter using GPT-4o as judge."""
    response = client.chat.completions.create(
        model="o3",
        messages=[
            {"role": "system", "content": "You are a credit dispute letter scoring expert. Respond only in valid JSON."},
            {"role": "user", "content": SCORING_PROMPT + letter_text}
        ],
        response_format={"type": "json_object"},
    )

    try:
        return json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return None


def run_scoring_pass(accounts, pack_keys, template_idx=0):
    """Generate and score letters for all accounts across specified packs."""
    results = []

    for pack_key in pack_keys:
        templates = PACKS.get(pack_key, PACKS['default'])
        num_templates = len(templates)
        t_idx = min(template_idx, num_templates - 1)

        print(f"\n── Pack: {pack_key} (template {t_idx + 1}/{num_templates}) ──")

        for i, account in enumerate(accounts):
            acct_name = account.get('account_name', 'Unknown')
            inac_count = len(account.get('inaccuracies', []))
            print(f"  [{i+1}/{len(accounts)}] {acct_name} ({inac_count} inaccuracies)...", end=' ', flush=True)

            try:
                letter, prompt = generate_test_letter(account, pack_key, t_idx)
                scores = score_letter(letter)

                if scores:
                    composite = scores.get('composite_score', 0)
                    if not composite:
                        s = scores.get('scores', {})
                        composite = sum(s.values()) / len(s) if s else 0
                        scores['composite_score'] = round(composite, 1)

                    print(f"Score: {composite:.1f}/10 | e-OSCAR: {scores['scores'].get('e_oscar_resistance', '?')}")

                    results.append({
                        'pack_key': pack_key,
                        'template_idx': t_idx,
                        'account_name': acct_name,
                        'account_number': account.get('account_number', ''),
                        'source_pdf': account.get('_source_pdf', ''),
                        'inaccuracy_count': inac_count,
                        'scores': scores,
                        'letter_length': len(letter),
                        'letter_preview': letter[:500],
                    })
                else:
                    print("⚠ Scoring failed")

            except Exception as e:
                print(f"✗ Error: {e}")

            # Rate limiting
            time.sleep(0.5)

    return results


def analyze_results(results):
    """Aggregate scoring results and identify patterns."""
    if not results:
        return {}

    # Aggregate by pack
    by_pack = {}
    for r in results:
        pk = r['pack_key']
        if pk not in by_pack:
            by_pack[pk] = []
        by_pack[pk].append(r)

    summary = {}
    for pack_key, pack_results in by_pack.items():
        scores_list = [r['scores']['scores'] for r in pack_results if r.get('scores')]
        if not scores_list:
            continue

        dims = list(scores_list[0].keys())
        avg_scores = {}
        for dim in dims:
            vals = [s[dim] for s in scores_list if dim in s]
            avg_scores[dim] = round(sum(vals) / len(vals), 1) if vals else 0

        composites = [r['scores']['composite_score'] for r in pack_results if r.get('scores')]
        avg_composite = round(sum(composites) / len(composites), 1) if composites else 0

        # Collect all weaknesses
        all_weaknesses = []
        for r in pack_results:
            if r.get('scores', {}).get('weaknesses'):
                all_weaknesses.extend(r['scores']['weaknesses'])

        # e-OSCAR vulnerabilities
        vuln_codes = [r['scores'].get('e_oscar_code_vulnerability', 'Unknown')
                      for r in pack_results if r.get('scores')]

        summary[pack_key] = {
            'num_letters': len(pack_results),
            'avg_composite': avg_composite,
            'avg_scores': avg_scores,
            'weaknesses': all_weaknesses,
            'e_oscar_vulnerabilities': vuln_codes,
            'lowest_dimension': min(avg_scores, key=avg_scores.get) if avg_scores else None,
            'highest_dimension': max(avg_scores, key=avg_scores.get) if avg_scores else None,
        }

    return summary


def print_summary(summary):
    """Print a formatted summary of results."""
    print("\n" + "=" * 70)
    print("AUTORESEARCH PROMPT SCORING SUMMARY")
    print("=" * 70)

    for pack_key, data in summary.items():
        print(f"\n┌── {pack_key.upper()} PACK ({data['num_letters']} letters) ──")
        print(f"│  Composite Score: {data['avg_composite']}/10")
        print(f"│")
        print(f"│  Dimension Scores:")
        for dim, score in sorted(data['avg_scores'].items(), key=lambda x: x[1], reverse=True):
            bar = '█' * int(score) + '░' * (10 - int(score))
            flag = ' ← WEAKEST' if dim == data['lowest_dimension'] else ''
            flag = ' ← STRONGEST' if dim == data['highest_dimension'] else flag
            print(f"│    {dim:.<30s} {score:.1f}/10  {bar}{flag}")

        print(f"│")
        print(f"│  e-OSCAR Vulnerabilities:")
        vuln_counts = {}
        for v in data['e_oscar_vulnerabilities']:
            vuln_counts[v] = vuln_counts.get(v, 0) + 1
        for code, count in sorted(vuln_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"│    {code}: {count}x")

        # Top 3 most common weakness themes
        if data['weaknesses']:
            print(f"│")
            print(f"│  Top Weaknesses:")
            seen = set()
            shown = 0
            for w in data['weaknesses']:
                desc = w.get('weakness', '')[:80]
                if desc not in seen and shown < 5:
                    print(f"│    • {desc}")
                    seen.add(desc)
                    shown += 1

        print(f"└{'─' * 50}")


def propose_improvements(summary, current_prompt):
    """Use GPT to propose prompt improvements based on scoring data."""
    # Build scoring summary text
    scoring_text = ""
    all_weaknesses = []
    for pack_key, data in summary.items():
        scoring_text += f"\n{pack_key.upper()} PACK (avg {data['avg_composite']}/10):\n"
        for dim, score in data['avg_scores'].items():
            scoring_text += f"  {dim}: {score}/10\n"
        all_weaknesses.extend(data.get('weaknesses', []))

    # Deduplicate and rank weaknesses
    weakness_counts = {}
    for w in all_weaknesses:
        desc = w.get('weakness', '')
        if desc:
            weakness_counts[desc] = weakness_counts.get(desc, 0) + 1

    top_weaknesses = sorted(weakness_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    weakness_text = "\n".join(f"  ({count}x) {desc}" for desc, count in top_weaknesses)

    num_total = sum(d['num_letters'] for d in summary.values())

    prompt = IMPROVEMENT_PROMPT.format(
        current_prompt=current_prompt,
        num_letters=num_total,
        scoring_summary=scoring_text,
        weaknesses=weakness_text,
    )

    response = client.chat.completions.create(
        model="o3",
        messages=[
            {"role": "system", "content": "You are an expert prompt engineer specializing in credit dispute letter generation."},
            {"role": "user", "content": prompt}
        ],
    )

    return response.choices[0].message.content


def save_results(results, summary, iteration=0):
    """Save results to JSON for later analysis."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    output = {
        'timestamp': timestamp,
        'iteration': iteration,
        'summary': summary,
        'detailed_results': results,
    }

    filepath = os.path.join(RESULTS_DIR, f'prompt_scores_iter{iteration}_{timestamp}.json')
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✓ Results saved to {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description='Autoresearch Prompt Optimization Loop')
    parser.add_argument('--pack', type=str, help='Test a single pack (default, arbitration, consumer_law, ACDV_response)')
    parser.add_argument('--sample', type=int, default=5, help='Number of accounts to test (default: 5)')
    parser.add_argument('--iterations', type=int, default=1, help='Number of improvement cycles (default: 1)')
    parser.add_argument('--dry-run', action='store_true', help='Score only, no improvement proposals')
    parser.add_argument('--template', type=int, default=0, help='Template index within pack (default: 0)')
    args = parser.parse_args()

    pack_keys = [args.pack] if args.pack else list(PACKS.keys())

    print("╔══════════════════════════════════════════════╗")
    print("║  AUTORESEARCH — Prompt Optimization Loop     ║")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Packs: {', '.join(pack_keys):<37s} ║")
    print(f"║  Sample size: {args.sample:<31d} ║")
    print(f"║  Iterations: {args.iterations:<32d} ║")
    print("╚══════════════════════════════════════════════╝")

    # Step 1: Parse corpus
    print("\n[1/4] Parsing PDF corpus...")
    accounts = parse_corpus(sample_limit=args.sample)

    if not accounts:
        print("✗ No accounts found. Check your PDF Corpus directory.")
        return

    for iteration in range(args.iterations):
        print(f"\n{'=' * 50}")
        print(f"  ITERATION {iteration + 1}/{args.iterations}")
        print(f"{'=' * 50}")

        # Step 2: Generate and score letters
        print(f"\n[2/4] Generating and scoring letters...")
        results = run_scoring_pass(accounts, pack_keys, args.template)

        # Step 3: Analyze
        print(f"\n[3/4] Analyzing results...")
        summary = analyze_results(results)
        print_summary(summary)

        # Save results
        save_results(results, summary, iteration)

        # Step 4: Propose improvements (unless dry-run or last iteration)
        if not args.dry_run and iteration < args.iterations - 1:
            print(f"\n[4/4] Proposing improvements...")
            current_prompt = SYSTEM_PROMPT_WITH_INACCURACIES
            improved = propose_improvements(summary, current_prompt)

            print(f"\n{'─' * 50}")
            print("PROPOSED SYSTEM PROMPT IMPROVEMENTS:")
            print(f"{'─' * 50}")
            print(improved[:2000])
            if len(improved) > 2000:
                print(f"\n... ({len(improved) - 2000} more characters)")

            # Save the proposed improvement
            improvement_path = os.path.join(RESULTS_DIR, f'proposed_prompt_iter{iteration + 1}.txt')
            with open(improvement_path, 'w') as f:
                f.write(improved)
            print(f"\n✓ Proposed prompt saved to {improvement_path}")

        elif args.dry_run:
            print(f"\n[4/4] Dry run — skipping improvement proposals")
        else:
            print(f"\n[4/4] Final iteration — generating improvement proposal...")
            current_prompt = SYSTEM_PROMPT_WITH_INACCURACIES
            improved = propose_improvements(summary, current_prompt)

            improvement_path = os.path.join(RESULTS_DIR, f'proposed_prompt_final.txt')
            with open(improvement_path, 'w') as f:
                f.write(improved)
            print(f"\n✓ Final proposed prompt saved to {improvement_path}")

    # Final summary
    print(f"\n{'═' * 50}")
    print("AUTORESEARCH COMPLETE")
    print(f"{'═' * 50}")

    all_composites = [r['scores']['composite_score'] for r in results if r.get('scores')]
    if all_composites:
        avg = sum(all_composites) / len(all_composites)
        lo = min(all_composites)
        hi = max(all_composites)
        print(f"  Average Score: {avg:.1f}/10")
        print(f"  Range: {lo:.1f} - {hi:.1f}")
        print(f"  Letters Scored: {len(all_composites)}")

    print(f"\n  Results saved to: {RESULTS_DIR}/")


if __name__ == '__main__':
    main()
