"""
Business dashboard blueprint — client management, analysis, uDispute, custom letters.
Extracted from dispute_ui.py.
"""

import os
import json
from datetime import datetime
from flask import (
    Blueprint, request, render_template, flash, redirect,
    url_for, session, send_from_directory, abort, current_app, jsonify, make_response
)
from flask_login import login_required, current_user
from flask_mail import Message as MailMessage
from werkzeug.utils import secure_filename

from models import (
    db, Client, ClientReportAnalysis, ClientDisputeLetter,
    WorkflowSetting, CustomLetter, MessageThread, Message,
    Correspondence, DisputePipeline, ClientPortalToken, SupportingDoc
)
from services.pdf_parser import extract_negative_items_from_pdf
from services.report_analyzer import run_report_analysis
from services.letter_generator import PACKS, generate_letter, build_prompt, build_notice_of_dispute_prompt, letter_to_pdf, image_to_pdf, merge_dispute_package, generate_dual_letters, build_dual_prompts
from services.cloud_storage import upload_file, get_file_url, download_to_temp, delete_file, is_configured as cloud_configured
from config import mail

business_bp = Blueprint('business', __name__)


@business_bp.before_request
@login_required
def require_business_plan():
    """Gate all business routes to business-plan users only."""
    if current_user.plan != 'business':
        flash('Business plan required.', 'error')
        return redirect(url_for('disputes.index'))


@business_bp.route('/business-dashboard')
@login_required
def business_dashboard():
    client_id = request.args.get('client_id', type=int)
    clients = Client.query.filter_by(business_user_id=current_user.id).all()

    selected_client = None
    workflow_enabled = False

    if client_id:
        selected_client = Client.query.get(client_id)
        if selected_client and selected_client.business_user_id == current_user.id:
            setting = WorkflowSetting.query.filter_by(
                client_id=client_id,
                key='cfpb_collection'
            ).first()
            if setting:
                workflow_enabled = setting.enabled

    total_clients = len(clients)
    total_workflows_enabled = WorkflowSetting.query.filter_by(
        business_user_id=current_user.id,
        enabled=True
    ).count()

    # Get pipeline statuses for each client
    pipelines = DisputePipeline.query.filter_by(user_id=current_user.id).order_by(
        DisputePipeline.created_at.desc()
    ).all()

    active_pipelines = sum(1 for p in pipelines if p.state not in ('completed', 'failed'))
    letters_sent = ClientDisputeLetter.query.join(Client).filter(
        Client.business_user_id == current_user.id
    ).count()

    stats = {
        'total_clients': total_clients,
        'active_pipelines': active_pipelines,
        'letters_sent': letters_sent,
        'workflows_enabled': total_workflows_enabled,
    }

    correspondence = []
    active_tab = request.args.get('tab', 'clients')

    custom_letters = CustomLetter.query.filter_by(user_id=current_user.id).all()

    return render_template("business_dashboard.html",
                           clients=clients,
                           selected_client=selected_client,
                           workflow_enabled=workflow_enabled,
                           stats=stats,
                           correspondence=correspondence,
                           active_tab=active_tab,
                           pipelines=pipelines,
                           custom_letters=custom_letters)


@business_bp.route('/clients/create', methods=['POST'])
@login_required
def create_client():
    first_name = request.form.get('first_name')
    last_name = request.form.get('last_name')
    email = request.form.get('email')

    if not all([first_name, last_name, email]):
        flash("First name, last name, and email are required.", "error")
        return redirect(url_for("business.business_dashboard"))

    client = Client(
        first_name=first_name,
        last_name=last_name,
        email=email,
        business_user_id=current_user.id,
        address_line1=request.form.get('address_line1', '').strip() or None,
        address_line2=request.form.get('address_line2', '').strip() or None,
        city=request.form.get('city', '').strip() or None,
        state=request.form.get('state', '').strip() or None,
        zip_code=request.form.get('zip_code', '').strip() or None,
        notes=request.form.get('notes', '').strip() or None,
    )
    db.session.add(client)
    db.session.commit()

    # Save uploaded files — Cloudinary if configured, otherwise local
    file_fields = {
        'pdf_file': 'pdf_filename',
        'id_file': 'id_filename',
        'ssn_file': 'ssn_filename',
        'utility_file': 'utility_filename',
    }
    # Image fields use resource_type="image" so Cloudinary serves them
    # inline; PDFs use "raw"
    _image_fields = {'id_file', 'ssn_file', 'utility_file'}
    if cloud_configured():
        for form_key, model_attr in file_fields.items():
            f = request.files.get(form_key)
            if f and f.filename:
                rtype = "image" if form_key in _image_fields else "raw"
                result = upload_file(f, folder=f"clients/{client.id}", resource_type=rtype)
                if result:
                    setattr(client, model_attr, result['secure_url'])
    else:
        upload_dir = current_app.config['UPLOAD_FOLDER']
        client_dir = os.path.join(upload_dir, str(client.id))
        os.makedirs(client_dir, exist_ok=True)
        for form_key, model_attr in file_fields.items():
            f = request.files.get(form_key)
            if f and f.filename:
                safe_name = secure_filename(f.filename)
                save_path = os.path.join(client_dir, safe_name)
                f.save(save_path)
                setattr(client, model_attr, safe_name)

    db.session.commit()

    thread = MessageThread(client_id=client.id)
    db.session.add(thread)
    db.session.commit()

    flash(f"Client {first_name} {last_name} created.", "success")
    return redirect(url_for("business.business_dashboard"))


@business_bp.route('/clients/<int:client_id>')
@login_required
def view_client(client_id):
    client = Client.query.get_or_404(client_id)

    # Clear stale parsed accounts if viewing a different client
    if session.get("parsed_accounts_client_id") != client_id:
        session.pop("client_parsed_accounts", None)
        session["parsed_accounts_client_id"] = client_id

    client_parsed_accounts = session.get("client_parsed_accounts", [])

    settings = WorkflowSetting.query.filter_by(client_id=client.id).all()
    workflow_settings = {s.key: s.enabled for s in settings}

    # Get active pipeline for this client
    active_pipeline = DisputePipeline.query.filter(
        DisputePipeline.client_id == client_id,
        DisputePipeline.state.notin_(['completed', 'failed']),
    ).first()

    pipeline_status = None
    if active_pipeline:
        from services.pipeline_engine import get_pipeline_status
        pipeline_status = get_pipeline_status(active_pipeline.id)

    # Client portal token
    portal_token = ClientPortalToken.query.filter_by(
        client_id=client.id, is_active=True
    ).first()

    # Supporting documents for this client
    supporting_docs = SupportingDoc.query.filter_by(
        client_id=client.id
    ).order_by(SupportingDoc.uploaded_at.desc()).all()

    # Client letters (Notices of Dispute, generated letters)
    letters = ClientDisputeLetter.query.filter_by(
        client_id=client.id
    ).order_by(ClientDisputeLetter.created_at.desc()).all()

    # Correspondence files
    docs = Correspondence.query.filter_by(
        client_id=client.id
    ).order_by(Correspondence.uploaded_at.desc()).all()

    return render_template("view_client.html",
                           client=client,
                           client_parsed_accounts=client_parsed_accounts,
                           workflow_settings=workflow_settings,
                           active_pipeline=active_pipeline,
                           pipeline_status=pipeline_status,
                           portal_token=portal_token,
                           supporting_docs=supporting_docs,
                           letters=letters,
                           docs=docs)


@business_bp.route('/clients/<int:client_id>/notes', methods=['GET', 'POST'])
@login_required
def client_notes(client_id):
    """AJAX endpoint — get or save client notes."""
    client = Client.query.get_or_404(client_id)
    if request.method == 'GET':
        return jsonify({'notes': client.notes or ''})
    # POST — save notes
    data = request.get_json(silent=True) or {}
    client.notes = data.get('notes', '').strip() or None
    db.session.commit()
    return jsonify({'ok': True, 'notes': client.notes or ''})


@business_bp.route('/clients/<int:client_id>/upload-correspondence', methods=['POST'])
@login_required
def upload_correspondence(client_id):
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    file = request.files.get('correspondence_file')

    if file:
        filename = secure_filename(file.filename)

        if cloud_configured():
            result = upload_file(file, folder=f"clients/{client.id}/correspondence", resource_type="raw")
            if result:
                new_file = Correspondence(
                    client_id=client.id,
                    user_id=current_user.id,
                    filename=filename,
                    file_url=result['secure_url'],
                )
                db.session.add(new_file)
                db.session.commit()
                flash("Correspondence uploaded.", "success")
        else:
            corr_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], str(client.id), 'correspondence')
            os.makedirs(corr_dir, exist_ok=True)
            filepath = os.path.join(corr_dir, filename)
            file.save(filepath)

            new_file = Correspondence(
                client_id=client.id,
                user_id=current_user.id,
                filename=filename,
                file_url=filepath,
            )
            db.session.add(new_file)
            db.session.commit()
            flash("Correspondence uploaded.", "success")

    return redirect(url_for('business.view_client', client_id=client_id))


@business_bp.route('/clients/<int:client_id>/correspondence/<filename>')
@login_required
def view_correspondence_file(client_id, filename):
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    # Check if this correspondence has a Cloudinary URL
    corr = Correspondence.query.filter_by(client_id=client_id, filename=filename).first()
    if corr and corr.file_url and corr.file_url.startswith('http'):
        return redirect(corr.file_url)

    corr_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], str(client_id), 'correspondence')
    return send_from_directory(corr_dir, filename)


# ── Supporting Documents — Multi-upload for Business Clients ──

@business_bp.route('/clients/<int:client_id>/upload-documents', methods=['POST'])
@login_required
def upload_supporting_docs(client_id):
    """Multi-file upload for supporting documents (evidence, prior correspondence, etc.)."""
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    files = request.files.getlist('supporting_files')
    doc_type = request.form.get('doc_type', 'other').strip()
    description = request.form.get('description', '').strip() or None
    include_in_package = request.form.get('include_in_package') == 'on'

    if not files or all(f.filename == '' for f in files):
        flash("No files selected.", "error")
        return redirect(url_for('business.view_client', client_id=client_id))

    count = 0
    for file in files:
        if not file or not file.filename:
            continue
        filename = secure_filename(file.filename)
        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        unique_name = f"{ts}_{filename}"

        if cloud_configured():
            result = upload_file(file, folder=f"clients/{client.id}/supporting_docs", resource_type="raw")
            if result:
                doc = SupportingDoc(
                    user_id=current_user.id,
                    client_id=client.id,
                    filename=unique_name,
                    file_url=result['secure_url'],
                    doc_type=doc_type,
                    description=description,
                    include_in_package=include_in_package
                )
                db.session.add(doc)
                count += 1
        else:
            doc_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], str(client.id), 'supporting_docs')
            os.makedirs(doc_dir, exist_ok=True)
            filepath = os.path.join(doc_dir, unique_name)
            file.save(filepath)

            doc = SupportingDoc(
                user_id=current_user.id,
                client_id=client.id,
                filename=unique_name,
                file_url=filepath,
                doc_type=doc_type,
                description=description,
                include_in_package=include_in_package
            )
            db.session.add(doc)
            count += 1

    db.session.commit()
    flash(f"{count} document{'s' if count != 1 else ''} uploaded.", "success")
    return redirect(url_for('business.view_client', client_id=client_id))


@business_bp.route('/clients/<int:client_id>/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
def delete_supporting_doc(client_id, doc_id):
    """Delete a supporting document."""
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    doc = SupportingDoc.query.get_or_404(doc_id)
    if doc.client_id != client.id:
        abort(403)

    # Remove file — Cloudinary or local
    if doc.file_url:
        if doc.file_url.startswith('http'):
            # Extract public_id from Cloudinary URL and delete
            delete_file(doc.file_url)
        elif os.path.exists(doc.file_url):
            os.remove(doc.file_url)

    db.session.delete(doc)
    db.session.commit()
    flash("Document removed.", "success")
    return redirect(url_for('business.view_client', client_id=client_id))


@business_bp.route('/clients/<int:client_id>/documents/<filename>')
@login_required
def view_supporting_doc_file(client_id, filename):
    """Serve a supporting document file."""
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    # Check if this doc has a Cloudinary URL
    doc = SupportingDoc.query.filter_by(client_id=client_id, filename=filename).first()
    if doc and doc.file_url and doc.file_url.startswith('http'):
        return redirect(doc.file_url)

    doc_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], str(client_id), 'supporting_docs')
    return send_from_directory(doc_dir, filename)


@business_bp.route('/clients/<int:client_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    client = Client.query.get_or_404(client_id)

    if client.business_user_id != current_user.id:
        flash("Unauthorized", "error")
        return redirect(url_for('business.business_dashboard'))

    if request.method == 'POST':
        client.first_name = request.form['first_name']
        client.last_name = request.form['last_name']
        client.email = request.form['email']
        client.address_line1 = request.form.get('address_line1')
        client.address_line2 = request.form.get('address_line2')
        client.city = request.form.get('city')
        client.state = request.form.get('state')
        client.zip_code = request.form.get('zip_code')
        client.round_status = request.form.get('round_status')
        client.notes = request.form.get('notes')

        uploads = [
            ('id_file', 'id_filename'),
            ('ssn_file', 'ssn_filename'),
            ('utility_file', 'utility_filename'),
            ('pdf_file', 'pdf_filename'),
        ]
        _img_fields = {'id_file', 'ssn_file', 'utility_file'}
        for field_name, model_attr in uploads:
            f = request.files.get(field_name)
            if f and f.filename:
                if cloud_configured():
                    rtype = "image" if field_name in _img_fields else "raw"
                    result = upload_file(f, folder=f"clients/{client.id}", resource_type=rtype)
                    if result:
                        setattr(client, model_attr, result['secure_url'])
                else:
                    filename = f"{client.id}_{field_name}_{secure_filename(f.filename)}"
                    full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
                    f.save(full_path)
                    setattr(client, model_attr, filename)

        db.session.commit()
        flash("Client updated", "success")
        return redirect(url_for('business.view_client', client_id=client.id))

    return render_template('edit_client.html', client=client)


@business_bp.route('/client-files/<int:client_id>/<filetype>')
@login_required
def client_file(client_id, filetype):
    c = Client.query.get_or_404(client_id)
    if c.business_user_id != current_user.id:
        abort(403)

    mapping = {
        'id': c.id_filename,
        'ssn': c.ssn_filename,
        'util': c.utility_filename,
        'pdf': c.pdf_filename
    }
    fn = mapping.get(filetype)
    if not fn:
        abort(404)

    # If it's a Cloudinary URL, proxy the file with correct Content-Type
    # so the browser displays PDFs inline instead of downloading
    if fn.startswith('http'):
        import requests as http_req
        try:
            resp = http_req.get(fn, timeout=15)
            response = make_response(resp.content)
            # Use the route's filetype param to determine content type
            type_map = {'pdf': 'application/pdf', 'id': 'image/jpeg', 'ssn': 'image/jpeg', 'util': 'image/jpeg'}
            ct = type_map.get(filetype) or resp.headers.get('Content-Type', 'application/pdf')
            response.headers['Content-Type'] = ct
            response.headers['Content-Disposition'] = 'inline'
            return response
        except Exception:
            return redirect(fn)

    # Files are saved in client-specific subdirectories; fall back to root for legacy files
    upload_dir = current_app.config['UPLOAD_FOLDER']
    client_dir = os.path.join(upload_dir, str(client_id))
    if os.path.exists(os.path.join(client_dir, fn)):
        return send_from_directory(client_dir, fn, as_attachment=False)
    return send_from_directory(upload_dir, fn, as_attachment=False)


@business_bp.route('/clients/<int:client_id>/run-analysis', methods=['POST'])
@login_required
def run_analysis_for_client(client_id):
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    if not client.pdf_filename:
        flash("No credit report uploaded!", "error")
        return redirect(url_for('business.view_client', client_id=client.id))

    if client.pdf_filename.startswith('http'):
        pdf_path = download_to_temp(client.pdf_filename)
        if not pdf_path:
            flash("Failed to download PDF from cloud storage.", "error")
            return redirect(url_for('business.view_client', client_id=client.id))
    else:
        pdf_path = os.path.join(current_app.config['UPLOAD_FOLDER'], str(client.id), client.pdf_filename)
        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(current_app.config['UPLOAD_FOLDER'], client.pdf_filename)

    analysis_data = run_report_analysis(pdf_path)

    analysis = ClientReportAnalysis(
        client_id=client_id,
        analysis_json=json.dumps(analysis_data)
    )
    db.session.add(analysis)
    db.session.commit()

    flash("Report analysis complete!", "success")
    return redirect(url_for('business.view_client', client_id=client.id))


@business_bp.route('/clients/<int:client_id>/messages', methods=['GET', 'POST'])
@login_required
def messages_thread(client_id):
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    thread = MessageThread.query.filter_by(client_id=client.id).first()
    if not thread:
        thread = MessageThread(client_id=client.id)
        db.session.add(thread)
        db.session.commit()

    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if body:
            msg = Message(thread_id=thread.id, from_business=True, body=body)
            db.session.add(msg)
            db.session.commit()
        return redirect(url_for('business.messages_thread', client_id=client.id))

    return render_template('messages_thread.html', thread=thread)


@business_bp.route("/analyses/<int:analysis_id>/update-recommendations", methods=["POST"])
@login_required
def update_recommendations(analysis_id):
    analysis = ClientReportAnalysis.query.get_or_404(analysis_id)
    client = Client.query.get_or_404(analysis.client_id)

    if client.business_user_id != current_user.id:
        abort(403)

    raw_text = request.form.get("recommendations", "")
    updated_recs = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]

    try:
        data = json.loads(analysis.analysis_json)
        data["recommendations"] = updated_recs
        analysis.analysis_json = json.dumps(data)
        db.session.commit()
        flash("Recommendations updated successfully!", "success")
    except Exception as e:
        flash(f"Error updating recommendations: {str(e)}", "error")

    return redirect(url_for("business.view_client", client_id=client.id))


@business_bp.route('/analyses/<int:analysis_id>/delete', methods=['POST'])
@login_required
def delete_analysis(analysis_id):
    """Delete an analysis record."""
    analysis = ClientReportAnalysis.query.get_or_404(analysis_id)
    client = Client.query.get_or_404(analysis.client_id)

    if client.business_user_id != current_user.id:
        abort(403)

    client_id = client.id
    db.session.delete(analysis)
    db.session.commit()
    flash("Analysis deleted.", "success")
    return redirect(url_for("business.view_client", client_id=client_id))


@business_bp.route('/analyses/<int:analysis_id>/send-email', methods=['POST'])
@login_required
def send_analysis_email_route(analysis_id):
    analysis_record = ClientReportAnalysis.query.get_or_404(analysis_id)
    client = Client.query.get_or_404(analysis_record.client_id)

    if client.business_user_id != current_user.id:
        abort(403)

    analysis = json.loads(analysis_record.analysis_json)
    _send_analysis_email(client, analysis)
    flash("Email sent to client!", "success")
    return redirect(url_for('business.view_client', client_id=client.id))


@business_bp.route('/clients/<int:client_id>/mail-analysis', methods=['POST'])
@login_required
def mail_analysis_to_client(client_id):
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    latest_analysis = ClientReportAnalysis.query.filter_by(client_id=client.id).order_by(
        ClientReportAnalysis.created_at.desc()
    ).first()
    if not latest_analysis:
        flash("No analysis found to email.", "error")
        return redirect(url_for('business.view_client', client_id=client.id))

    try:
        analysis_data = json.loads(latest_analysis.analysis_json)
        _send_analysis_email(client, analysis_data)
        flash("Analysis emailed to client!", "success")
    except Exception as e:
        flash(f"Failed to send email: {e}", "error")

    return redirect(url_for('business.view_client', client_id=client.id))


@business_bp.route("/client/<int:client_id>/run-udispute", methods=["POST"])
@login_required
def run_udispute_flow(client_id):
    client = Client.query.get_or_404(client_id)

    account_number = request.form["account_number"]
    entity = request.form["entity"]
    action = request.form["action"]
    issue = request.form["issue"]
    prompt_pack = request.form.get("prompt_pack", "default")

    # Use cached parsed accounts from session if available (avoids re-downloading + re-parsing PDF)
    parsed_accounts = session.get("client_parsed_accounts") if session.get("parsed_accounts_client_id") == client_id else None

    if not parsed_accounts:
        # Fallback: parse from PDF if session cache is empty
        if client.pdf_filename and client.pdf_filename.startswith('http'):
            _temp_pdf = download_to_temp(client.pdf_filename)
            if not _temp_pdf:
                flash("Failed to download PDF from cloud storage.", "error")
                return redirect(url_for("business.view_client", client_id=client.id))
            _pdf_for_parse = _temp_pdf
        else:
            _pdf_for_parse = os.path.join(current_app.config["UPLOAD_FOLDER"], str(client.id), client.pdf_filename)
            if not os.path.exists(_pdf_for_parse):
                _pdf_for_parse = os.path.join(current_app.config["UPLOAD_FOLDER"], client.pdf_filename)
        parsed_accounts = extract_negative_items_from_pdf(_pdf_for_parse)

    selected = next((acc for acc in parsed_accounts if acc["account_number"] == account_number), None)

    if not selected:
        flash("Couldn't find the selected account.", "error")
        return redirect(url_for("business.view_client", client_id=client.id))

    ctx = {
        "entity": entity,
        "account_name": selected["account_name"],
        "account_number": selected["account_number"],
        "marks": selected["status"],
        "action": action,
        "issue": issue,
        "dispute_date": "",
        "days": "",
    }

    custom_id = request.form.get("custom_letter_id")
    if custom_id:
        tpl = CustomLetter.query.get(int(custom_id))
        if not tpl or tpl.user_id != current_user.id:
            flash("Invalid custom template.", "error")
            return redirect(url_for("business.view_client", client_id=client.id))
        prompt = tpl.body
        letter = generate_letter(prompt)
    elif request.form.get('dual_letter') == '1':
        # Dual-Letter Strategy: generate CRA + furnisher letters
        relevant_accounts = [selected] if selected.get('inaccuracies') else []
        cra_prompt, furnisher_prompt, has_inaccuracies, has_legal = build_dual_prompts(
            prompt_pack, ctx, parsed_accounts=relevant_accounts
        )
        cra_letter, furnisher_letter = generate_dual_letters(
            cra_prompt, furnisher_prompt,
            has_inaccuracies=has_inaccuracies, has_legal_research=has_legal
        )
        flash("Dual letters generated!", "success")
        return render_template("udispute_dual_result.html",
                               client=client,
                               cra_letter=cra_letter,
                               furnisher_letter=furnisher_letter,
                               custom_letters=current_user.custom_letters,
                               custom_id=custom_id)
    else:
        # Use build_prompt to inject parser-detected inaccuracies with FCRA citations
        relevant_accounts = [selected] if selected.get('inaccuracies') else []
        prompt, has_inaccuracies, has_legal = build_prompt(prompt_pack, 0, ctx, parsed_accounts=relevant_accounts)
        letter = generate_letter(prompt, has_inaccuracies=has_inaccuracies, has_legal_research=has_legal)

    flash("Letter generated!", "success")
    return render_template("udispute_result.html",
                           client=client,
                           letter=letter,
                           custom_letters=current_user.custom_letters,
                           custom_id=custom_id)


@business_bp.route("/client/<int:client_id>/finalize-udispute", methods=["POST"])
@login_required
def finalize_udispute_letter(client_id):
    client = Client.query.get_or_404(client_id)
    final_text = request.form["edited_letter"].strip()

    if not final_text:
        flash("No letter content to finalize.", "error")
        return redirect(url_for("business.view_client", client_id=client.id))

    upload_folder = current_app.config["UPLOAD_FOLDER"]

    # Build PDF package
    letter_pdf = letter_to_pdf(final_text, os.path.join(upload_folder, 'letter.pdf'))
    pdf_paths = [letter_pdf]

    for attr, field_type in [("id_filename", "id_file"), ("ssn_filename", "ssn_file")]:
        filename = getattr(client, attr)
        if not filename:
            continue
        path = os.path.join(upload_folder, filename)
        ext = filename.rsplit('.', 1)[-1].lower()
        if ext in ("jpg", "jpeg", "png"):
            img_pdf = image_to_pdf(path, field_type=field_type)
            pdf_paths.append(img_pdf)
        elif ext == "pdf":
            pdf_paths.append(path)

    final_pdf = merge_dispute_package(pdf_paths, os.path.join(upload_folder, "DisputePackage.pdf"))
    final_url = url_for('business.client_file', client_id=client.id, filetype='DisputePackage', _external=True)
    session['final_pdf_url'] = final_url

    flash("Letter finalized! Ready to mail.", "success")
    return redirect(url_for('disputes.mail_letter'))


@business_bp.route('/client/<int:client_id>/extract-udispute', methods=['POST'])
@login_required
def extract_for_udispute(client_id):
    client = Client.query.get_or_404(client_id)

    if not client.pdf_filename:
        flash("No PDF found for this client.", "error")
        return redirect(url_for("business.view_client", client_id=client.id))

    if client.pdf_filename.startswith('http'):
        pdf_path = download_to_temp(client.pdf_filename)
        if not pdf_path:
            flash("Failed to download PDF from cloud storage.", "error")
            return redirect(url_for("business.view_client", client_id=client.id))
    else:
        pdf_path = os.path.join(current_app.config["UPLOAD_FOLDER"], str(client.id), client.pdf_filename)
        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(current_app.config["UPLOAD_FOLDER"], client.pdf_filename)

    parsed_accounts = extract_negative_items_from_pdf(pdf_path)
    session["client_parsed_accounts"] = parsed_accounts
    session["parsed_accounts_client_id"] = client.id

    flash(f"Found {len(parsed_accounts)} negative account(s) from the PDF.", "success")
    return redirect(url_for("business.view_client", client_id=client.id))


# ─── Notice of Dispute ───

BUREAU_ADDRESSES = {
    'Equifax': {
        'name': 'Equifax Information Services LLC',
        'address1': 'P.O. Box 740256',
        'city': 'Atlanta', 'state': 'GA', 'zip': '30374',
    },
    'TransUnion': {
        'name': 'TransUnion LLC',
        'address1': 'P.O. Box 2000',
        'city': 'Chester', 'state': 'PA', 'zip': '19016',
    },
    'Experian': {
        'name': 'Experian',
        'address1': 'P.O. Box 4500',
        'city': 'Allen', 'state': 'TX', 'zip': '75013',
    },
}


@business_bp.route('/client/<int:client_id>/notice-of-dispute', methods=['POST'])
@login_required
def generate_notice_of_dispute(client_id):
    """Generate Notice of Dispute letters for a business client — one per bureau."""
    import traceback

    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    # Get parsed accounts from session
    parsed_accounts = session.get("client_parsed_accounts") if session.get("parsed_accounts_client_id") == client_id else None
    if not parsed_accounts:
        flash("No accounts extracted. Click 'Extract Accounts' first.", "error")
        return redirect(url_for("business.view_client", client_id=client.id))

    # Group accounts by bureau
    BUREAU_NAME_MAP = {
        'experian': 'Experian', 'transunion': 'TransUnion', 'equifax': 'Equifax',
    }
    accounts_by_bureau = {}
    for item in parsed_accounts:
        raw = (item.get('bureau') or 'Unknown').lower().strip()
        bureau = BUREAU_NAME_MAP.get(raw, raw.title())
        if bureau not in accounts_by_bureau:
            accounts_by_bureau[bureau] = []
        accounts_by_bureau[bureau].append(item)

    # Build client context
    base_context = {
        'client_full_name': f"{client.first_name} {client.last_name}",
        'client_address': client.address_line1 or '',
        'client_address_line2': client.address_line2 or '',
        'client_city_state_zip': f"{client.city or ''}, {client.state or ''} {client.zip_code or ''}".strip(', '),
        'today_date': datetime.utcnow().strftime('%B %d, %Y'),
    }

    generated_ids = []
    errors = []

    for bureau, accounts in accounts_by_bureau.items():
        client_context = dict(base_context)

        # Bureau mailing address
        bureau_info = BUREAU_ADDRESSES.get(bureau, {})
        if bureau_info:
            client_context['bureau_address'] = (
                f"{bureau_info.get('address1', '')}, "
                f"{bureau_info.get('city', '')} {bureau_info.get('state', '')} {bureau_info.get('zip', '')}"
            )

        try:
            prompt, _, _ = build_notice_of_dispute_prompt(bureau, accounts, client_context)
            letter_text = generate_letter(prompt, is_notice=True)
        except Exception as e:
            traceback.print_exc()
            errors.append(f"{bureau}: {str(e)}")
            continue

        if not letter_text or not letter_text.strip():
            errors.append(f"{bureau}: GPT returned an empty letter.")
            continue

        # Convert to PDF and upload
        pdf_url = None
        try:
            upload_folder = current_app.config['UPLOAD_FOLDER']
            os.makedirs(upload_folder, exist_ok=True)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f'Notice_{bureau}_{timestamp}.pdf'
            pdf_path = letter_to_pdf(letter_text, os.path.join(upload_folder, pdf_filename))

            if cloud_configured():
                from services.cloud_storage import upload_from_path
                cloud_result = upload_from_path(
                    pdf_path,
                    folder=f"clients/{client.id}/notices",
                    filename=pdf_filename.rsplit('.', 1)[0]
                )
                if cloud_result:
                    pdf_url = cloud_result['secure_url']
        except Exception as e:
            traceback.print_exc()
            pdf_url = None

        # Save as ClientDisputeLetter
        letter_record = ClientDisputeLetter(
            client_id=client.id,
            letter_text=letter_text,
            template_name=f'Notice of Dispute — {bureau}',
            pdf_url=pdf_url,
        )
        db.session.add(letter_record)
        db.session.flush()
        generated_ids.append(letter_record.id)

    db.session.commit()

    if errors and not generated_ids:
        flash(f"Failed to generate notices: {'; '.join(errors)}", "error")
    elif errors:
        flash(f"Generated {len(generated_ids)} notice(s), but had errors: {'; '.join(errors)}", "warning")
    else:
        flash(f"Generated {len(generated_ids)} Notice of Dispute letter(s)!", "success")

    return redirect(url_for("business.view_client", client_id=client.id))


@business_bp.route('/toggle-workflow', methods=['POST'])
def toggle_workflow():
    client_id = int(request.form['client_id'])
    key = request.form['workflow_key']
    enabled = bool(int(request.form['enabled']))

    setting = WorkflowSetting.query.filter_by(client_id=client_id, key=key).first()
    if setting:
        setting.enabled = enabled
    else:
        setting = WorkflowSetting(
            client_id=client_id,
            key=key,
            enabled=enabled,
            business_user_id=current_user.id
        )
        db.session.add(setting)
    db.session.commit()

    return redirect(url_for('business.business_dashboard', client_id=client_id))


# ─── Custom Letters ───

@business_bp.route("/custom-letters")
@login_required
def list_custom_letters():
    letters = CustomLetter.query.filter_by(user_id=current_user.id).all()
    return render_template("custom_letters/list.html", letters=letters)


@business_bp.route("/custom-letters/new", methods=["GET", "POST"])
@login_required
def new_custom_letter():
    if request.method == "POST":
        letter = CustomLetter(
            user_id=current_user.id,
            name=request.form["name"],
            subject=request.form.get("subject", ""),
            body=request.form["body"]
        )
        db.session.add(letter)
        db.session.commit()
        flash("Custom letter saved!", "success")
        return redirect(url_for("business.list_custom_letters"))
    return render_template("custom_letters/new.html")


@business_bp.route("/custom-letters/<int:letter_id>/edit", methods=["GET", "POST"])
@login_required
def edit_custom_letter(letter_id):
    letter = CustomLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)
    if request.method == "POST":
        letter.name = request.form["name"]
        letter.subject = request.form.get("subject", "")
        letter.body = request.form["body"]
        db.session.commit()
        flash("Custom letter updated.", "success")
        return redirect(url_for("business.list_custom_letters"))
    return render_template("custom_letters/edit.html", letter=letter)


@business_bp.route("/custom-letters/<int:letter_id>/delete", methods=["POST"])
@login_required
def delete_custom_letter(letter_id):
    letter = CustomLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)
    db.session.delete(letter)
    db.session.commit()
    flash("Custom letter deleted.", "info")
    return redirect(url_for("business.list_custom_letters"))


@business_bp.route("/custom-letters/upload", methods=["POST"])
@login_required
def upload_custom_letter():
    """Upload a PDF, DOCX, or TXT file and extract text into a new custom letter."""
    file = request.files.get('letter_file')
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("business.list_custom_letters"))

    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext not in ('pdf', 'docx', 'txt'):
        flash("Unsupported file type. Please upload a PDF, DOCX, or TXT file.", "error")
        return redirect(url_for("business.list_custom_letters"))

    try:
        if ext == 'txt':
            body = file.read().decode('utf-8', errors='replace')

        elif ext == 'pdf':
            import pdfplumber
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            file.save(tmp.name)
            tmp.close()
            pages_text = []
            with pdfplumber.open(tmp.name) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages_text.append(text)
            os.unlink(tmp.name)
            body = '\n\n'.join(pages_text)

        elif ext == 'docx':
            import docx
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.docx')
            file.save(tmp.name)
            tmp.close()
            doc = docx.Document(tmp.name)
            body = '\n\n'.join(p.text for p in doc.paragraphs if p.text.strip())
            os.unlink(tmp.name)

        if not body or not body.strip():
            flash("Could not extract any text from the file.", "error")
            return redirect(url_for("business.list_custom_letters"))

        # Create the custom letter with extracted text
        name = filename.rsplit('.', 1)[0] if '.' in filename else filename
        letter = CustomLetter(
            user_id=current_user.id,
            name=name,
            subject='',
            body=body.strip(),
        )
        db.session.add(letter)
        db.session.commit()

        flash("Letter uploaded! Review and edit the extracted text below.", "success")
        return redirect(url_for("business.edit_custom_letter", letter_id=letter.id))

    except Exception as e:
        flash(f"Error extracting text: {e}", "error")
        return redirect(url_for("business.list_custom_letters"))


# ─── CFPB Search (Business Only) ───

@business_bp.route('/cfpb-search')
@login_required
def cfpb_search_page():
    """CFPB complaint search page."""
    return render_template('cfpb_search.html')


@business_bp.route('/cfpb-search', methods=['POST'])
@login_required
def cfpb_search_submit():
    """Execute CFPB search and render results."""
    company = request.form.get('company', '').strip()
    narratives_only = request.form.get('narratives_only') == 'on'
    if not company:
        flash("Please enter a company name.", "error")
        return render_template('cfpb_search.html')

    from services.cfpb_search import search_complaints
    results = search_complaints(company, limit=25, has_narrative=narratives_only or None)
    return render_template('cfpb_search.html',
                           company=company,
                           results=results,
                           narratives_only=narratives_only)


@business_bp.route('/api/cfpb-search')
@login_required
def cfpb_search_api():
    """AJAX CFPB search endpoint."""
    company = request.args.get('company', '').strip()
    if not company:
        return jsonify({'error': 'company parameter required'}), 400

    from services.cfpb_search import search_complaints
    has_narrative = request.args.get('narratives_only', 'false').lower() == 'true'
    response_filter = request.args.get('response_filter', '') or None
    results = search_complaints(company, limit=25,
                                has_narrative=has_narrative or None,
                                response_filter=response_filter)
    return jsonify(results)


# ─── Client Portal Management ───

@business_bp.route('/clients/<int:client_id>/portal/generate', methods=['POST'])
@login_required
def generate_portal_link(client_id):
    """Generate a unique portal link for a client."""
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    from models import ClientPortalToken
    existing = ClientPortalToken.query.filter_by(client_id=client_id).first()
    if existing:
        existing.token = ClientPortalToken.generate_token()
        existing.is_active = True
        existing.created_at = datetime.utcnow()
    else:
        existing = ClientPortalToken(
            client_id=client_id,
            token=ClientPortalToken.generate_token(),
        )
        db.session.add(existing)
    db.session.commit()

    portal_url = f"{request.host_url.rstrip('/')}/portal/{existing.token}"
    return jsonify({'ok': True, 'portal_url': portal_url, 'token': existing.token})


@business_bp.route('/clients/<int:client_id>/portal/revoke', methods=['POST'])
@login_required
def revoke_portal_link(client_id):
    """Deactivate a client's portal link."""
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    from models import ClientPortalToken
    token = ClientPortalToken.query.filter_by(client_id=client_id).first()
    if token:
        token.is_active = False
        db.session.commit()

    return jsonify({'ok': True})


@business_bp.route('/clients/<int:client_id>/portal/link')
@login_required
def get_portal_link(client_id):
    """Return the portal URL for a client."""
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    from models import ClientPortalToken
    token = ClientPortalToken.query.filter_by(client_id=client_id, is_active=True).first()
    if not token:
        return jsonify({'has_link': False})

    portal_url = f"{request.host_url.rstrip('/')}/portal/{token.token}"
    return jsonify({'has_link': True, 'portal_url': portal_url, 'token': token.token})


# ─── Helper ───

def _send_analysis_email(client, analysis):
    """Send analysis results email to client."""
    from flask import render_template as rt
    msg = MailMessage(
        subject=f"uDispute Analysis Results - For {client.first_name} {client.last_name}",
        sender=current_app.config['MAIL_USERNAME'],
        recipients=[client.email]
    )
    msg.html = rt("email/analysis_summary.html", client=client, analysis=analysis)
    mail.send(msg)
