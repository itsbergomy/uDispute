"""
Pipeline engine — the state machine that drives autonomous dispute processing.
Each state has a handler that does one thing and returns the next state.
"""

import os
import re
import json
import logging
from datetime import datetime

from models import (
    db, DisputePipeline, PipelineTask, DisputeAccount,
    Client, ClientReportAnalysis, ClientDisputeLetter, WorkflowSetting,
    CustomLetter, BureauResponse
)
from services.pdf_parser import extract_negative_items_from_pdf, compute_pdf_hash
from services.report_analyzer import run_report_analysis
from services.cloud_storage import download_to_temp, upload_from_path, is_configured as cloud_configured
from services.strategy import (
    select_accounts_for_dispute, get_escalation_config, build_dispute_reason
)
from services.letter_generator import (
    PACKS, generate_letter, build_prompt, letter_to_pdf,
    image_to_pdf, merge_dispute_package
)
from services.delivery import mail_letter_via_docupost

logger = logging.getLogger(__name__)

# Wait states — pipeline pauses here until external action
WAIT_STATES = {'review', 'awaiting_response', 'round_review', 'completed', 'failed'}

# Hardcoded bureau dispute addresses
BUREAU_ADDRESSES = {
    'experian': {
        'name': 'Experian',
        'company': 'Experian',
        'address1': 'P.O. Box 4500',
        'city': 'Allen',
        'state': 'TX',
        'zip': '75013',
    },
    'transunion': {
        'name': 'TransUnion LLC',
        'company': 'TransUnion',
        'address1': 'P.O. Box 2000',
        'city': 'Chester',
        'state': 'PA',
        'zip': '19016',
    },
    'equifax': {
        'name': 'Equifax Information Services LLC',
        'company': 'Equifax',
        'address1': 'P.O. Box 740256',
        'city': 'Atlanta',
        'state': 'GA',
        'zip': '30374',
    },
}

# Placeholder patterns that must NOT survive into mailed letters
PLACEHOLDER_RE = re.compile(r'\{[A-Z_]+\}|\[YOUR[ _].*?\]|\[CLIENT.*?\]|\[ADDRESS.*?\]|\[ACCOUNT.*?\]', re.IGNORECASE)


# ─── Helpers ───

def _get_agent_config(pipeline):
    """Extract agent_config from strategy_json, or return empty dict."""
    data = json.loads(pipeline.strategy_json or '{}')
    return data.get('agent_config', {})


def _get_client_context(client, account=None, recipient=None):
    """Build a full context dict for letter personalization."""
    today = datetime.utcnow().strftime('%B %d, %Y')
    ctx = {
        'client_full_name': f"{client.first_name} {client.last_name}",
        'client_first_name': client.first_name or '',
        'client_last_name': client.last_name or '',
        'client_address': client.address_line1 or '',
        'client_address_line2': client.address_line2 or '',
        'client_city': client.city or '',
        'client_state': client.state or '',
        'client_zip': client.zip_code or '',
        'client_city_state_zip': f"{client.city or ''}, {client.state or ''} {client.zip_code or ''}",
        'today_date': today,
        'date': today,
    }
    if account:
        ctx.update({
            'account_name': account.account_name or '',
            'account_number': account.account_number or '',
            'bureau': account.bureau or '',
            'entity': (account.bureau or '').title(),
            'marks': account.status or '',
            'action': (account.issue.split('.')[0] if account.issue else 'investigation and correction'),
            'issue': account.issue or 'Inaccurate reporting',
            'dispute_date': '',
            'days': '30',
        })
    if recipient:
        ctx.update({
            'creditor_name': recipient.get('name', ''),
            'creditor_address': recipient.get('address1', ''),
            'creditor_city_state_zip': f"{recipient.get('city','')}, {recipient.get('state','')} {recipient.get('zip','')}",
            'bureau_name': recipient.get('name', ''),
            'bureau_address': recipient.get('address1', ''),
        })
    return ctx


def _sanitize_letter(text, context):
    """Replace any remaining placeholders in generated letter text with real values."""
    # First pass: replace known placeholder patterns with context values
    replacements = {
        '{CLIENT_NAME}': context.get('client_full_name', ''),
        '{CLIENT_FULL_NAME}': context.get('client_full_name', ''),
        '{CLIENT_ADDRESS}': context.get('client_address', ''),
        '{CLIENT_CITY_STATE_ZIP}': context.get('client_city_state_zip', ''),
        '{ACCOUNT_NAME}': context.get('account_name', ''),
        '{ACCOUNT_NUMBER}': context.get('account_number', ''),
        '{BUREAU}': context.get('bureau', ''),
        '{ENTITY}': context.get('entity', ''),
        '{DATE}': context.get('today_date', ''),
        '{TODAY_DATE}': context.get('today_date', ''),
        '[YOUR NAME]': context.get('client_full_name', ''),
        '[YOUR ADDRESS]': context.get('client_address', ''),
        '[CLIENT NAME]': context.get('client_full_name', ''),
        '[CLIENT ADDRESS]': context.get('client_address', ''),
        '[ACCOUNT NAME]': context.get('account_name', ''),
        '[ACCOUNT NUMBER]': context.get('account_number', ''),
        '[ADDRESS]': context.get('client_address', ''),
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)

    # Second pass: catch any remaining {UPPERCASE} or [BRACKET] placeholders
    remaining = PLACEHOLDER_RE.findall(text)
    if remaining:
        logger.warning(f"Unfilled placeholders found in letter: {remaining}")
        # Remove them rather than mail with placeholders
        text = PLACEHOLDER_RE.sub('', text)

    return text


def _validate_pdf_no_placeholders(pdf_path):
    """
    Final safety gate: read PDF text and check for surviving placeholders.
    Returns True if clean, raises ValueError if placeholders found.
    """
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            page_text = page.extract_text() or ''
            matches = PLACEHOLDER_RE.findall(page_text)
            if matches:
                raise ValueError(
                    f"Letter PDF contains unfilled placeholders: {matches}. "
                    f"Refusing to mail. File: {pdf_path}"
                )
    except ImportError:
        logger.warning("PyPDF2 not available for placeholder validation")
    return True


def create_pipeline(client_id, user_id, config=None):
    """
    Create a new dispute pipeline for a client.

    config dict (optional):
        mode: "supervised" | "full_auto"
        round_packs: ["default", "consumer_law", "ACDV_response"]
        max_rounds: 3
        send_to: "bureaus" | "creditors"
        creditor_addresses: [{"name":"...", "address1":"...", ...}]
    """
    strategy_json = '{}'
    if config:
        strategy_json = json.dumps({'agent_config': config})

    pipeline = DisputePipeline(
        client_id=client_id,
        user_id=user_id,
        state='intake',
        round_number=1,
        strategy_json=strategy_json,
    )
    db.session.add(pipeline)
    db.session.commit()
    return pipeline


def advance_pipeline(pipeline_id):
    """
    Advance a pipeline through its states iteratively (not recursively).
    This is the main entry point — called by the task queue or directly.
    """
    while True:
        # Always start each iteration with a clean session
        try:
            db.session.remove()
        except Exception:
            pass

        pipeline = DisputePipeline.query.get(pipeline_id)
        if not pipeline:
            return

        current_state = pipeline.state

        if current_state in ('completed', 'failed'):
            return

        handler = STATE_HANDLERS.get(current_state)
        if not handler:
            return

        # Create a task record for tracking
        task = PipelineTask(
            pipeline_id=pipeline_id,
            task_type=current_state,
            state='running',
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id  # Cache the ID before handler potentially kills session

        try:
            next_state = handler(pipeline)

            # Handler may have killed the session — get fresh everything
            db.session.remove()
            pipeline = DisputePipeline.query.get(pipeline_id)
            task = PipelineTask.query.get(task_id)

            task.state = 'completed'
            task.completed_at = datetime.utcnow()
            task.output_json = json.dumps({'next_state': next_state})

            pipeline.state = next_state
            pipeline.updated_at = datetime.utcnow()
            db.session.commit()

            # If next state is a wait state, stop advancing
            if next_state in WAIT_STATES:
                return
            # Otherwise, loop continues to next state

        except Exception as e:
            logger.exception(f"Pipeline {pipeline_id} failed at {current_state}: {e}")
            # Full cleanup — nuke the session and start fresh
            try:
                db.session.rollback()
            except Exception:
                pass
            try:
                db.session.remove()
            except Exception:
                pass
            # Re-fetch with a completely fresh session
            try:
                pipeline = DisputePipeline.query.get(pipeline_id)
                if pipeline:
                    pipeline.state = 'failed'
                    pipeline.error_message = f"{current_state}: {str(e)[:450]}"
                    pipeline.updated_at = datetime.utcnow()
                task = PipelineTask.query.get(task_id)
                if task and task.state == 'running':
                    task.state = 'failed'
                    task.error_message = str(e)[:500]
                    task.completed_at = datetime.utcnow()
                db.session.commit()
            except Exception as inner:
                logger.error(f"Could not mark pipeline {pipeline_id} as failed: {inner}")
            return


def get_pipeline_status(pipeline_id):
    """Get full status of a pipeline for API/dashboard display."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline:
        return None

    tasks = PipelineTask.query.filter_by(pipeline_id=pipeline_id).order_by(PipelineTask.created_at).all()
    accounts = DisputeAccount.query.filter_by(pipeline_id=pipeline_id).order_by(DisputeAccount.created_at).all()
    agent_config = _get_agent_config(pipeline)

    # Compute per-round outcome summary
    current_round_accounts = [a for a in accounts if a.round_number == pipeline.round_number]
    round_summary = {
        'total': len(current_round_accounts),
        'removed': sum(1 for a in current_round_accounts if a.outcome == 'removed'),
        'updated': sum(1 for a in current_round_accounts if a.outcome == 'updated'),
        'verified': sum(1 for a in current_round_accounts if a.outcome == 'verified'),
        'no_response': sum(1 for a in current_round_accounts if a.outcome == 'no_response'),
        'pending': sum(1 for a in current_round_accounts if a.outcome == 'pending'),
    }

    return {
        'id': pipeline.id,
        'client_id': pipeline.client_id,
        'state': pipeline.state,
        'round_number': pipeline.round_number,
        'max_rounds': agent_config.get('max_rounds', 3),
        'mode': agent_config.get('mode', 'supervised'),
        'round_packs': agent_config.get('round_packs', ['default', 'consumer_law', 'ACDV_response']),
        'round_summary': round_summary,
        'error_message': pipeline.error_message,
        'created_at': pipeline.created_at.isoformat() if pipeline.created_at else None,
        'updated_at': pipeline.updated_at.isoformat() if pipeline.updated_at else None,
        'tasks': [
            {
                'type': t.task_type,
                'state': t.state,
                'error': t.error_message,
                'created_at': t.created_at.isoformat() if t.created_at else None,
                'completed_at': t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in tasks
        ],
        'accounts': [
            {
                'id': a.id,
                'account_name': a.account_name,
                'account_number': a.account_number,
                'bureau': a.bureau,
                'template_pack': a.template_pack,
                'escalation_level': a.escalation_level,
                'outcome': a.outcome,
                'round_number': a.round_number,
                'mailed_at': a.mailed_at.isoformat() if a.mailed_at else None,
                'letter_id': a.letter_id,
                'letter_status': a.letter.status if a.letter else None,
                'docupost_letter_id': a.letter.docupost_letter_id if a.letter else None,
                'docupost_cost': a.letter.docupost_cost if a.letter else None,
                'delivery_status': a.letter.delivery_status if a.letter else None,
            }
            for a in accounts
        ],
    }


# ─── State Handlers ───

def handle_intake(pipeline):
    """Validate that the client has a PDF and ID documents uploaded."""
    client = Client.query.get(pipeline.client_id)
    if not client:
        raise ValueError("Client not found")

    if not client.pdf_filename:
        raise ValueError("No credit report PDF uploaded for this client")

    # Resolve PDF path — Cloudinary URL or local file
    if client.pdf_filename.startswith('http'):
        pdf_path = download_to_temp(client.pdf_filename, suffix='.pdf')
        if not pdf_path:
            raise ValueError(f"Could not download PDF from cloud: {client.pdf_filename}")
    else:
        upload_folder = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
        pdf_path = os.path.join(upload_folder, str(client.id), client.pdf_filename)
        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(upload_folder, client.pdf_filename)
        if not os.path.exists(pdf_path):
            raise ValueError(f"PDF file not found: {client.pdf_filename}")

    # Compute PDF hash
    pdf_hash = compute_pdf_hash(pdf_path)

    # Cache IDs and strategy before releasing DB
    pipeline_id = pipeline.id
    client_id = client.id
    strategy_data = json.loads(pipeline.strategy_json or '{}')

    # Pull existing analysis while we still have DB
    analysis = {}
    latest = ClientReportAnalysis.query.filter_by(client_id=client_id).order_by(
        ClientReportAnalysis.id.desc()
    ).first()
    if latest:
        try:
            analysis = json.loads(latest.analysis_json)
        except (ValueError, TypeError):
            pass

    # ── Release DB before long PDF extraction ──
    db.session.expunge_all()
    db.session.remove()

    # Extract negative items (SLOW — 30-60s, no DB connection held)
    try:
        negative_items = extract_negative_items_from_pdf(pdf_path)
    except Exception as exc:
        raise ValueError(f"Failed to extract accounts from PDF: {exc}")

    # ── Fresh DB connection — write results ──
    pipeline = DisputePipeline.query.get(pipeline_id)
    pipeline.pdf_hash = pdf_hash
    strategy_data['negative_items'] = negative_items
    strategy_data['analysis'] = analysis
    pipeline.strategy_json = json.dumps(strategy_data)
    db.session.commit()

    return 'strategy'


def handle_analysis(pipeline):
    """Extract negative items and run vision-based report analysis."""
    client = Client.query.get(pipeline.client_id)

    if client.pdf_filename and client.pdf_filename.startswith('http'):
        pdf_path = download_to_temp(client.pdf_filename, suffix='.pdf')
    else:
        upload_folder = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
        pdf_path = os.path.join(upload_folder, str(client.id), client.pdf_filename)
        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(upload_folder, client.pdf_filename)

    # Extract negative items
    negative_items = extract_negative_items_from_pdf(pdf_path)

    # Preserve agent_config when writing to strategy_json
    strategy_data = json.loads(pipeline.strategy_json or '{}')
    strategy_data['negative_items'] = negative_items

    # Run full report analysis
    try:
        analysis = run_report_analysis(pdf_path)
        strategy_data['analysis'] = analysis

        # Save analysis to database
        analysis_record = ClientReportAnalysis(
            client_id=client.id,
            analysis_json=json.dumps(analysis)
        )
        db.session.add(analysis_record)
    except Exception as e:
        logger.warning(f"Report analysis failed (non-fatal): {e}")
        strategy_data['analysis'] = {}

    pipeline.strategy_json = json.dumps(strategy_data)
    db.session.commit()

    return 'strategy'


def handle_strategy(pipeline):
    """Dispute ALL extracted accounts — every negative item gets a letter."""
    # ── PHASE 1: Read everything from DB into plain Python variables ──
    pipeline_id = pipeline.id
    round_number = pipeline.round_number
    strategy_data = json.loads(pipeline.strategy_json or '{}')
    negative_items = strategy_data.get('negative_items', [])
    analysis = strategy_data.get('analysis', {})
    agent_config = strategy_data.get('agent_config', {})

    if not negative_items:
        raise ValueError("No negative items found to dispute — run Extract Accounts first")

    # For round > 1, read unresolved accounts into plain dicts
    unresolved_dicts = []
    if round_number > 1:
        unresolved = DisputeAccount.query.filter(
            DisputeAccount.pipeline_id == pipeline_id,
            DisputeAccount.outcome.in_(['verified', 'no_response']),
            DisputeAccount.round_number == round_number - 1,
        ).all()
        unresolved_dicts = [
            {'account_name': a.account_name, 'account_number': a.account_number}
            for a in unresolved
        ]

    # ── PHASE 2: Release DB connection before any API calls ──
    db.session.expunge_all()
    db.session.remove()

    # ── PHASE 3: Do all the thinking (API calls) with NO db connection ──
    if round_number > 1:
        decisions = [
            {
                'account_name': d['account_name'],
                'account_number': d['account_number'],
                'reason': f'Previous dispute was verified/no response. Escalating to round {round_number}.',
                'legal_basis': '',
            }
            for d in unresolved_dicts
        ]
    else:
        # Round 1: dispute EVERY negative item — no AI filtering
        try:
            decisions = select_accounts_for_dispute(
                negative_items=negative_items,
                analysis_data=analysis,
                round_number=round_number,
            )
        except Exception:
            decisions = []
        # Make sure every negative item is included even if AI skipped it
        ai_keys = {(d.get('account_name',''), d.get('account_number','')) for d in decisions}
        for item in negative_items:
            key = (item.get('account_name', ''), item.get('account_number', ''))
            if key not in ai_keys:
                decisions.append({
                    'account_name': item.get('account_name', ''),
                    'account_number': item.get('account_number', ''),
                    'reason': item.get('reason', 'Inaccurate or unverified reporting'),
                    'legal_basis': 'FCRA Section 611 — right to dispute inaccurate information',
                })

    # Determine pack and targets from agent config or escalation map
    round_packs = agent_config.get('round_packs', [])
    send_to = agent_config.get('send_to', 'bureaus')

    if round_packs and round_number <= len(round_packs):
        pack = round_packs[round_number - 1]
        level = round_number
    else:
        escalation = get_escalation_config(round_number)
        pack = escalation['pack']
        level = escalation['level']

    # Smart escalation: override pack with creditor intelligence if available (round 2+)
    if round_number > 1 and negative_items:
        try:
            from services.escalation_engine import recommend_escalation
            # Use first account's outcome to drive recommendation
            first_account_name = negative_items[0].get('account_name', '')
            prev_accounts = DisputeAccount.query.filter_by(
                pipeline_id=pipeline_id,
                round_number=round_number - 1,
            ).all()
            prev_outcome = 'verified'
            for pa in prev_accounts:
                if pa.account_name == first_account_name and pa.outcome:
                    prev_outcome = pa.outcome
                    break
            rec = recommend_escalation(pipeline.user_id, first_account_name, round_number - 1, prev_outcome)
            if rec and rec.get('confidence', 0) > 0.4 and rec.get('source') != 'default_ladder':
                pack = rec['pack']
                plog(f"[PIPELINE] Smart escalation: {first_account_name} → {pack} (confidence: {rec['confidence']}, source: {rec['source']})")
        except Exception as e:
            plog(f"[PIPELINE] Smart escalation check failed: {e}")

    if send_to == 'creditors':
        creditor_addresses = agent_config.get('creditor_addresses', [])
        targets = [c['name'] for c in creditor_addresses] if creditor_addresses else ['experian', 'transunion', 'equifax']
    else:
        targets = ['experian', 'transunion', 'equifax']

    # ── PHASE 4: Fresh DB connection — write everything at once ──
    pipeline = DisputePipeline.query.get(pipeline_id)
    pipeline.strategy_json = json.dumps(strategy_data)

    for decision in decisions:
        for target in targets:
            action, issue = build_dispute_reason(decision, round_number)
            account = DisputeAccount(
                pipeline_id=pipeline_id,
                account_name=decision.get('account_name', ''),
                account_number=decision.get('account_number', ''),
                bureau=target,
                status=decision.get('reason', ''),
                issue=issue,
                template_pack=pack,
                dispute_reason=decision.get('legal_basis', ''),
                escalation_level=level,
                round_number=round_number,
            )
            db.session.add(account)

    db.session.commit()
    return 'generation'


def _generate_notice_of_dispute(pipeline, client_data, accounts_data, round_number):
    """Generate hardcoded Notice of Dispute letters — one per bureau, no AI calls."""
    from datetime import datetime as _dt

    client_name = f"{client_data['first_name']} {client_data['last_name']}"
    client_email = client_data.get('email', '')
    today_date = _dt.now().strftime('%B %d, %Y')

    # Group accounts by bureau
    bureaus = {}
    for ad in accounts_data:
        bureau = ad.get('bureau', '').strip()
        if not bureau or bureau == 'cfpb':
            continue
        bureaus.setdefault(bureau, []).append(ad)

    generated_letters = []

    for bureau, accts in bureaus.items():
        # Bureau address block
        addr = BUREAU_ADDRESSES.get(bureau.lower(), {})
        bureau_block = f"{addr.get('name', bureau)}\n{addr.get('address1', '')}\n{addr.get('city', '')}, {addr.get('state', '')} {addr.get('zip', '')}"

        # Account list block
        account_lines = []
        for a in accts:
            line = f"- {a['account_name']}"
            if a.get('account_number'):
                line += f" (Account #{a['account_number']})"
            account_lines.append(line)
        account_block = '\n'.join(account_lines)

        letter_text = f"""{client_name}
{client_data.get('address_line1', '[Your Address]')}
{client_data.get('city', '[City]')}, {client_data.get('state', '[State]')} {client_data.get('zip_code', '[ZIP]')}

{today_date}

{bureau_block}

RE: Notice of Dispute

Dear {addr.get('name', bureau)},

I am writing to formally dispute the inclusion of the following account(s) associated with my credit file:

{account_block}

Under the Fair Credit Reporting Act (15 U.S.C. § 1681i), I am exercising my right to dispute the accuracy and completeness of the information listed above. I request that you conduct a thorough and reasonable investigation into each disputed item.

Please be advised that these account(s) contain inaccurate, incomplete, or unverifiable information. I demand that you verify every aspect of each account — including the original creditor, balance, payment history, dates, and account status — directly with the data furnisher. If the information cannot be verified within 30 days, it must be removed from my credit report.

Please send me written confirmation of your investigation results and any corrections made to my credit file.

Below is my ID.

Sincerely,

{client_name}
{client_email}"""

        # Create one letter per bureau, link to the first account in that bureau
        generated_letters.append({
            'account_ids': [a['id'] for a in accts],
            'letter_text': letter_text,
            'template_name': f"Notice of Dispute - {bureau}",
            'bureau': bureau,
        })
        logger.info(f"Generated Notice of Dispute for {bureau} ({len(accts)} accounts)")

    if not generated_letters:
        raise ValueError("No bureaus found to generate Notice of Dispute letters")

    # Save letters and link to accounts
    for gl in generated_letters:
        letter_record = ClientDisputeLetter(
            client_id=pipeline.client_id,
            letter_text=gl['letter_text'],
            status='Draft',
            template_name=gl['template_name'],
            round_number=round_number,
            bureau=gl['bureau'],
        )
        db.session.add(letter_record)
        db.session.flush()

        # Link all accounts for this bureau to this single letter
        for acct_id in gl['account_ids']:
            account = DisputeAccount.query.get(acct_id)
            if account:
                account.letter_id = letter_record.id

    db.session.commit()
    logger.info(f"Notice of Dispute: {len(generated_letters)} letters created (one per bureau)")
    return 'review'


def handle_generation(pipeline):
    """Generate dispute letters for each DisputeAccount in the current round."""
    # ── PHASE 1: Read all DB data into plain Python structures ──
    pipeline_id = pipeline.id
    client_id = pipeline.client_id
    round_number = pipeline.round_number

    accounts_data = []
    for a in DisputeAccount.query.filter_by(
        pipeline_id=pipeline_id, round_number=round_number, outcome='pending'
    ).all():
        accounts_data.append({
            'id': a.id, 'account_name': a.account_name,
            'account_number': a.account_number, 'bureau': a.bureau,
            'template_pack': a.template_pack,
            'status': a.status or '', 'issue': a.issue or '',
        })

    client = Client.query.get(client_id)
    client_data = {
        'id': client.id, 'first_name': client.first_name, 'last_name': client.last_name,
        'email': client.email, 'address_line1': getattr(client, 'address_line1', ''),
        'address_line2': getattr(client, 'address_line2', ''),
        'city': getattr(client, 'city', ''), 'state': getattr(client, 'state', ''),
        'zip_code': getattr(client, 'zip_code', ''),
    }

    agent_config = _get_agent_config(pipeline)
    send_to = agent_config.get('send_to', 'bureaus')
    creditor_addresses = agent_config.get('creditor_addresses', [])
    custom_letter_id = agent_config.get('custom_letter_id')
    strategy = agent_config.get('strategy', 'standard')

    strategy_data = json.loads(pipeline.strategy_json or '{}')
    parsed_accounts = strategy_data.get('negative_items', [])

    # ── Notice of Dispute: hardcoded template, no AI ──
    if strategy == 'notice_of_dispute':
        return _generate_notice_of_dispute(pipeline, client_data, accounts_data, round_number)

    # Load custom letter template if needed
    custom_body = None
    if custom_letter_id:
        custom = CustomLetter.query.get(custom_letter_id)
        if custom:
            custom_body = custom.body

    # Load legal research data for round 2+
    legal_data = {}
    if round_number > 1:
        for ad in accounts_data:
            prev_response = BureauResponse.query.filter_by(
                dispute_account_id=ad['id']
            ).order_by(BureauResponse.uploaded_at.desc()).first()
            if prev_response and prev_response.analysis_json:
                legal_data[ad['id']] = prev_response.analysis_json

    # ── PHASE 2: Release DB — do all API work with no connection ──
    db.session.expunge_all()
    db.session.remove()

    generated_letters = []  # Collect results to save later

    for ad in accounts_data:
        if ad['bureau'] == 'cfpb':
            continue

        logger.info(f"Generating letter for {ad['account_name']} / {ad['bureau']}")

        # Build recipient
        if send_to == 'creditors':
            recipient = next(
                (c for c in creditor_addresses if c['name'] == ad['bureau']),
                {'name': ad['bureau']}
            )
        else:
            recipient = BUREAU_ADDRESSES.get(ad['bureau'].lower(), {'name': ad['bureau'].title()})

        # Build context from plain dicts (no ORM objects)
        # Reconstruct a minimal account-like dict for _get_client_context
        class _Obj:
            pass
        account_obj = _Obj()
        for k, v in ad.items():
            setattr(account_obj, k, v)
        client_obj = _Obj()
        for k, v in client_data.items():
            setattr(client_obj, k, v)

        context = _get_client_context(client_obj, account_obj, recipient)

        relevant_accounts = [
            a for a in parsed_accounts
            if a.get('account_number') == ad['account_number']
            or a.get('account_name', '').lower() == (ad['account_name'] or '').lower()
        ]

        # Legal research for round 2+
        legal_context = None
        if round_number > 1 and ad['id'] in legal_data:
            try:
                from services.legal_research import research_for_prompt
                legal_context = research_for_prompt(
                    account_name=ad['account_name'],
                    bureau_response=legal_data[ad['id']],
                    round_number=round_number,
                )
            except Exception as e:
                logger.warning(f"Legal research failed for {ad['account_name']}: {e}")

        # Generate the letter (API call — no DB needed)
        try:
            if custom_body:
                letter_text = _sanitize_letter(custom_body, context)
            else:
                prompt, has_inaccuracies, has_legal = build_prompt(
                    ad['template_pack'], 0, context,
                    parsed_accounts=relevant_accounts,
                    legal_research_context=legal_context,
                )
                letter_text = _sanitize_letter(
                    generate_letter(prompt, has_inaccuracies=has_inaccuracies, has_legal_research=has_legal),
                    context,
                )

            generated_letters.append({
                'account_id': ad['id'],
                'letter_text': letter_text,
                'template_name': f"{ad['template_pack']} - {ad['bureau']}",
            })
            logger.info(f"Generated letter for {ad['account_name']} / {ad['bureau']}")

        except Exception as e:
            logger.error(f"Failed to generate letter for {ad['account_name']} / {ad['bureau']}: {e}")
            continue

    if not generated_letters:
        raise ValueError("Failed to generate any letters — all API calls failed")

    # ── PHASE 3: Fresh DB connection — save all letters at once ──
    for gl in generated_letters:
        letter_record = ClientDisputeLetter(
            client_id=client_id,
            letter_text=gl['letter_text'],
            status='Draft',
            template_name=gl['template_name'],
            round_number=round_number,
        )
        db.session.add(letter_record)
        db.session.flush()

        account = DisputeAccount.query.get(gl['account_id'])
        account.letter_id = letter_record.id

    db.session.commit()
    return 'review'


def handle_review(pipeline):
    """
    Check if auto-approve is enabled. If so, advance to delivery.
    Otherwise, stay in review and wait for human approval.
    """
    # Check agent config first
    agent_config = _get_agent_config(pipeline)
    if agent_config.get('mode') == 'full_auto':
        # Auto-approve all draft letters
        _approve_all_drafts(pipeline)
        return 'delivery'

    # Fallback: check WorkflowSetting for backward compatibility
    auto_approve = WorkflowSetting.query.filter_by(
        client_id=pipeline.client_id,
        key='auto_approve',
        enabled=True,
    ).first()

    if auto_approve:
        _approve_all_drafts(pipeline)
        return 'delivery'

    # Stay in review — human must approve via API
    return 'review'


def _approve_all_drafts(pipeline):
    """Helper to approve all draft letters for the current round."""
    accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline.id,
        round_number=pipeline.round_number,
    ).all()

    for account in accounts:
        if account.letter and account.letter.status == 'Draft':
            account.letter.status = 'Approved'

    db.session.commit()


def approve_pipeline_letters(pipeline_id):
    """Called when a human approves all letters in a pipeline at the review stage."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.state != 'review':
        return False

    _approve_all_drafts(pipeline)

    # Advance to delivery — the actual mailing happens in advance_pipeline
    pipeline.state = 'delivery'
    pipeline.updated_at = datetime.utcnow()
    db.session.commit()

    # NOTE: advance_pipeline is called by the API endpoint in a background thread,
    # not here, to avoid blocking the HTTP request during DocuPost mailing.
    return True


def handle_delivery(pipeline):
    """Merge PDFs and mail each letter via DocuPost."""
    import shutil
    import uuid as _uuid

    # ── Early validation — resolve BYOK token first ──
    from services.delivery import get_docupost_token
    docupost_token = get_docupost_token(pipeline.user_id)
    dry_run = os.environ.get('DOCUPOST_DRY_RUN', 'false').lower() == 'true'

    if not docupost_token and not dry_run:
        logger.error("DOCUPOST_API_TOKEN not configured — cannot proceed with delivery. "
                      "Set DOCUPOST_DRY_RUN=true to test without sending.")
        return 'delivery'  # Stay in delivery state for retry

    client = Client.query.get(pipeline.client_id)
    upload_folder = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
    base_url = os.environ.get('APP_BASE_URL', 'http://localhost:5001')
    agent_config = _get_agent_config(pipeline)
    send_to = agent_config.get('send_to', 'bureaus')
    creditor_addresses = agent_config.get('creditor_addresses', [])

    accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline.id,
        round_number=pipeline.round_number,
        outcome='pending',
    ).all()

    sent_count = 0
    fail_count = 0

    for account in accounts:
        if not account.letter or account.letter.status != 'Approved':
            continue
        if account.bureau == 'cfpb':
            continue

        # Build the PDF package
        pdf_paths = []

        # 1. Letter PDF
        letter_pdf = letter_to_pdf(account.letter.letter_text)
        pdf_paths.append(letter_pdf)

        # 2. Supporting documents (ID, SSN) — cloud or local
        for attr, field_type in [('id_filename', 'id_file'), ('ssn_filename', 'ssn_file')]:
            filename = getattr(client, attr)
            if not filename:
                continue

            if filename.startswith('http'):
                # Download from Cloudinary to temp file
                # Cloudinary URLs may not have extensions — default to pdf
                from urllib.parse import urlparse
                _path = urlparse(filename).path
                _ext_part = _path.rsplit('.', 1)[-1].lower() if '.' in _path.split('/')[-1] else 'pdf'
                ext = _ext_part if len(_ext_part) <= 5 else 'pdf'
                doc_path = download_to_temp(filename, suffix=f'.{ext}')
                if not doc_path:
                    continue
            else:
                client_dir = os.path.join(upload_folder, str(client.id))
                doc_path = os.path.join(client_dir, filename)
                if not os.path.exists(doc_path):
                    doc_path = os.path.join(upload_folder, filename)
                if not os.path.exists(doc_path):
                    continue
                ext = filename.rsplit('.', 1)[-1].lower()

            if ext in ('png', 'jpg', 'jpeg'):
                img_pdf = image_to_pdf(doc_path, field_type=field_type)
                pdf_paths.append(img_pdf)
            elif ext == 'pdf':
                pdf_paths.append(doc_path)

        # 3. Supporting docs attached to this account
        from models import SupportingDoc
        sup_docs = SupportingDoc.query.filter_by(
            dispute_account_id=account.id, include_in_package=True
        ).all()
        for sd in sup_docs:
            if sd.file_url:
                if sd.file_url.startswith('http'):
                    sd_ext = sd.filename.rsplit('.', 1)[-1].lower() if '.' in sd.filename else 'pdf'
                    sd_path = download_to_temp(sd.file_url, suffix=f'.{sd_ext}')
                    if not sd_path:
                        continue
                else:
                    sd_path = sd.file_url
                    if not os.path.exists(sd_path):
                        continue
                    sd_ext = sd.filename.rsplit('.', 1)[-1].lower() if '.' in sd.filename else ''

                if sd_ext in ('png', 'jpg', 'jpeg'):
                    pdf_paths.append(image_to_pdf(sd_path, field_type='supporting'))
                elif sd_ext == 'pdf':
                    pdf_paths.append(sd_path)

        # 4. Merge into package
        tmp_package = merge_dispute_package(pdf_paths)

        package_filename = f"package_{client.id}_{account.id}_{_uuid.uuid4().hex[:8]}.pdf"

        # 4a. Placeholder safety check — validate BEFORE upload/move
        try:
            _validate_pdf_no_placeholders(tmp_package)
        except ValueError as e:
            logger.error(str(e))
            account.letter.status = 'Rejected'
            db.session.commit()
            fail_count += 1
            continue

        if cloud_configured():
            cloud_result = upload_from_path(tmp_package, folder=f"clients/{client.id}/packages", filename=package_filename.rsplit('.', 1)[0])
            pdf_url = cloud_result['secure_url'] if cloud_result else None
            if not pdf_url:
                logger.error(f"Failed to upload package to cloud for account {account.id}")
                continue
        else:
            package_dir = os.path.join(upload_folder, str(client.id), 'packages')
            os.makedirs(package_dir, exist_ok=True)
            public_path = os.path.join(package_dir, package_filename)
            shutil.move(tmp_package, public_path)
            pdf_url = f"{base_url}/static/uploads/{client.id}/packages/{package_filename}"

        # 5. Determine recipient address
        if send_to == 'creditors':
            recipient = next(
                (c for c in creditor_addresses if c['name'] == account.bureau),
                {}
            )
            if not recipient:
                logger.warning(f"No creditor address for {account.bureau}, skipping")
                fail_count += 1
                continue
        else:
            recipient = BUREAU_ADDRESSES.get(account.bureau.lower(), {})
            if not recipient:
                logger.warning(f"Unknown bureau {account.bureau}, skipping")
                fail_count += 1
                continue

        sender = {
            'name': f"{client.first_name} {client.last_name}",
            'address1': client.address_line1 or '',
            'address2': client.address_line2 or '',
            'city': client.city or '',
            'state': client.state or '',
            'zip': client.zip_code or '',
        }

        # 6. Mail via DocuPost (or dry-run)
        logger.info(f"Delivery: account={account.account_name} bureau={account.bureau} pdf_url={pdf_url}")

        if dry_run:
            logger.info(f"DRY RUN: Would mail to {recipient.get('name')} "
                        f"for account {account.account_name} ({account.account_number})")
            result = {'success': True, 'response': 'dry-run'}
        else:
            mail_opts = agent_config.get('mail_options', {})
            result = mail_letter_via_docupost(
                pdf_url=pdf_url,
                recipient=recipient,
                sender=sender,
                mail_options=mail_opts,
                api_token=docupost_token,
            )
            logger.info(f"DocuPost response: {result}")

        if result.get('success'):
            account.mailed_at = datetime.utcnow()
            account.letter.status = 'Sent'
            account.letter.pdf_url = pdf_url
            account.letter.mailed_at = datetime.utcnow()
            account.letter.delivery_status = 'queued'
            account.letter.mail_class = mail_opts.get('mail_class', 'usps_first_class')
            account.letter.service_level = mail_opts.get('servicelevel') or None
            # Store DocuPost tracking info
            if result.get('letter_id'):
                account.letter.docupost_letter_id = result['letter_id']
                logger.info(f"DocuPost letter_id: {result['letter_id']}")
            if result.get('cost'):
                account.letter.docupost_cost = result['cost']
                logger.info(f"DocuPost cost: ${result['cost']}")
            sent_count += 1
        else:
            logger.warning(f"Mail failed for account {account.account_number}: {result.get('error')}")
            account.letter.delivery_status = 'error'
            fail_count += 1

        db.session.commit()

    # ── Partial failure handling ──
    logger.info(f"Delivery complete for pipeline {pipeline.id}: "
                f"{sent_count} sent, {fail_count} failed")

    if sent_count == 0 and fail_count > 0:
        logger.error(f"All {fail_count} letters failed — staying in delivery for retry")
        return 'delivery'

    return 'awaiting_response'


def handle_awaiting_response(pipeline):
    """No-op — pipeline waits here until responses are uploaded or timeout occurs."""
    return 'awaiting_response'


def handle_response_received(pipeline):
    """
    Examine outcomes for all accounts in the current round.
    Updates creditor intelligence profiles.
    If all removed/updated -> completed.
    If max rounds exhausted -> completed.
    Otherwise -> round_review (hard pause — user must start next round).
    """
    accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline.id,
        round_number=pipeline.round_number,
    ).all()

    # Update creditor intelligence profiles with outcomes
    try:
        from services.creditor_intelligence import update_creditor_profile
        for acct in accounts:
            if acct.outcome and acct.outcome != 'pending':
                update_creditor_profile(
                    business_user_id=pipeline.user_id,
                    account_name=acct.account_name,
                    outcome=acct.outcome,
                    round_number=acct.round_number,
                    template_pack=acct.template_pack,
                )
    except Exception as e:
        plog(f"[PIPELINE] Creditor intelligence update failed: {e}")

    all_resolved = all(a.outcome in ('removed', 'updated') for a in accounts)

    if all_resolved:
        return 'completed'

    # Check max rounds from agent config (default 3)
    agent_config = _get_agent_config(pipeline)
    max_rounds = agent_config.get('max_rounds', 3)

    if pipeline.round_number >= max_rounds:
        return 'completed'  # Exhausted configured rounds

    # Evaluate business rules for round_completed trigger
    try:
        from services.rules_engine import evaluate_rules
        round_summary = {
            'removed': sum(1 for a in accounts if a.outcome == 'removed'),
            'updated': sum(1 for a in accounts if a.outcome == 'updated'),
            'verified': sum(1 for a in accounts if a.outcome == 'verified'),
            'no_response': sum(1 for a in accounts if a.outcome == 'no_response'),
        }
        rules_result = evaluate_rules(pipeline.user_id, 'round_completed', {
            'pipeline_id': pipeline.id,
            'round_number': pipeline.round_number,
            'round_summary': round_summary,
            'outcome': 'verified' if round_summary['verified'] > 0 else 'no_response' if round_summary['no_response'] > 0 else 'mixed',
        })
        # If auto_escalate was executed, the pipeline state was already changed
        if rules_result:
            pipeline_refreshed = DisputePipeline.query.get(pipeline.id)
            if pipeline_refreshed and pipeline_refreshed.state == 'strategy':
                plog(f"[PIPELINE] Rules engine auto-escalated pipeline {pipeline.id}")
                return 'strategy'
    except Exception as e:
        plog(f"[PIPELINE] Rules engine evaluation failed: {e}")

    # Hard pause — user reviews outcomes and decides whether to start next round
    return 'round_review'


def handle_round_review(pipeline):
    """No-op — pipeline waits here until user explicitly starts the next round."""
    return 'round_review'


# ─── State Handler Registry ───

STATE_HANDLERS = {
    'intake': handle_intake,
    'analysis': handle_analysis,
    'strategy': handle_strategy,
    'generation': handle_generation,
    'review': handle_review,
    'delivery': handle_delivery,
    'awaiting_response': handle_awaiting_response,
    'response_received': handle_response_received,
    'round_review': handle_round_review,
}
