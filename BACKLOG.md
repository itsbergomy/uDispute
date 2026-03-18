# uDispute — Feature Backlog

Items to revisit after beta feedback. Not blocking launch.

---

## 1. Flow Persistence Across Plan Upgrades

**Priority:** Post-beta
**Context:** Free users complete the full dispute flow (upload PDF → analyze → generate letter → final review) but hit the mail gate because mailing is Pro-only. If they upgrade, they lose their progress and have to start over.

**Solution:**
- Save flow state to DB when a free user hits the mail gate (letter text, merged PDF path, bureau, account info, round number)
- Use existing `MailedLetter` model with a `status='draft'` flag, or create a `PendingDispute` model
- On upgrade, show a "You have a pending dispute ready to mail" banner on the dashboard
- One click takes them to the mail form with everything pre-filled

**What's already in place:**
- `MailedLetter` model saves `letter_text`, `pdf_url`, `bureau`, `round_number`
- `/convert-pdf` route already creates a backup PDF in uploads
- Just need to persist as draft instead of discarding

**Why not now:** Beta testers can switch plans freely with the dev toggle. No one gets stuck. This is for when real users are paying for upgrades.

---

## 2. Twin Agents on Auto Mode

**Priority:** Post-beta
**Context:** Run two autonomous AI agents simultaneously on a client's dispute pipeline. One agent handles the primary dispute strategy (letter generation, escalation, mailing) while a second agent runs a parallel strategy — different prompt pack, different legal angle — against the same accounts.

**Why it matters:**
- Bureaus respond differently to different legal approaches
- Running two strategies in parallel doubles the chance of a successful deletion/update
- Credit repair pros already do this manually — send one FCRA letter and one FDCPA letter for the same account
- Autonomous mode already exists for single-agent pipelines — this extends it to dual-track

**Implementation ideas:**
- Fork the pipeline into two agent instances per client
- Each agent gets a different prompt pack assignment
- Results merge back into a single timeline view so the user sees both tracks
- If one strategy succeeds, the other auto-pauses for that account

**Why not now:** Single-agent auto mode needs real-world testing first. Twin agents add complexity — need to validate the base pipeline is solid before doubling it.

---

## 3. Pro Semi-Auto Agent (48hr Cooldown)

**Priority:** Post-beta
**Context:** Give Pro users a limited autonomous agent that does the thinking (analyze → generate letters for all accounts → merge docs) but stops at mailing. User reviews and sends manually. 48-hour cooldown between rounds.

**Tier structure:**
| | Free | Pro | Business |
|---|---|---|---|
| Disputes | Manual, 3 accounts | Semi-Auto, all accounts | Full Auto, all clients |
| Cooldown | 48hr | 48hr per round | None |
| Mailing | No | Manual (user clicks send) | Automatic |
| Escalation | No | Auto strategy, manual trigger | Fully autonomous |
| Multi-client | No | No | CRM + pipelines |

**Implementation:**
- Add `last_semi_auto_run` timestamp to User model
- Reuse existing Business pipeline, hard stop at delivery stage
- Save generated packages as `status='ready_to_mail'` instead of calling DocuPost
- Show "Your round is ready — review and send" banner on dashboard
- 48hr cooldown enforced server-side before next run allowed
- Natural upsell: "Tired of waiting? Business plan runs unlimited rounds automatically."

**Why not now:** Core manual flow needs beta validation first. This is a post-launch upgrade path.

---

## 4. Dispute Strategies (Workflow Playbooks)

**Priority:** Post-beta / V2
**Context:** Strategies are full dispute workflows — not just what legal language to use (that's prompt packs), but the entire playbook: what to send, in what order, to whom, with what timing, and how to escalate.

**Strategy vs Prompt Pack:**
- **Prompt Pack** = the *voice* (which laws to cite, what tone)
- **Strategy** = the *playbook* (sequence of steps, timing, escalation path)
- A Strategy can specify which prompt pack to use at each stage

**First Strategy: 7-Day CFPB Deletion Process**

A proven rinse-and-repeat CFPB complaint workflow used by credit repair pros:

**Prerequisites:**
- Experian credit report covering all three bureaus
- Identified inaccurate accounts (especially collections: LVNV, Midland Funding, Credit Collection Services, etc.)
- Dispute letter prepared for CFPB complaint
- Supporting documents ready

**CFPB Complaint Flow:**
1. Go to www.cfpb.gov → Create account → File complaint
2. Category selections:
   - Complaint about: **Debt Collection**
   - Type of debt: **I do not know**
   - Problem: **Took or threatened to take negative or legal action**
   - Best describes problem: **Threatened or suggested your credit would be damaged**
   - Already tried to fix: **Yes**
   - Requested info from company: **Yes**
   - Company provided info: **No**
3. "What happened?" — choose one:
   - *"This company is violating my rights. They have not provided validation information under 12 CFR 1006.34(b)(5) yet they have placed a collection on my consumer report recently."*
   - *"This agency is violating my consumer rights by using false, misleading, misrepresentation, and deceptive means."*
   - Closing: *"I have made previous attempts to fix these issues directly with them and they are violating my rights. I'm entitled to $1,000 for every violation listed. They either pay me or delete these accounts ASAP."*
4. Fair resolution: *"I demand for this to be removed from my credit report. It's damaging..."* + personal impact statement
5. Skip document upload
6. Company name: as listed on credit report
7. Account number: from credit report
8. Submitting for: Myself

**If account comes back verified → rinse and repeat.**

**Implementation ideas:**
- `Strategy` model: name, description, steps (JSON), timing rules, prompt pack per stage
- Strategy picker on dispute flow (alongside prompt pack selector)
- Business users can create/save custom strategies
- Community strategy sharing (Skool → uDispute import)
- Built-in strategies ship with the app (7-Day Deletion, Standard 30-Day, Aggressive Arbitration, etc.)
- Semi-Auto and Full Auto agents follow the selected strategy's playbook

**Why not now:** Need the core dispute pipeline validated with beta users first. Strategies layer on top of a working foundation.

---
