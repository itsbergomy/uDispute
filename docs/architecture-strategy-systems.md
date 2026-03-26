# Credit OS — Strategy Systems Architecture
## CFPB AI Narratives + Agentic Pipeline Response System

---

## SYSTEM 1: CFPB AI-Generated Narratives

### Current Flow (Static)
```
User clicks "CFPB Fast Track" on tier2_issues
    ↓
/cfpb-wizard (GET) — reads account from session
    ↓
cfpb_wizard.html — 5-step wizard
    ↓
Step 2: 3 PRE-WRITTEN narratives (same for everyone)
    ├── Validation Violation (generic)
    ├── Deceptive Practices (generic)
    └── Demand/Closing Statement (generic)
    ↓
User copies text → pastes into CFPB.gov manually
```

### Target Flow (AI-Personalized)
```
User clicks "CFPB Fast Track"
    ↓
/cfpb-wizard (GET) — reads account + inaccuracies from session
    ↓
BEFORE rendering wizard, system calls:
    ├── cfpb_search.py → get complaint stats for this creditor
    ├── legal_research.py → get FCRA citations + case law
    └── letter history → get prior dispute dates, outcomes, round #
    ↓
AI generates 3 PERSONALIZED narratives via o3:
    ├── Narrative 1: "Investigation Failure"
    │   Uses: specific inaccuracies, dispute dates, bureau name,
    │   FCRA 1681i(a)(7) citation, CFPB v. Experian reference
    │
    ├── Narrative 2: "Pattern of Violations"
    │   Uses: CFPB complaint count for this creditor, win rate,
    │   matching issue types, "X other consumers reported same issue"
    │
    └── Narrative 3: "Statutory Damages Demand"
    │   Uses: prior round history, all dispute dates, accumulated
    │   violations, willful noncompliance argument
    ↓
cfpb_wizard.html renders with AI narratives (user can still edit)
    ↓
User copies → pastes into CFPB.gov
```

### What Needs to Be Built
```
┌─────────────────────────────────────────────────────┐
│  services/cfpb_narrative_generator.py  [NEW FILE]   │
│                                                     │
│  generate_cfpb_narratives(                          │
│      account_name,                                  │
│      account_number,                                │
│      bureau,                                        │
│      inaccuracies[],        ← from parser           │
│      dispute_history[],     ← prior letters/rounds  │
│      cfpb_data{},           ← from cfpb_search.py   │
│      legal_research{},      ← from legal_research.py│
│  ) → [narrative_1, narrative_2, narrative_3]         │
│                                                     │
│  Uses: o3 with a CFPB-specific system prompt        │
│  Cost: 1 API call per wizard open                   │
│  Fallback: static narratives if API fails           │
└─────────────────────────────────────────────────────┘

Changes to existing files:
  blueprints/disputes.py
    └── /cfpb-wizard route: call generate_cfpb_narratives()
        before rendering, pass AI narratives to template
        with fallback to static if generation fails

  templates/cfpb_wizard.html
    └── No changes needed — narratives already rendered
        from template variables. Just receives better data.
```

### Data Flow Diagram
```
                    ┌──────────────┐
                    │  PDF Parser  │
                    │  (existing)  │
                    └──────┬───────┘
                           │ inaccuracies[]
                           ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  CFPB Search │    │    CFPB      │    │    Legal     │
│    API       │───▶│  Narrative   │◀───│  Research    │
│  (existing)  │    │  Generator   │    │  (existing)  │
└──────────────┘    │   [NEW]      │    └──────────────┘
  cfpb_data{}       └──────┬───────┘      legal_research{}
                           │
                    ┌──────┴───────┐
                    │  Dispute     │
                    │  History     │
                    │  (DB query)  │
                    └──────┬───────┘
                           │ prior letters, outcomes
                           ▼
                    ┌──────────────┐
                    │  3 AI        │
                    │  Narratives  │
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  CFPB Wizard │
                    │  (existing)  │
                    └──────────────┘
```

---

## SYSTEM 2: Agentic Pipeline Response System

### Current Flow (Semi-Automated)
```
Pipeline delivers letters → state: awaiting_response
    ↓
User manually uploads response files
    ↓
upload_response() classifies outcome + runs auto-research
    ↓
Business rules evaluate on response_received trigger
    ├── If auto_escalate rule matches → jump to strategy
    └── If no rule matches → continue
    ↓
When ALL accounts respond → state: response_received
    ↓
handle_response_received()
    ├── Updates creditor intelligence
    ├── Evaluates round_completed rules
    └── Transitions to round_review (HARD PAUSE)
    ↓
██ USER MUST MANUALLY START NEXT ROUND ██
    ↓
User clicks "Start Round 2" → strategy → generation → review → delivery
```

### Target Flow (Fully Agentic for Business Auto Mode)
```
Pipeline delivers letters → state: awaiting_response
    ↓
User uploads response files (single or batch)
    ↓
upload_response() classifies outcome + runs auto-research
    ↓
Business rules evaluate on response_received trigger
    ↓
When ALL accounts respond → state: response_received
    ↓
handle_response_received()
    ├── Updates creditor intelligence
    ├── Checks: is mode == 'full_auto'?
    │
    ├── IF FULL AUTO + unresolved accounts exist:
    │   ├── Increment round_number
    │   ├── Use escalation_engine.recommend_escalation()
    │   │   for each unresolved account (per-account pack)
    │   ├── Skip round_review entirely
    │   ├── Return 'strategy' → auto-advance
    │   └── Pipeline generates + delivers next round automatically
    │
    └── IF SUPERVISED or all resolved:
        ├── Hard pause at round_review (existing behavior)
        └── User reviews outcomes, picks strategy, starts next round
    ↓
For FULL AUTO: Pipeline loops until:
    ├── All accounts removed/updated → state: completed ✅
    ├── Max rounds exhausted → state: completed (with summary)
    └── Critical failure → state: failed (with error)
```

### What Needs to Be Built
```
┌─────────────────────────────────────────────────────┐
│  CHANGE: services/pipeline_engine.py                │
│                                                     │
│  handle_response_received() — add auto mode check:  │
│                                                     │
│  agent_config = _get_agent_config(pipeline)          │
│  mode = agent_config.get('mode', 'supervised')       │
│                                                     │
│  if mode == 'full_auto' and has_unresolved:          │
│      # Auto-escalate without user intervention       │
│      pipeline.round_number += 1                      │
│      for each unresolved account:                    │
│          rec = recommend_escalation(...)              │
│          account.template_pack = rec['pack']          │
│      return 'strategy'  # skip round_review          │
│                                                     │
│  # else: existing behavior (round_review pause)      │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  CHANGE: services/pipeline_engine.py                │
│                                                     │
│  handle_strategy() — already supports Round 2+      │
│  with intelligent pack selection. No changes needed. │
│  It already filters to unresolved accounts and      │
│  queries escalation_engine for pack recommendations.│
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  CHANGE: services/pipeline_engine.py                │
│                                                     │
│  handle_generation() — already generates letters    │
│  for all accounts in current round. No changes.     │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  CHANGE: services/pipeline_engine.py                │
│                                                     │
│  handle_review() — add auto-approve for full_auto:  │
│                                                     │
│  if mode == 'full_auto':                             │
│      return 'delivery'  # skip review pause          │
│  else:                                               │
│      return 'review'    # existing supervised pause  │
└─────────────────────────────────────────────────────┘
```

### Full Auto Loop Diagram
```
                    ┌──────────────────────────┐
                    │     ROUND 1              │
                    │  (User starts pipeline)  │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │  intake → strategy →     │
                    │  generation → review →   │
                    │  delivery                │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │   awaiting_response      │
                    │   (letters in the mail)   │
                    └────────────┬─────────────┘
                                 │
                         responses uploaded
                                 │
                    ┌────────────▼─────────────┐
                    │   response_received      │
                    │                          │
                    │   All removed? ──YES──▶ COMPLETED ✅
                    │        │                 │
                    │       NO                 │
                    │        │                 │
                    │   Max rounds? ──YES──▶ COMPLETED ⚠️
                    │        │                 │
                    │       NO                 │
                    │        │                 │
                    │   Mode?                  │
                    │   ├── full_auto ──────────┤
                    │   │   auto-escalate       │
                    │   │   recommend packs     │
                    │   │   skip round_review   │
                    │   │        │              │
                    │   └── supervised          │
                    │       round_review PAUSE  │
                    │       (user decides)      │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │     ROUND 2+             │
                    │  strategy (unresolved    │
                    │  accounts only, new      │
                    │  packs from escalation   │
                    │  engine) → generation →  │
                    │  review* → delivery      │
                    │                          │
                    │  *review skipped in      │
                    │   full_auto mode         │
                    └────────────┬─────────────┘
                                 │
                            loops back to
                         awaiting_response
                                 │
                          (repeat until
                        resolved or max
                          rounds hit)
```

---

## SYSTEM 3: Letter Quality Gate

### Problem
Auto mode generates letters without human review. Bad letters damage credibility —
fabricated evidence, wrong legal citations, missing dispute points, FDCPA cited
against original creditors. Need a quality check before any letter is sent.

### Design
```
Letter Generated (o3)
    ↓
┌──────────────────────────────────────────────────────┐
│           QUALITY GATE                                │
│           services/letter_quality_gate.py              │
│                                                       │
│  Rule 1: Account Accuracy                             │
│  ├── Account name appears in letter body              │
│  ├── Account number appears in letter body            │
│  └── Bureau/creditor name matches recipient           │
│                                                       │
│  Rule 2: Dispute Structure                            │
│  ├── Contains at least 3 labeled DISPUTE POINTs       │
│  ├── References Metro 2 field names + numbers         │
│  └── Letter length between 400-3000 words             │
│                                                       │
│  Rule 3: Evidence Integrity                           │
│  ├── Does NOT mention "1099-C" unless user provided   │
│  ├── Does NOT mention "attached chat log"             │
│  ├── Does NOT reference "payment receipts" unprovided │
│  └── Does NOT claim "see attached" for phantom docs   │
│                                                       │
│  Rule 4: Legal Citation Accuracy                      │
│  ├── FDCPA (§1692) NOT cited against original creditor│
│  │   (check against known originals: Capital One,     │
│  │    Chase, Discover, Amex, Wells Fargo, etc.)       │
│  ├── FCRA sections cited correctly (611, 623, etc.)   │
│  └── Case law names formatted properly                │
│                                                       │
│  Rule 5: Strategy Alignment                           │
│  ├── Arbitration pack → mentions arbitration clause   │
│  ├── Consumer Law pack → cites consumer statutes      │
│  ├── ACDV Response → demands verification method      │
│  └── Default pack → standard FCRA dispute structure   │
│                                                       │
│  Rule 6: Escalation Continuity (Round 2+)             │
│  ├── References prior dispute attempt                 │
│  ├── Mentions prior response/outcome if available     │
│  └── Escalation language present (stronger tone)      │
│                                                       │
│  Rule 7: Tone & Professionalism                       │
│  ├── No threats of physical action                    │
│  ├── No profanity or slurs                            │
│  ├── No demands for money/damages (unless arbitration)│
│  └── Professional closing                             │
│                                                       │
│  Rule 8: Recipient Accuracy                           │
│  ├── Bureau letter addressed to CRA (not creditor)    │
│  ├── Furnisher letter addressed to creditor           │
│  └── Address block present                            │
└──────────────────────────────────────────────────────┘
    ↓
┌──────────────┐          ┌──────────────────┐
│  ALL PASS    │          │  RULE(S) FAILED  │
│              │          │                  │
│  Letter      │          │  Auto mode:      │
│  approved    │          │  Regenerate with │
│  → continues │          │  failure reason  │
│  in pipeline │          │  injected into   │
│              │          │  prompt (max 2   │
│              │          │  retries)        │
│              │          │                  │
│              │          │  Supervised mode: │
│              │          │  Flag letter with │
│              │          │  specific warnings│
│              │          │  user can override│
└──────────────┘          └──────────────────┘
```

### Implementation
```
┌─────────────────────────────────────────────────────┐
│  services/letter_quality_gate.py  [NEW FILE]        │
│                                                     │
│  check_letter_quality(                              │
│      letter_text: str,                              │
│      account_name: str,                             │
│      account_number: str,                           │
│      bureau: str,                                   │
│      prompt_pack: str,                              │
│      round_number: int,                             │
│      is_original_creditor: bool,                    │
│      user_provided_docs: list = [],                 │
│  ) → QualityResult                                  │
│                                                     │
│  class QualityResult:                               │
│      passed: bool                                   │
│      score: int  (0-100)                            │
│      failures: list[str]  (rule descriptions)       │
│      warnings: list[str]  (non-blocking issues)     │
│                                                     │
│  No API calls — pure Python regex/keyword checks    │
│  Runs in <50ms per letter                           │
│  Zero cost                                          │
└─────────────────────────────────────────────────────┘

Integration points:
  pipeline_engine.py → handle_generation()
    After letter is generated, run quality gate
    If failed + auto mode: regenerate (max 2 retries)
    If failed + supervised: attach warnings to letter record

  blueprints/disputes.py → generate_process()
    After Pro plan letter generation, run quality gate
    Show warnings on review page if any rules failed

  Pro Auto Mode → autopilot route
    Run quality gate on each letter before dropping into folder
    Flag letters that need user attention
```

### Known Original Creditors List (for FDCPA guard)
```
CAPITAL ONE, CHASE, JP MORGAN CHASE, DISCOVER, AMERICAN EXPRESS,
AMEX, WELLS FARGO, BANK OF AMERICA, CITIBANK, CITI, US BANK,
BARCLAYS, SYNCHRONY, NAVY FEDERAL, USAA, PNC, TD BANK,
REGIONS, TRUIST, FIFTH THIRD, ALLY, SOFI, MARCUS,
GEORGIAS OWN, BRIDGECREST, EDFINANCIAL, NELNET, NAVIENT,
GREAT LAKES, MOHELA, SALLIE MAE
```

### Known Debt Collectors (FDCPA applies)
```
LVNV FUNDING, MIDLAND CREDIT, PORTFOLIO RECOVERY, PRA GROUP,
CONVERGENT, ENHANCED RECOVERY, IC SYSTEM, TRANSWORLD,
AFNI, ALLIED INTERSTATE, ASSET ACCEPTANCE, CAVALRY,
CREDIT CORP, ENCORE CAPITAL, FIRST SOURCE ADVANTAGE,
JEFFERSON CAPITAL, RESURGENT CAPITAL, UNIFIN
```

---

## IMPLEMENTATION PRIORITY

### Phase A: CFPB AI Narratives (2-3 hours)
1. Create `services/cfpb_narrative_generator.py` (~80 lines)
   - System prompt for CFPB narrative generation
   - Takes parsed inaccuracies + CFPB data + legal research + dispute history
   - Returns 3 personalized narratives
2. Update `/cfpb-wizard` route in `blueprints/disputes.py` (~20 lines)
   - Call generator before rendering
   - Pass AI narratives with static fallback
3. No template changes needed

### Phase B: Letter Quality Gate (1-2 hours)
1. Create `services/letter_quality_gate.py` (~150 lines)
   - 8 rule categories, pure Python regex/keyword checks
   - Returns QualityResult with pass/fail, score, failures, warnings
   - Known creditor/collector lists for FDCPA guard
2. Integrate into `pipeline_engine.py` handle_generation()
   - Auto-retry on failure (max 2 retries with failure reason in prompt)
3. Integrate into `blueprints/disputes.py` generate_process()
   - Show warnings on review page
4. Integrate into Pro Auto Mode autopilot route
   - Flag letters needing user attention

### Phase C: Agentic Response System (1-2 hours)
1. Modify `handle_response_received()` in `pipeline_engine.py` (~15 lines)
   - Add mode check for full_auto
   - Auto-increment round, recommend packs, return 'strategy'
2. Modify `handle_review()` in `pipeline_engine.py` (~5 lines)
   - Auto-approve in full_auto mode
3. No new files needed — all infrastructure exists

### Phase D: Verification
1. Pro user: Open CFPB wizard → verify AI narratives are personalized
2. Business user (supervised): Run pipeline → upload responses → verify round_review pause
3. Business user (full_auto): Run pipeline → upload responses → verify auto-escalation loops

---

## EXISTING FILE MAP

### Already Built (no changes needed):
- `services/cfpb_search.py` — CFPB API integration
- `services/legal_research.py` — Legal research agent
- `services/escalation_engine.py` — Smart pack recommendations
- `services/creditor_intelligence.py` — Cross-client win rates
- `services/rules_engine.py` — Business rules evaluation
- `services/response_classifier.py` — Auto-classify response files
- `templates/cfpb_wizard.html` — Wizard UI
- `templates/log_response.html` — Response logging UI
- `templates/research_results.html` — Research display + escalation form

### Needs Modification:
- `services/pipeline_engine.py` — handle_response_received() + handle_review()
- `blueprints/disputes.py` — /cfpb-wizard route

### New Files:
- `services/cfpb_narrative_generator.py` — AI narrative generation
- `services/letter_quality_gate.py` — Pre-send letter validation (8 rules, no API cost)
