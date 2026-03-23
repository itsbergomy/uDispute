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
                logger.info(f"[BG Thread] Advancing pipeline {pipeline_id}")
                advance_pipeline(pipeline_id)
                logger.info(f"[BG Thread] Pipeline {pipeline_id} advanced OK")
        except Exception:
            logger.exception(f"[BG Thread] Pipeline {pipeline_id} failed")
            # Mark pipeline as failed so the UI shows an error
            try:
                with app.app_context():
                    pipe = DisputePipeline.query.get(pipeline_id)
                    if pipe and pipe.state not in ('completed', 'failed'):
                        pipe.state = 'failed'
                        pipe.error_message = 'Background processing error — check server logs'
                        db.session.commit()
            except Exception:
                pass

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
    db.session.commit()

    # Check if all accounts in the round have responses
    round_accounts = DisputeAccount.query.filter_by(
        pipeline_id=pipeline_id,
        round_number=pipeline.round_number,
    ).all()

    all_responded = all(a.outcome != 'pending' for a in round_accounts)
    if all_responded and pipeline.state == 'awaiting_response':
        pipeline.state = 'response_received'
        pipeline.updated_at = datetime.utcnow()
        db.session.commit()

        _advance(pipeline.id)

    return jsonify({
        'message': 'Response recorded',
        'account_outcome': response_type,
        'all_responded': all_responded,
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
