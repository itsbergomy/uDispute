# Autoresearch Scoring Results — Prompt Pack Optimization

**Date:** March 20-21, 2026
**Model:** o3 (letter generation) + GPT-4o (scoring judge)
**Parser:** Upgraded PDF parser with inaccuracy detection
**Corpus:** Real credit reports (Experian, TransUnion formats)
**Methodology:** Generate letters from parsed accounts with detected inaccuracies, score each on 7 dimensions (1-10), composite average across 3 letters per pack.

---

## Scoring Rubric (7 Dimensions)

| # | Dimension | What It Measures |
|---|-----------|-----------------|
| 1 | **E-OSCAR Resistance** | Can a CRA clerk reduce this to a single e-OSCAR code in 4 minutes? |
| 2 | **Field-Specific Verification** | How many specific Metro 2 data fields does it demand verification of? |
| 3 | **Legal Citation Density** | Number of specific statutes, case law, and CFPB citations correctly applied |
| 4 | **Deadline & Consequences** | Clear deadlines with specific statutory remedies for non-compliance |
| 5 | **DNR Trap Potential** | Demands documents the furnisher is unlikely to have (forces auto-delete) |
| 6 | **Personalization** | Specific to THIS account's parsed inaccuracies vs generic boilerplate |
| 7 | **Professional Tone** | Reads like a consumer attorney's letter, not threatening or rambling |

---

## Results Summary

### Composite Scores by Pack

| Pack | Composite | Verdict |
|------|-----------|---------|
| **ACDV Response** | **8.7/10** | Highest overall — strong on field verification and DNR traps |
| **Arbitration** | **8.7/10** | Tied highest — strong personalization and deadline consequences |
| **Default** | **8.5/10** | Solid baseline — strong professional tone and field verification |
| **Consumer Law** | **8.5/10** | Tied baseline — highest legal citation density |

### Detailed Dimension Scores

| Dimension | Default | Arbitration | Consumer Law | ACDV Response |
|-----------|---------|-------------|--------------|---------------|
| E-OSCAR Resistance | 8.0 | 8.0 | 8.0 | **8.7** |
| Field Verification | **9.0** | 8.0 | 8.7 | **9.0** |
| Legal Citations | **9.0** | **9.0** | **9.0** | **9.0** |
| Deadline & Consequences | 8.0 | **9.0** | 8.7 | 8.7 |
| DNR Trap Potential | 8.3 | 8.7 | **9.0** | **9.0** |
| Personalization | 8.3 | **9.0** | 8.0 | 8.0 |
| Professional Tone | **9.0** | **9.0** | 8.3 | 8.7 |

### Strengths by Pack

- **Default:** Professional tone (9.0), field verification (9.0), legal citations (9.0). Best all-rounder for Round 1 disputes.
- **Arbitration:** Personalization (9.0), deadline/consequences (9.0), professional tone (9.0). Strongest for escalation — account-specific with real legal teeth.
- **Consumer Law:** DNR trap potential (9.0), legal citations (9.0). Heaviest on statutory authority and document demands.
- **ACDV Response:** E-OSCAR resistance (8.7), field verification (9.0), DNR traps (9.0). Best at defeating e-OSCAR code compression.

### Weakest Dimension Across All Packs

**E-OSCAR Resistance (8.0 avg)** — Despite anti-compression rules in the prompts, letters are still vulnerable to being collapsed into e-OSCAR codes 001, 103, or 106 by time-pressed CRA clerks. The ACDV Response pack handles this best at 8.7.

---

## Common Weaknesses Identified

### 1. E-OSCAR Code Compression
All packs showed vulnerability to code 103 ("Information inaccurate"). CRA clerks can still collapse multi-point disputes into a single code.
- **Mitigation applied:** Added Rule 2 to e-OSCAR intelligence block — "Structure as 3+ labeled DISPUTE POINTS targeting different field categories"

### 2. Placeholder Leakage
Placeholders like "$[BALANCE]" and "March ____" occasionally appeared, weakening personalization.
- **Mitigation applied:** Inaccuracy parser now feeds real values directly into prompts

### 3. FDCPA Misapplication
Consumer Law pack incorrectly cited FDCPA against original creditors (Bank of America, Capital One) who are not "debt collectors" under § 1692a(6).
- **Mitigation applied:** Added FDCPA Guard Rule (Rule 9) — "FDCPA (15 USC 1692) applies ONLY to third-party debt collectors, NEVER cite against original creditors"

### 4. Evidence Fabrication
Arbitration pack referenced documents the consumer never provided (1099-C forms, chat logs, payment receipts).
- **Mitigation applied:** Added Evidence Integrity Rule (Rule 10) — "NEVER reference documents the consumer has not provided"

### 5. Deadline Misalignment
Letters set 15-day deadlines when FCRA allows 30 days, giving bureaus grounds to ignore.
- **Mitigation applied:** Prompts now align with § 1681i(a)(1) 30-day window

---

## Post-Optimization Changes Applied

1. **e-OSCAR Intelligence Block** — Compressed from ~1,600 to ~399 tokens. Deduplicated instructions. Added Rules 9 (FDCPA guard) and 10 (evidence integrity).
2. **All Pack Templates** — Compressed ~75%. Heaviest prompt reduced from 4,500 to 818 tokens. Removes redundancy with e-OSCAR block.
3. **Model Switch** — Moved from GPT-4o to o3 for letter generation. o3 reasons better with concise prompts.
4. **Inaccuracy Pipeline** — Parsed inaccuracies from PDF now flow directly into letter prompts for all packs including the autonomous Business pipeline.

---

## Test Accounts Used

| Account | Bureau | Inaccuracies Detected |
|---------|--------|-----------------------|
| BANK OF AMERICA | Experian | 2 (status mismatch, balance inconsistency) |
| BRIDGECREST | Experian | 3 (charge-off status, payment history, DOFD) |
| EDFINANCIAL SERVICES | Experian | 2 (missing fields, incomplete information) |

---

## Raw Data Location

Full JSON scoring results with per-letter breakdowns:
`~/Desktop/PDF Corpus/autoresearch_results/`

- `prompt_scores_iter0_20260320_204448.json` — Default pack
- `prompt_scores_iter0_20260320_205946.json` — Arbitration pack
- `prompt_scores_iter0_20260321_170750.json` — ACDV Response pack
- `prompt_scores_iter0_20260321_170847.json` — Consumer Law pack
