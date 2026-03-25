"""
Pipeline API blueprint — endpoints for autonomous dispute pipeline control.
"""

import os
import json
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from models import (
    db, DisputePipeline, DisputeAccount, BureauResponse,
    Client, ClientDisputeLetter, WorkflowSetting, SupportingDoc
)
from services.pipeline_engine import (
    create_pipeline, advance_pipeline, get_pipeline_status,
    approve_pipeline_letters, _get_agent_config
)
from services.cloud_storage import upload_file, delete_file, is_configured as cloud_configured

pipeline_bp = Blueprint('pipeline', __name__)

# Valid prompt packs
VALID_PACKS = {'default', 'consumer_law', 'ACDV_response', 'arbitration'}


def _run_pipeline_bg(pipeline_id):
    """Run pipeline advancement in a background thread using the CURRENT app."""
    import logging
    from flask import current_app
    logger = logging.getLogger(__name__)

    # Grab the real app object from the current request context
    # so the thread reuses the same SQLAlchemy engine / connection pool.
    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                # Small delay to let the request's commit finish
                import time; time.sleep(0.5)
                logger.info(f"[BG Thread] Starting advance for pipeline {pipeline_id}")
                advance_pipeline(pipeline_id)
                logger.info(f"[BG Thread] Pipeline {pipeline_id} advanced OK")
        except Exception as exc:
            logger.exception(f"[BG Thread] Pipeline {pipeline_id} failed")
            # Mark pipeline as failed so the UI shows an error
            try:
                with app.app_context():
                    db.session.rollback()
                    pipe = DisputePipeline.query.get(pipeline_id)
                    if pipe and pipe.state not in ('completed', 'failed'):
                        pipe.state = 'failed'
                        pipe.error_message = f'Pipeline error: {str(exc)[:200]}'
                        db.session.commit()
            except Exception as inner_exc:
                logger.error(f"[BG Thread] Could not mark pipeline {pipeline_id} as failed: {inner_exc}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _advance(pipeline_id):
    """Launch pipeline in a background thread (Huey-free dev mode)."""
    _run_pipeline_bg(pipeline_id)


def _validate_config(config):
    """Validate agent config dict. Returns (cleaned_config, error_string)."""
    if not isinstance(config, dict):
        return None, 'config must be a dict'

    mode = config.get('mode', 'supervised')
    if mode not in ('supervised', 'full_auto'):
        return None, 'mode must be "supervised" or "full_auto"'

    strategy = config.get('strategy', 'standard')
    if strategy not in ('standard', 'notice', 'dual'):
        return None, 'strategy must be "standard", "notice", or "dual"'

    batch_size = config.get('batch_size', {})
    if batch_size and not isinstance(batch_size, dict):
        return None, 'batch_size must be a dict'

    max_rounds = config.get('max_rounds', 3)
    if not isinstance(max_rounds, int) or max_rounds < 1 or max_rounds > 5:
        return None, 'max_rounds must be 1-5'

    round_packs = config.get('round_packs', [])
    if round_packs:
        if not isinstance(round_packs, list) or len(round_packs) > max_rounds:
            return None, f'round_packs must be a list of up to {max_rounds} items'
        for pack in round_packs:
            if pack not in VALID_PACKS:
                return None, f'Invalid pack: {pack}. Valid: {", ".join(VALID_PACKS)}'

    send_to = config.get('send_to', 'bureaus')
    if send_to not in ('bureaus', 'creditors'):
        return None, 'send_to must be "bureaus" or "creditors"'

    creditor_addresses = config.get('creditor_addresses', [])
    if send_to == 'creditors':
        if not creditor_addresses:
            return None, 'creditor_addresses required when send_to is "creditors"'
        for i, cred in enumerate(creditor_addresses):
            for field in ('name', 'address1', 'city', 'state', 'zip'):
                if not cred.get(field, '').strip():
                    return None, f'Creditor {i+1} missing required field: {field}'

    # Optional custom letter override
    custom_letter_id = config.get('custom_letter_id')
    if custom_letter_id is not None:
        if not isinstance(custom_letter_id, int):
            return None, 'custom_letter_id must be an integer'
        from models import CustomLetter
        from flask_login import current_user
        cl = CustomLetter.query.get(custom_letter_id)
        if not cl or cl.user_id != current_user.id:
            return None, 'Custom letter not found or not yours'

    # Mail options
    mail_options = config.get('mail_options', {})
    valid_mail_classes = ('usps_first_class', 'usps_standard', 'usps_priority_mail', 'usps_priority_mail_express')
    valid_service_levels = ('', 'certified', 'certified_return_receipt')
    if mail_options:
        if not isinstance(mail_options, dict):
            return None, 'mail_options must be a dict'
        mc = mail_options.get('mail_class', 'usps_first_class')
        if mc not in valid_mail_classes:
            return None, f'Invalid mail_class. Valid: {", ".join(valid_mail_classes)}'
        sl = mail_options.get('servicelevel', '')
        if sl not in valid_service_levels:
            return None, f'Invalid servicelevel. Valid: {", ".join(valid_service_levels)}'

    cleaned = {
        'mode': mode,
        'strategy': strategy,
        'max_rounds': max_rounds,
        'round_packs': round_packs,
        'send_to': send_to,
        'creditor_addresses': creditor_addresses if send_to == 'creditors' else [],
        'batch_size': batch_size if batch_size else {},
        'mail_options': mail_options if mail_options else {},
    }
    if custom_letter_id is not None:
        cleaned['custom_letter_id'] = custom_letter_id

    return cleaned, None


@pipeline_bp.route('/pipeline/start', methods=['POST'])
@login_required
def start_pipeline():
    """Start an autonomous dispute pipeline for a client."""
    data = request.get_json()
    client_id = data.get('client_id')

    if not client_id:
        return jsonify({'error': 'client_id is required'}), 400

    client = Client.query.get(client_id)
    if not client or client.business_user_id != current_user.id:
        return jsonify({'error': 'Client not found or unauthorized'}), 404

    if not client.pdf_filename:
        return jsonify({'error': 'No credit report PDF uploaded for this client'}), 400

    # Check for existing active pipeline
    active = DisputePipeline.query.filter(
        DisputePipeline.client_id == client_id,
        DisputePipeline.state.notin_(['completed', 'failed']),
    ).first()

    if active:
        return jsonify({
            'error': 'Client already has an active pipeline',
            'pipeline_id': active.id,
            'state': active.state,
        }), 409

    # Validate agent config
    config = data.get('config')
    if config:
        config, error = _validate_config(config)
        if error:
            return jsonify({'error': error}), 400

    # Create and start the pipeline
    pipeline = create_pipeline(client_id, current_user.id, config=config)

    # Advance in background
    _advance(pipeline.id)

    return jsonify({
        'pipeline_id': pipeline.id,
        'state': pipeline.state,
        'mode': (config or {}).get('mode', 'supervised'),
        'message': 'Pipeline started successfully',
    }), 201


@pipeline_bp.route('/pipeline/<int:pipeline_id>/status', methods=['GET'])
@login_required
def pipeline_status(pipeline_id):
    """Get the current status of a pipeline."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    status = get_pipeline_status(pipeline_id)
    return jsonify(status)


@pipeline_bp.route('/pipeline/<int:pipeline_id>/config', methods=['GET'])
@login_required
def pipeline_config(pipeline_id):
    """Get the agent config for a pipeline."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    config = _get_agent_config(pipeline)
    return jsonify(config or {})


@pipeline_bp.route('/pipeline/<int:pipeline_id>/approve', methods=['POST'])
@login_required
def approve_pipeline(pipeline_id):
    """Approve all draft letters in a pipeline at the review stage."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    if pipeline.state != 'review':
        return jsonify({'error': f'Pipeline is in "{pipeline.state}" state, not "review"'}), 400

    success = approve_pipeline_letters(pipeline_id)
    if success:
        # Run delivery in background so the HTTP response returns immediately
        _advance(pipeline_id)
        return jsonify({'message': 'Letters approved. Delivery started.'})
    else:
        return jsonify({'error': 'Failed to approve letters'}), 500


@pipeline_bp.route('/pipeline/<int:pipeline_id>/response', methods=['POST'])
@login_required
def upload_response(pipeline_id):
    """Upload a bureau response letter for a specific dispute account."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    account_id = request.form.get('account_id', type=int)
    response_type = request.form.get('response_type')
    file = request.files.get('response_file')

    if not account_id or not response_type:
        return jsonify({'error': 'account_id and response_type are required'}), 400

    account = DisputeAccount.query.get(account_id)
    if not account or account.pipeline_id != pipeline_id:
        return jsonify({'error': 'Account not found in this pipeline'}), 404

    # Save the response file if provided
    filename = ''
    if file and file.filename:
        filename = secure_filename(f"response_{account_id}_{file.filename}")
        if cloud_configured():
            result = upload_file(file, folder=f"clients/{pipeline.client_id}/responses", resource_type="raw")
            if result:
                filename = result['secure_url']
        else:
            upload_folder = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
            file.save(os.path.join(upload_folder, filename))

    # Create response record
    response = BureauResponse(
        dispute_account_id=account_id,
        filename=filename,
        response_type=response_type,
    )
    db.session.add(response)

    # Update account outcome
    account.outcome = response_type
    account.response_received_at = datetime.utcnow()

    # Auto-research: run legal research for escalation-worthy outcomes
    if response_type in ('verified', 'stall', 'no_response'):
        try:
            from services.legal_research import research_dispute
            import json as _json
            package = research_dispute(
                company_name=account.account_name,
                bureau_response=response_type,
                round_number=account.round_number,
            )
            # Cache research in the response record for use in next round's letter generation
            response.analysis_json = _json.dumps({
                'cfpb_summary': package.get('cfpb_summary'),
                'case_law': package.get('case_law'),
                'fcra_citation': package.get('fcra_citation'),
                'prompt_context': package.get('prompt_context', ''),
            }, default=str)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Auto-research failed for account {account_id}: {e}")

    db.session.commit()

    # Check if all accounts in the round have responses
    round_accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline_id,
        round_number=pipeline.round_number,
    ).all()

    all_responded = all(a.outcome != 'pending' for a in round_accounts)

    # Evaluate business rules on individual response
    rules_executed = []
    try:
        from services.rules_engine import evaluate_rules
        rules_executed = evaluate_rules(current_user.id, 'response_received', {
            'account_name': account.account_name,
            'account_number': account.account_number,
            'bureau': account.bureau,
            'outcome': response_type,
            'round_number': account.round_number,
            'pipeline_id': pipeline_id,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Rules evaluation failed: {e}")

    if all_responded and pipeline.state == 'awaiting_response':
        pipeline.state = 'response_received'
        pipeline.updated_at = datetime.utcnow()
        db.session.commit()

        _advance(pipeline.id)

    return jsonify({
        'message': 'Response recorded',
        'account_outcome': response_type,
        'all_responded': all_responded,
        'rules_executed': rules_executed,
    })


@pipeline_bp.route('/pipeline/<int:pipeline_id>/cancel', methods=['POST'])
@login_required
def cancel_pipeline(pipeline_id):
    """Cancel a running pipeline."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    if pipeline.state in ('completed', 'failed'):
        return jsonify({'error': 'Pipeline already terminated'}), 400

    pipeline.state = 'failed'
    pipeline.error_message = 'Cancelled by user'
    pipeline.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'message': 'Pipeline cancelled'})


@pipeline_bp.route('/pipeline/<int:pipeline_id>/delete', methods=['DELETE'])
@login_required
def delete_pipeline(pipeline_id):
    """Delete a pipeline and all its associated records."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    # Delete associated records (bureau responses link to accounts, not pipeline)
    from models import PipelineTask
    accounts = DisputeAccount.query.filter_by(pipeline_id=pipeline_id).all()
    for acct in accounts:
        BureauResponse.query.filter_by(dispute_account_id=acct.id).delete()
    DisputeAccount.query.filter_by(pipeline_id=pipeline_id).delete()
    PipelineTask.query.filter_by(pipeline_id=pipeline_id).delete()
    db.session.delete(pipeline)
    db.session.commit()

    return jsonify({'message': 'Pipeline deleted'})


@pipeline_bp.route('/pipeline/list', methods=['GET'])
@login_required
def list_pipelines():
    """List all pipelines for the current user."""
    pipelines = DisputePipeline.query.filter_by(user_id=current_user.id).order_by(
        DisputePipeline.created_at.desc()
    ).all()

    return jsonify([
        {
            'id': p.id,
            'client_id': p.client_id,
            'client_name': f"{p.client.first_name} {p.client.last_name}" if p.client else 'Unknown',
            'state': p.state,
            'round_number': p.round_number,
            'mode': _get_agent_config(p).get('mode', 'supervised'),
            'created_at': p.created_at.isoformat() if p.created_at else None,
            'updated_at': p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in pipelines
    ])


@pipeline_bp.route('/pipeline/letter/<int:letter_id>', methods=['GET'])
@login_required
def get_letter(letter_id):
    """Get the text of a dispute letter for viewing/editing."""
    letter = ClientDisputeLetter.query.get(letter_id)
    if not letter:
        return jsonify({'error': 'Letter not found'}), 404

    # Verify ownership through the client → pipeline chain
    client = Client.query.get(letter.client_id)
    if not client or client.business_user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    return jsonify({
        'id': letter.id,
        'letter_text': letter.letter_text,
        'status': letter.status,
        'template_name': letter.template_name,
        'created_at': letter.created_at.isoformat() if letter.created_at else None,
    })


@pipeline_bp.route('/pipeline/letter/<int:letter_id>', methods=['PUT'])
@login_required
def update_letter(letter_id):
    """Update the text of a draft dispute letter."""
    letter = ClientDisputeLetter.query.get(letter_id)
    if not letter:
        return jsonify({'error': 'Letter not found'}), 404

    client = Client.query.get(letter.client_id)
    if not client or client.business_user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    if letter.status != 'Draft':
        return jsonify({'error': f'Cannot edit a letter with status "{letter.status}"'}), 400

    data = request.get_json()
    new_text = data.get('letter_text')
    if not new_text or not new_text.strip():
        return jsonify({'error': 'letter_text is required'}), 400

    letter.letter_text = new_text.strip()
    db.session.commit()

    return jsonify({'message': 'Letter updated', 'id': letter.id})


@pipeline_bp.route('/pipeline/<int:pipeline_id>/next-round', methods=['POST'])
@login_required
def start_next_round(pipeline_id):
    """
    Advance a pipeline from round_review into the next round.
    User must explicitly trigger this — the pipeline never auto-advances between rounds.
    """
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline:
        return jsonify({'error': 'Pipeline not found'}), 404
    if pipeline.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    if pipeline.state != 'round_review':
        return jsonify({'error': f'Pipeline is in "{pipeline.state}" state, not "round_review"'}), 400

    agent_config = _get_agent_config(pipeline)
    max_rounds = agent_config.get('max_rounds', 3)

    if pipeline.round_number >= max_rounds:
        return jsonify({'error': f'Already at max rounds ({max_rounds})'}), 400

    # Optionally accept updated round_packs for the next round
    data = request.get_json() or {}
    if 'round_packs' in data:
        new_packs = data['round_packs']
        if isinstance(new_packs, list) and all(p in VALID_PACKS for p in new_packs):
            agent_config['round_packs'] = new_packs
            strategy = json.loads(pipeline.strategy_json or '{}')
            strategy['agent_config'] = agent_config
            pipeline.strategy_json = json.dumps(strategy)

    # Increment round and advance to strategy
    pipeline.round_number += 1
    pipeline.state = 'strategy'
    db.session.commit()

    # Kick off the pipeline in background
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Starting Round {pipeline.round_number} for pipeline {pipeline.id}")

    thread = threading.Thread(
        target=_run_pipeline_bg,
        args=(pipeline.id,),
        daemon=True,
    )
    thread.start()

    return jsonify({
        'message': f'Round {pipeline.round_number} started',
        'pipeline_id': pipeline.id,
        'round_number': pipeline.round_number,
    })


@pipeline_bp.route('/pipeline/<int:pipeline_id>/rounds', methods=['GET'])
@login_required
def get_pipeline_rounds(pipeline_id):
    """Return accounts and letters grouped by round number."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    accounts = DisputeAccount.query.filter_by(pipeline_id=pipeline_id).order_by(
        DisputeAccount.round_number, DisputeAccount.created_at
    ).all()

    rounds = {}
    for acct in accounts:
        rn = acct.round_number or 1
        if rn not in rounds:
            rounds[rn] = {'round_number': rn, 'accounts': [], 'letters': []}
        letter_data = None
        if acct.letter:
            letter_data = {
                'id': acct.letter.id,
                'status': acct.letter.status,
                'delivery_status': acct.letter.delivery_status,
                'mail_class': acct.letter.mail_class,
                'mailed_at': acct.letter.mailed_at.isoformat() if acct.letter.mailed_at else None,
                'tracking_number': acct.letter.tracking_number,
                'docupost_cost': acct.letter.docupost_cost,
            }
            rounds[rn]['letters'].append(letter_data)
        rounds[rn]['accounts'].append({
            'id': acct.id,
            'account_name': acct.account_name,
            'account_number': acct.account_number,
            'bureau': acct.bureau,
            'status': acct.status,
            'issue': acct.issue,
            'balance': acct.balance,
            'outcome': acct.outcome,
            'mailed_at': acct.mailed_at.isoformat() if acct.mailed_at else None,
            'letter': letter_data,
        })

    return jsonify({
        'pipeline_id': pipeline_id,
        'current_round': pipeline.round_number,
        'rounds': [rounds[k] for k in sorted(rounds.keys())],
    })


# ─── Letter Tracking ───

@pipeline_bp.route('/pipeline/<int:pipeline_id>/refresh-tracking', methods=['POST'])
@login_required
def refresh_tracking(pipeline_id):
    """Poll DocuPost for delivery status updates on all letters in this pipeline."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    from services.tracking import poll_letter_status
    accounts = DisputeAccount.query.filter_by(pipeline_id=pipeline_id).all()
    results = []
    for acct in accounts:
        if acct.letter and acct.letter.docupost_letter_id:
            r = poll_letter_status(acct.letter.id, user_id=current_user.id)
            results.append({
                'account_id': acct.id,
                'account_name': acct.account_name,
                'delivery_status': r.get('status'),
                'tracking_number': r.get('tracking_number'),
                'updated': r.get('updated', False),
            })
    return jsonify({'results': results})


@pipeline_bp.route('/pipeline/<int:pipeline_id>/tracking', methods=['GET'])
@login_required
def get_tracking(pipeline_id):
    """Return tracking data for all letters, grouped by round."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    accounts = DisputeAccount.query.filter_by(pipeline_id=pipeline_id).order_by(
        DisputeAccount.round_number, DisputeAccount.created_at
    ).all()

    rounds = {}
    for acct in accounts:
        rn = acct.round_number or 1
        if rn not in rounds:
            rounds[rn] = []
        ltr = acct.letter
        rounds[rn].append({
            'account_name': acct.account_name,
            'bureau': acct.bureau,
            'round_number': rn,
            'delivery_status': ltr.delivery_status if ltr else None,
            'mail_class': ltr.mail_class if ltr else None,
            'service_level': ltr.service_level if ltr else None,
            'tracking_number': ltr.tracking_number if ltr else None,
            'mailed_at': ltr.mailed_at.isoformat() if ltr and ltr.mailed_at else None,
            'last_updated': ltr.delivery_status_updated_at.isoformat() if ltr and ltr.delivery_status_updated_at else None,
            'cost': ltr.docupost_cost if ltr else None,
        })

    return jsonify({
        'pipeline_id': pipeline_id,
        'rounds': {str(k): v for k, v in sorted(rounds.items())},
    })


# ─── Supporting Docs ───

@pipeline_bp.route('/pipeline/<int:pipeline_id>/account/<int:account_id>/docs', methods=['GET'])
@login_required
def list_account_docs(pipeline_id, account_id):
    """List supporting docs for a specific dispute account."""
    account = DisputeAccount.query.get(account_id)
    if not account or account.pipeline_id != pipeline_id:
        return jsonify({'error': 'Account not found'}), 404

    docs = SupportingDoc.query.filter_by(dispute_account_id=account_id).order_by(
        SupportingDoc.uploaded_at.desc()
    ).all()

    return jsonify([{
        'id': d.id,
        'filename': d.filename,
        'doc_type': d.doc_type,
        'description': d.description,
        'include_in_package': d.include_in_package,
        'uploaded_at': d.uploaded_at.isoformat() if d.uploaded_at else None,
    } for d in docs])


@pipeline_bp.route('/pipeline/<int:pipeline_id>/account/<int:account_id>/docs', methods=['POST'])
@login_required
def upload_account_doc(pipeline_id, account_id):
    """Upload a supporting document for a dispute account."""
    account = DisputeAccount.query.get(account_id)
    if not account or account.pipeline_id != pipeline_id:
        return jsonify({'error': 'Account not found'}), 404

    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': 'No file provided'}), 400

    filename = secure_filename(f"{account_id}_{file.filename}")

    if cloud_configured():
        result = upload_file(file, folder=f"clients/{pipeline.client_id}/supporting_docs", resource_type="raw")
        filepath = result['secure_url'] if result else ''
    else:
        upload_folder = os.path.join(
            os.environ.get('UPLOAD_FOLDER', 'static/uploads'),
            str(pipeline.client_id), 'supporting_docs'
        )
        os.makedirs(upload_folder, exist_ok=True)
        filepath = os.path.join(upload_folder, filename)
        file.save(filepath)

    doc = SupportingDoc(
        user_id=current_user.id,
        client_id=pipeline.client_id,
        dispute_account_id=account_id,
        round_number=pipeline.round_number,
        filename=filename,
        file_url=filepath,
        doc_type=request.form.get('doc_type', 'other'),
        description=request.form.get('description', ''),
        include_in_package=request.form.get('include_in_package', 'true').lower() == 'true',
    )
    db.session.add(doc)
    db.session.commit()

    return jsonify({'ok': True, 'id': doc.id, 'filename': doc.filename})


@pipeline_bp.route('/pipeline/doc/<int:doc_id>', methods=['DELETE'])
@login_required
def delete_account_doc(doc_id):
    """Delete a supporting document."""
    doc = SupportingDoc.query.get(doc_id)
    if not doc or doc.user_id != current_user.id:
        return jsonify({'error': 'Document not found'}), 404

    # Remove file — Cloudinary or local
    if doc.file_url:
        if doc.file_url.startswith('http'):
            delete_file(doc.file_url)
        elif os.path.exists(doc.file_url):
            os.remove(doc.file_url)

    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════
#  Creditor Intelligence API
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  Dispute Timeline
# ═══════════════════════════════════════════════════════════

@pipeline_bp.route('/pipeline/<int:pipeline_id>/timeline', methods=['GET'])
@login_required
def pipeline_timeline(pipeline_id):
    """Generate a chronological timeline of all dispute events for evidence/court use."""
    from flask import render_template

    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    client = Client.query.get(pipeline.client_id)
    events = []

    # Pipeline creation
    events.append({
        'date': pipeline.created_at,
        'type': 'pipeline_started',
        'title': 'Dispute Pipeline Initiated',
        'detail': f"Mode: {pipeline.mode or 'supervised'}",
        'round': 0,
        'icon': 'rocket',
        'color': 'blue',
    })

    # Get all accounts across all rounds
    accounts = DisputeAccount.query.filter_by(pipeline_id=pipeline_id).order_by(
        DisputeAccount.round_number, DisputeAccount.created_at
    ).all()

    rounds_seen = set()
    for acct in accounts:
        # Round start event (first account of each round)
        if acct.round_number not in rounds_seen:
            rounds_seen.add(acct.round_number)
            if acct.round_number > 1:
                events.append({
                    'date': acct.created_at,
                    'type': 'round_started',
                    'title': f'Round {acct.round_number} Escalation',
                    'detail': f'Strategy: {acct.template_pack or "default"}',
                    'round': acct.round_number,
                    'icon': 'escalate',
                    'color': 'purple',
                })

        # Dispute filed
        events.append({
            'date': acct.created_at,
            'type': 'dispute_filed',
            'title': f'Dispute Filed: {acct.account_name}',
            'detail': f'{acct.bureau} · Round {acct.round_number} · Pack: {acct.template_pack or "default"}',
            'round': acct.round_number,
            'icon': 'letter',
            'color': 'blue',
        })

        # Letter mailed
        if acct.letter and acct.letter.mailed_at:
            events.append({
                'date': acct.letter.mailed_at,
                'type': 'letter_mailed',
                'title': f'Letter Mailed: {acct.account_name}',
                'detail': f'{acct.bureau} · {acct.letter.mail_class or "USPS"} · Tracking: {acct.letter.tracking_number or "N/A"}',
                'round': acct.round_number,
                'icon': 'mail',
                'color': 'green',
            })

        # Letter delivered
        if acct.letter and acct.letter.delivery_status == 'delivered':
            events.append({
                'date': acct.letter.delivery_status_updated_at or acct.letter.mailed_at,
                'type': 'letter_delivered',
                'title': f'Letter Delivered: {acct.account_name}',
                'detail': f'{acct.bureau}',
                'round': acct.round_number,
                'icon': 'delivered',
                'color': 'green',
            })

        # Response received
        if acct.response_received_at:
            events.append({
                'date': acct.response_received_at,
                'type': 'response_received',
                'title': f'Response: {acct.account_name}',
                'detail': f'{acct.bureau} · Outcome: {acct.outcome or "pending"}',
                'round': acct.round_number,
                'icon': 'response',
                'color': 'green' if acct.outcome in ('removed', 'updated') else 'red' if acct.outcome == 'verified' else 'orange',
            })

        # Bureau responses with research
        for resp in acct.responses:
            if resp.analysis_json and resp.analysis_json != '{}':
                events.append({
                    'date': resp.uploaded_at,
                    'type': 'research_completed',
                    'title': f'Legal Research: {acct.account_name}',
                    'detail': 'CFPB complaint data + case law cached for escalation',
                    'round': acct.round_number,
                    'icon': 'research',
                    'color': 'purple',
                })

    # Sort by date
    events.sort(key=lambda e: e['date'] if e['date'] else datetime.min)

    # Summary stats
    total_letters = len([a for a in accounts if a.letter])
    total_mailed = len([a for a in accounts if a.letter and a.letter.mailed_at])
    total_removed = len([a for a in accounts if a.outcome == 'removed'])
    total_updated = len([a for a in accounts if a.outcome == 'updated'])
    max_round = max((a.round_number for a in accounts), default=1)

    # Check if template exists, otherwise return JSON
    try:
        return render_template('dispute_timeline.html',
                               pipeline=pipeline,
                               client=client,
                               events=events,
                               total_letters=total_letters,
                               total_mailed=total_mailed,
                               total_removed=total_removed,
                               total_updated=total_updated,
                               max_round=max_round)
    except Exception:
        # Fallback to JSON if template doesn't exist yet
        return jsonify({
            'events': [{
                'date': e['date'].isoformat() if e['date'] else None,
                'type': e['type'],
                'title': e['title'],
                'detail': e['detail'],
                'round': e['round'],
                'color': e['color'],
            } for e in events],
            'summary': {
                'total_letters': total_letters,
                'total_mailed': total_mailed,
                'total_removed': total_removed,
                'total_updated': total_updated,
                'max_round': max_round,
            }
        })


# ═══════════════════════════════════════════════════════════
#  Batch Response Processing
# ═══════════════════════════════════════════════════════════

@pipeline_bp.route('/pipeline/<int:pipeline_id>/batch-response', methods=['POST'])
@login_required
def batch_upload_responses(pipeline_id):
    """Upload multiple response files at once — auto-classify and match to accounts."""
    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline or pipeline.user_id != current_user.id:
        return jsonify({'error': 'Pipeline not found'}), 404

    files = request.files.getlist('response_files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    # Get pending accounts for this round
    pending_accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline_id,
        round_number=pipeline.round_number,
        outcome='pending',
    ).all()

    if not pending_accounts:
        return jsonify({'error': 'No pending accounts in this round'}), 400

    import tempfile
    from services.response_classifier import classify_response_file

    matched = []
    unmatched = []

    for file in files:
        if not file or not file.filename:
            continue

        # Save to temp file for processing
        ext = os.path.splitext(file.filename)[1].lower()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        file.save(tmp.name)
        tmp.close()

        try:
            result = classify_response_file(tmp.name, pending_accounts)
            if result and result['match_confidence'] >= 0.3:
                # Upload the file to permanent storage
                filename = secure_filename(f"response_{result['account_id']}_{file.filename}")
                if cloud_configured():
                    from services.cloud_storage import upload_file
                    upload_result = upload_file(
                        open(tmp.name, 'rb'),
                        folder=f"clients/{pipeline.client_id}/responses",
                        resource_type="raw",
                    )
                    if upload_result:
                        filename = upload_result['secure_url']

                # Create BureauResponse record
                response = BureauResponse(
                    dispute_account_id=result['account_id'],
                    filename=filename,
                    response_type=result['outcome'],
                )
                db.session.add(response)

                # Update account outcome
                account = DisputeAccount.query.get(result['account_id'])
                if account:
                    account.outcome = result['outcome']
                    account.response_received_at = datetime.utcnow()

                    # Auto-research for escalation-worthy outcomes
                    if result['outcome'] in ('verified', 'stall', 'no_response'):
                        try:
                            from services.legal_research import research_dispute
                            package = research_dispute(
                                company_name=account.account_name,
                                bureau_response=result['outcome'],
                                round_number=account.round_number,
                            )
                            response.analysis_json = json.dumps({
                                'cfpb_summary': package.get('cfpb_summary'),
                                'case_law': package.get('case_law'),
                                'fcra_citation': package.get('fcra_citation'),
                                'prompt_context': package.get('prompt_context', ''),
                            }, default=str)
                        except Exception:
                            pass

                # Remove from pending list so it's not matched again
                pending_accounts = [a for a in pending_accounts if a.id != result['account_id']]

                matched.append({
                    'filename': file.filename,
                    'account_name': result['account_name'],
                    'outcome': result['outcome'],
                    'match_confidence': result['match_confidence'],
                    'outcome_confidence': result['outcome_confidence'],
                })
            else:
                unmatched.append({'filename': file.filename, 'reason': 'Could not match to any pending account'})
        except Exception as e:
            unmatched.append({'filename': file.filename, 'reason': str(e)[:100]})
        finally:
            os.unlink(tmp.name)

    db.session.commit()

    # Check if all accounts now have responses
    all_accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline_id,
        round_number=pipeline.round_number,
    ).all()
    all_responded = all(a.outcome != 'pending' for a in all_accounts)

    if all_responded and pipeline.state == 'awaiting_response':
        pipeline.state = 'response_received'
        pipeline.updated_at = datetime.utcnow()
        db.session.commit()
        _advance(pipeline.id)

    return jsonify({
        'matched': matched,
        'unmatched': unmatched,
        'all_responded': all_responded,
        'remaining_pending': len([a for a in all_accounts if a.outcome == 'pending']),
    })


# ═══════════════════════════════════════════════════════════
#  Creditor Intelligence API
# ═══════════════════════════════════════════════════════════

@pipeline_bp.route('/creditor-profile/<path:creditor_name>', methods=['GET'])
@login_required
def get_creditor_profile(creditor_name):
    """Get creditor intelligence profile for a given creditor name."""
    from services.creditor_intelligence import get_creditor_recommendation, normalize_creditor_name
    from models import CreditorProfile

    normalized = normalize_creditor_name(creditor_name)
    profile = CreditorProfile.query.filter_by(
        business_user_id=current_user.id,
        creditor_name=normalized,
    ).first()

    if not profile:
        return jsonify({'found': False, 'creditor_name': normalized})

    recommendation = get_creditor_recommendation(current_user.id, creditor_name)

    return jsonify({
        'found': True,
        'creditor_name': normalized,
        'total_disputes': profile.total_disputes,
        'removed_count': profile.removed_count,
        'updated_count': profile.updated_count,
        'verified_count': profile.verified_count,
        'no_response_count': profile.no_response_count,
        'win_rate': round((profile.removed_count + profile.updated_count) / max(profile.total_disputes, 1) * 100, 1),
        'avg_rounds_to_remove': round(profile.avg_rounds_to_remove, 1) if profile.avg_rounds_to_remove else None,
        'best_pack': profile.best_pack,
        'cfpb_complaint_count': profile.cfpb_complaint_count,
        'cfpb_win_rate': profile.cfpb_win_rate,
        'recommendation': recommendation,
    })


@pipeline_bp.route('/creditor-profiles', methods=['GET'])
@login_required
def list_creditor_profiles():
    """List all creditor profiles for the current business user."""
    from models import CreditorProfile

    profiles = CreditorProfile.query.filter_by(
        business_user_id=current_user.id,
    ).order_by(CreditorProfile.total_disputes.desc()).all()

    return jsonify({
        'profiles': [{
            'creditor_name': p.creditor_name,
            'total_disputes': p.total_disputes,
            'removed_count': p.removed_count,
            'verified_count': p.verified_count,
            'win_rate': round((p.removed_count + p.updated_count) / max(p.total_disputes, 1) * 100, 1),
            'best_pack': p.best_pack,
            'avg_rounds': round(p.avg_rounds_to_remove, 1) if p.avg_rounds_to_remove else None,
        } for p in profiles],
        'total': len(profiles),
    })


@pipeline_bp.route('/creditor-profiles/rebuild', methods=['POST'])
@login_required
def rebuild_creditor_profiles():
    """Rebuild all creditor profiles from historical dispute data."""
    from services.creditor_intelligence import rebuild_all_profiles

    count = rebuild_all_profiles(current_user.id)
    return jsonify({'ok': True, 'accounts_processed': count})


# ═══════════════════════════════════════════════════════════
#  Business Rules API
# ═══════════════════════════════════════════════════════════

@pipeline_bp.route('/rules', methods=['GET'])
@login_required
def list_rules():
    """List all business rules for the current user."""
    from models import BusinessRule

    rules = BusinessRule.query.filter_by(user_id=current_user.id).order_by(BusinessRule.created_at).all()
    return jsonify({
        'rules': [{
            'id': r.id,
            'name': r.name,
            'trigger': r.trigger,
            'conditions': json.loads(r.conditions_json or '{}'),
            'action': r.action,
            'action_config': json.loads(r.action_config_json or '{}'),
            'enabled': r.enabled,
        } for r in rules],
    })


@pipeline_bp.route('/rules', methods=['POST'])
@login_required
def create_rule():
    """Create a new business rule."""
    from models import BusinessRule

    data = request.get_json()
    if not data or not data.get('name') or not data.get('trigger') or not data.get('action'):
        return jsonify({'error': 'name, trigger, and action are required'}), 400

    rule = BusinessRule(
        user_id=current_user.id,
        name=data['name'],
        trigger=data['trigger'],
        conditions_json=json.dumps(data.get('conditions', {})),
        action=data['action'],
        action_config_json=json.dumps(data.get('action_config', {})),
        enabled=data.get('enabled', True),
    )
    db.session.add(rule)
    db.session.commit()

    return jsonify({'ok': True, 'rule_id': rule.id})


@pipeline_bp.route('/rules/<int:rule_id>', methods=['PUT'])
@login_required
def update_rule(rule_id):
    """Update a business rule."""
    from models import BusinessRule

    rule = BusinessRule.query.get(rule_id)
    if not rule or rule.user_id != current_user.id:
        return jsonify({'error': 'Rule not found'}), 404

    data = request.get_json()
    if 'name' in data:
        rule.name = data['name']
    if 'trigger' in data:
        rule.trigger = data['trigger']
    if 'conditions' in data:
        rule.conditions_json = json.dumps(data['conditions'])
    if 'action' in data:
        rule.action = data['action']
    if 'action_config' in data:
        rule.action_config_json = json.dumps(data['action_config'])
    if 'enabled' in data:
        rule.enabled = data['enabled']

    db.session.commit()
    return jsonify({'ok': True})


@pipeline_bp.route('/rules/<int:rule_id>', methods=['DELETE'])
@login_required
def delete_rule(rule_id):
    """Delete a business rule."""
    from models import BusinessRule

    rule = BusinessRule.query.get(rule_id)
    if not rule or rule.user_id != current_user.id:
        return jsonify({'error': 'Rule not found'}), 404

    db.session.delete(rule)
    db.session.commit()
    return jsonify({'ok': True})


@pipeline_bp.route('/rules/presets', methods=['POST'])
@login_required
def create_preset_rules_endpoint():
    """Create default preset rules for the current user."""
    from services.rules_engine import create_preset_rules

    create_preset_rules(current_user.id)
    return jsonify({'ok': True, 'message': 'Preset rules created (disabled by default)'})
