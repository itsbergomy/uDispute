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

### Phase B: Agentic Response System (1-2 hours)
1. Modify `handle_response_received()` in `pipeline_engine.py` (~15 lines)
   - Add mode check for full_auto
   - Auto-increment round, recommend packs, return 'strategy'
2. Modify `handle_review()` in `pipeline_engine.py` (~5 lines)
   - Auto-approve in full_auto mode
3. No new files needed — all infrastructure exists

### Phase C: Verification
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

### New File:
- `services/cfpb_narrative_generator.py` — AI narrative generation
