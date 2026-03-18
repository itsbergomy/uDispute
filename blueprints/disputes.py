"""
Core dispute workflow blueprint — the consumer-facing dispute flow.
Extracted from dispute_ui.py.
"""

import os
import json
from datetime import datetime, timedelta
from flask import (
    Blueprint, request, jsonify, render_template, flash,
    abort, redirect, url_for, session, send_file, send_from_directory, current_app
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from models import db, User, UserSetting, DisputeRound, DailyLogEntry, MailedLetter, Correspondence
from services.pdf_parser import (
    extract_negative_items_from_pdf, compute_pdf_hash,
    extract_pdf_metrics, pdf_to_base64_images
)
from services.letter_generator import (
    PACKS, PACK_INFO, generate_letter, letter_to_pdf,
    image_to_pdf, merge_dispute_package
)
from services.delivery import mail_letter_via_docupost, get_docupost_token
from services.report_analyzer import run_report_analysis

disputes_bp = Blueprint('disputes', __name__)

# ── Bureau dispute mailing addresses (verified March 2026) ──
BUREAU_ADDRESSES = {
    'Equifax': {
        'name': 'Equifax Information Services LLC',
        'company': 'Equifax',
        'address1': 'P.O. Box 740256',
        'address2': '',
        'city': 'Atlanta',
        'state': 'GA',
        'zip': '30374',
    },
    'TransUnion': {
        'name': 'TransUnion LLC',
        'company': 'TransUnion Consumer Solutions',
        'address1': 'P.O. Box 2000',
        'address2': '',
        'city': 'Chester',
        'state': 'PA',
        'zip': '19016',
    },
    'Experian': {
        'name': 'Experian',
        'company': 'Experian Disputes',
        'address1': 'P.O. Box 4500',
        'address2': '',
        'city': 'Allen',
        'state': 'TX',
        'zip': '75013',
    },
}


def free_user_limit_for_dispute(user):
    if user.plan != 'free':
        return False
    if not user.last_round_time:
        return False
    now = datetime.utcnow()
    if now - user.last_round_time < timedelta(hours=48):
        return True
    return False


def require_pro_or_business(f):
    """Decorator: block free users from Pro+ features."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.plan == 'free':
            flash("Upgrade to Pro to access this feature.", "error")
            return redirect(url_for('disputes.index'))
        return f(*args, **kwargs)
    return decorated


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'pdf'}


@disputes_bp.route('/')
def index():
    if current_user.is_authenticated and current_user.plan == 'business':
        return redirect(url_for('business.business_dashboard'))
    return render_template('index.html')


@disputes_bp.route('/landing')
def landing_preview():
    """Temp preview route for the landing page — remove before production."""
    return render_template('landing.html')


@disputes_bp.route('/upload-pdf', methods=['GET', 'POST'])
@login_required
def upload_pdf():
    if request.method == 'POST':
        if current_user.is_authenticated:
            if current_user.plan == 'free':
                if free_user_limit_for_dispute(current_user):
                    flash("Free plan: You must wait 48 hours between dispute rounds.", "error")
                    return redirect(url_for('disputes.index'))

        if 'pdfFile' not in request.files:
            return jsonify({"error": 'No file selected'}), 400

        file = request.files['pdfFile']
        if file.filename == '':
            return jsonify({"error": 'No file selected'}), 400

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            pdf_hash = compute_pdf_hash(filepath)
            session['pdf_hash'] = pdf_hash

            try:
                negative_items = extract_negative_items_from_pdf(filepath)
            except Exception as e:
                flash(f"Could not parse PDF: {e}", "error")
                return redirect(url_for('disputes.upload_pdf'))
            session['negative_items'] = negative_items

            existing_round = DisputeRound.query.filter_by(
                user_id=current_user.id,
                pdf_hash=pdf_hash
            ).first()

            if not existing_round:
                new_round = DisputeRound(
                    user_id=current_user.id,
                    pdf_hash=pdf_hash,
                    round_number=1
                )
                db.session.add(new_round)
                db.session.commit()
                session['current_round'] = 1
                session['disputed_accounts'] = []
                flash("New PDF detected — Starting Round 1. Next: Select the accounts you want to dispute and choose your strategy.", "success")
                return redirect('/select-account')
            else:
                session['current_round'] = existing_round.round_number
                session['disputed_accounts'] = existing_round.get_disputed_accounts()

                if all(item['account_number'] in session['disputed_accounts'] for item in negative_items):
                    return redirect(url_for('disputes.confirm_next_round'))

                flash(f"Resuming Round {existing_round.round_number}.", "info")
                return redirect('/select-account')
        else:
            return jsonify({"error": "Invalid file type. Only PDFs allowed."}), 400

    return render_template('upload_pdf.html')


@disputes_bp.route('/confirm-next-round', methods=['GET', 'POST'])
def confirm_next_round():
    pdf_hash = session.get('pdf_hash')
    if not pdf_hash:
        flash("Missing PDF context.", "error")
        return redirect(url_for('disputes.upload_pdf'))

    if request.method == 'POST':
        session['pending_round_upgrade'] = False
        session['current_round'] = session.get('current_round', 1) + 1
        session['disputed_accounts'] = []
        return redirect(url_for('disputes.select_account'))

    current_round = session.get('current_round', 1)
    return render_template('confirm_next_round.html', current_round=current_round)


@disputes_bp.route('/select-account', methods=['GET'])
def select_account():
    items = session.get('negative_items', [])
    return render_template('select_negative.html', negative_items=items)


@disputes_bp.route('/confirm-account', methods=['GET'])
def confirm_account():
    account_name = request.args.get('account_name')
    account_number = request.args.get('account_number')
    status = request.args.get('status')

    return render_template('confirm_account.html',
        account_name=account_name,
        account_number=account_number,
        status=status
    )


@disputes_bp.route('/confirm-account/save', methods=['POST'])
def save_confirmed_account():
    account_number = request.form.get('account_number')
    session['account_name'] = request.form.get('account_name', '')
    session['account_number'] = account_number or ''
    session['status'] = request.form.get('status', '')

    pdf_hash = session.get('pdf_hash')
    if not pdf_hash:
        flash("Missing PDF context.", "error")
        return redirect(url_for('disputes.upload_pdf'))

    round_record = DisputeRound.query.filter_by(
        user_id=current_user.id,
        pdf_hash=pdf_hash
    ).first()

    if not round_record:
        flash("Could not find your dispute round record.", "error")
        return redirect(url_for('disputes.upload_pdf'))

    disputed_accounts = round_record.get_disputed_accounts()
    if account_number not in disputed_accounts:
        disputed_accounts.append(account_number)
        round_record.set_disputed_accounts(disputed_accounts)
        db.session.commit()

    flash("Account confirmed! Next: Choose a prompt pack and hit Generate to create your dispute letter.", "success")
    return redirect(url_for('disputes.select_entity'))


@disputes_bp.route('/select-entity', methods=['GET', 'POST'])
def select_entity():
    if request.method == 'POST':
        session['account_name'] = request.form.get('account_name')
        session['account_number'] = request.form.get('account_number')
        session['status'] = request.form.get('status')
    return render_template('select_entity.html')


@disputes_bp.route('/handle-entity', methods=['POST'])
def handle_entity():
    selected = request.form.get('entity')
    if not selected:
        flash("Please select an entity.", "error")
        return redirect(url_for('disputes.select_entity'))
    session['selected_entity'] = selected
    return redirect(url_for('disputes.define_details'))


@disputes_bp.route('/define-details', methods=['GET', 'POST'])
@login_required
def define_details():
    pack_key = session.get('prompt_pack', 'default')

    core_fields = [
        ('action', 'What would you like them to do?'),
        ('issue', 'Brief summary of the dispute issue'),
    ]
    acdv_fields = [
        ('dispute_date', 'Original Dispute Date (YYYY-MM-DD)'),
        ('days', 'Deadline in business days')
    ] if pack_key == 'ACDV_response' else []

    all_fields = core_fields + acdv_fields

    if request.method == 'POST':
        for name, _ in all_fields:
            session[name] = request.form.get(name, '').strip()
        return redirect(url_for('disputes.choose_template'))

    return render_template(
        'define_details.html',
        pack_key=pack_key,
        core_fields=core_fields,
        acdv_fields=acdv_fields,
        entity=session.get('selected_entity', '')
    )


@disputes_bp.route('/choose-template', methods=['GET', 'POST'])
@login_required
def choose_template():
    pack_key = session.get('prompt_pack', 'default')
    raw_templates = PACKS.get(pack_key, PACKS['default'])

    ctx = {
        'entity': session.get('selected_entity', ''),
        'account_name': session.get('account_name', ''),
        'account_number': session.get('account_number', ''),
        'marks': session.get('status', ''),
        'action': session.get('action', ''),
        'issue': session.get('issue', ''),
        'dispute_date': session.get('dispute_date', ''),
        'days': session.get('days', ''),
    }

    filled = [tpl.format(**ctx) for tpl in raw_templates]

    if request.method == 'POST':
        session['selected_template'] = request.form['template_text']
        return redirect(url_for('disputes.generate_letter_screen'))

    return render_template('choose_template.html', templates=filled, pack_key=pack_key)


@disputes_bp.route('/prompt-packs', methods=['GET', 'POST'])
@login_required
@require_pro_or_business
def prompt_packs():
    if request.method == 'POST':
        session['prompt_pack'] = request.form['pack_key']
        return redirect(url_for('disputes.index'))
    return render_template('prompt_packs.html', packs=PACK_INFO)


@disputes_bp.route('/set-pack/<pack>')
@login_required
def set_prompt_pack(pack):
    """Quick-set prompt pack from nav toggle."""
    valid = {'default', 'arbitration', 'consumer_law', 'ACDV_response'}
    if pack in valid:
        session['prompt_pack'] = pack
        flash(f'Switched to {pack.replace("_"," ")} pack. Your next dispute letter will use this strategy.', 'success')
    return redirect(request.referrer or url_for('disputes.index'))


@disputes_bp.route('/generate-letter-screen', methods=['POST'])
def generate_letter_screen():
    template = request.form.get('template_text')
    session['selected_template'] = template
    return render_template('generate_letter.html')


@disputes_bp.route('/generate-process')
def generate_process():
    template = session['selected_template']
    data = {
        "action": session.get('action', ''),
        "issue": session.get('issue', ''),
        "entity": session.get('selected_entity', ''),
        "account_name": session.get('account_name', ''),
        "account_number": session.get('account_number', ''),
        "marks": session.get('status', '')
    }
    prompt = template.format(**data)
    letter_text = generate_letter(prompt)
    session['generated_letter'] = letter_text
    return redirect(url_for('disputes.final_review'))


@disputes_bp.route('/final-review')
def final_review():
    letter = session.get('generated_letter')
    return render_template('final_review.html', letter=letter)


@disputes_bp.route('/manual-mode', methods=['GET', 'POST'])
def manual_mode():
    if request.method == 'POST':
        if current_user.is_authenticated and current_user.plan == 'free':
            now = datetime.utcnow()
            if current_user.last_round_time is None or (now - current_user.last_round_time > timedelta(hours=48)):
                current_user.manual_accounts_used = 0
                current_user.last_round_time = now
                db.session.commit()

            if current_user.manual_accounts_used >= 3:
                flash("Free plan: You can only dispute 3 accounts in manual mode every 48 hours.", "error")
                return redirect(url_for('disputes.index'))

        session['account_name'] = request.form.get('account_name', '').strip()
        session['account_number'] = request.form.get('account_number', '').strip()
        session['status'] = request.form.get('account_status', '').strip()
        session['selected_entity'] = request.form.get('entity', '').strip()
        session['action'] = request.form.get('action', '').strip()
        session['issue'] = request.form.get('issue', '').strip()
        session['manual_mode'] = True

        if current_user.is_authenticated and current_user.plan == 'free':
            current_user.manual_accounts_used += 1
            current_user.last_round_time = datetime.utcnow()
            db.session.commit()

        return redirect(url_for('disputes.choose_template'))

    return render_template(
        'manual_mode.html',
        account_name=session.get('account_name', ''),
        account_number=session.get('account_number', ''),
        status=session.get('status', ''),
        selected_entity=session.get('selected_entity', ''),
        action=session.get('action', ''),
        issue=session.get('issue', '')
    )


@disputes_bp.route('/mail-letter', methods=['GET', 'POST'])
@login_required
@require_pro_or_business
def mail_letter():
    if request.method == 'GET':
        entity = session.get('selected_entity', '')
        bureau = BUREAU_ADDRESSES.get(entity, {})
        return render_template('mail_letter.html',
            from_name=session.get('user_name', ''),
            from_address1=session.get('user_address_line1', ''),
            from_city=session.get('user_city', ''),
            from_state=session.get('user_state', ''),
            from_zip=session.get('user_zip', ''),
            selected_entity=entity,
            bureau_addresses=BUREAU_ADDRESSES,
            to_name=bureau.get('name', ''),
            to_company=bureau.get('company', ''),
            to_address1=bureau.get('address1', ''),
            to_address2=bureau.get('address2', ''),
            to_city=bureau.get('city', ''),
            to_state=bureau.get('state', ''),
            to_zip=bureau.get('zip', ''),
        )

    # ── Resolve the PDF to send ──
    # Priority: uploaded file > session URL from /convert-pdf
    pdf_url = None
    uploaded = request.files.get('pdf_file')
    if uploaded and uploaded.filename:
        # Save uploaded PDF and generate a serveable URL
        upload_folder = current_app.config['UPLOAD_FOLDER']
        user_folder = os.path.join(upload_folder, str(current_user.id))
        os.makedirs(user_folder, exist_ok=True)

        from datetime import datetime as dt
        timestamp = dt.utcnow().strftime('%Y%m%d_%H%M%S')
        safe_name = f'MailUpload_{timestamp}.pdf'
        save_path = os.path.join(user_folder, safe_name)
        uploaded.save(save_path)
        pdf_url = generate_public_pdf_url(safe_name)
    else:
        # Session has the local filename — generate a public URL from it
        session_pdf = session.get('final_pdf_url', '')
        if session_pdf:
            # Extract just the filename from the session URL path
            pdf_filename = session_pdf.rsplit('/', 1)[-1] if '/' in session_pdf else session_pdf
            pdf_url = generate_public_pdf_url(pdf_filename)
        else:
            pdf_url = None

    if not pdf_url:
        flash("No PDF found. Please upload a PDF or generate a Dispute Package first.", "error")
        return redirect(url_for('disputes.mail_letter'))

    # ── Collect addresses ──
    recipient = {
        'name': request.form.get('to_name', ''),
        'company': request.form.get('to_company', ''),
        'address1': request.form.get('to_address1', ''),
        'address2': request.form.get('to_address2', ''),
        'city': request.form.get('to_city', ''),
        'state': request.form.get('to_state', ''),
        'zip': request.form.get('to_zip', ''),
    }
    sender = {
        'name': request.form.get('from_name', session.get('user_name', '')),
        'company': request.form.get('from_company', ''),
        'address1': request.form.get('from_address1', session.get('user_address_line1', '')),
        'address2': request.form.get('from_address2', ''),
        'city': request.form.get('from_city', session.get('user_city', '')),
        'state': request.form.get('from_state', session.get('user_state', '')),
        'zip': request.form.get('from_zip', session.get('user_zip', '')),
    }

    # ── Collect mail options ──
    mail_options = {
        'mail_class': request.form.get('class', 'usps_first_class'),
        'servicelevel': request.form.get('servicelevel', ''),
        'color': 'true' if request.form.get('color') else 'false',
        'doublesided': 'true' if request.form.get('doublesided') else 'false',
        'return_envelope': 'true' if request.form.get('return_envelope') else 'false',
    }

    byok_token = get_docupost_token(current_user.id)
    result = mail_letter_via_docupost(
        pdf_url=pdf_url,
        recipient=recipient,
        sender=sender,
        mail_options=mail_options,
        api_token=byok_token,
    )

    if result.get('success'):
        flash("Your letter has been sent! It will arrive in 3–10 business days. Track it in your Dispute Folder.", "success")
        return redirect(url_for('disputes.final_review'))
    else:
        flash(f"DocuPost error: {result.get('error')}", "error")
        return redirect(url_for('disputes.mail_letter'))


@disputes_bp.route('/convert-pdf', methods=['POST'])
def convert_pdf():
    letter_text = request.form.get('letter', '').strip()
    if not letter_text:
        return "Letter content is missing.", 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)

    # Generate letter PDF
    letter_pdf_path = letter_to_pdf(letter_text, os.path.join(upload_folder, 'letter.pdf'))

    # Convert uploaded supporting docs to PDF
    pdf_paths = [letter_pdf_path]
    for field in ('id_file', 'ssn_file', 'utility_file'):
        file = request.files.get(field)
        if not file or not file.filename:
            continue

        filename = secure_filename(file.filename)
        raw_path = os.path.join(upload_folder, filename)
        file.save(raw_path)
        ext = filename.rsplit('.', 1)[-1].lower()

        if ext in ('png', 'jpg', 'jpeg'):
            img_pdf = image_to_pdf(raw_path, field_type=field)
            pdf_paths.append(img_pdf)
        elif ext == 'pdf':
            pdf_paths.append(raw_path)

    # Merge into DisputePackage
    final_pdf = merge_dispute_package(pdf_paths, os.path.join(upload_folder, 'DisputePackage.pdf'))

    # Auto-save letter backup to Mailed Letters
    if current_user.is_authenticated:
        import shutil
        from datetime import datetime as dt
        timestamp = dt.utcnow().strftime('%Y%m%d_%H%M%S')
        backup_name = f'DisputePackage_{timestamp}.pdf'
        user_folder = os.path.join(upload_folder, str(current_user.id))
        os.makedirs(user_folder, exist_ok=True)
        backup_path = os.path.join(user_folder, backup_name)
        shutil.copy2(final_pdf, backup_path)

        pdf_serve_url = url_for('disputes.serve_upload', filename=backup_name, _external=True)

        # Extract bureau and round from form data or letter content
        bureau = request.form.get('bureau', '').strip() or None
        round_number = request.form.get('round_number', 1)
        account_name = request.form.get('account_name', '').strip() or None
        try:
            round_number = int(round_number)
        except (ValueError, TypeError):
            round_number = 1

        mailed = MailedLetter(
            user_id=current_user.id,
            letter_text=letter_text,
            pdf_url=pdf_serve_url,
            bureau=bureau,
            round_number=round_number,
            account_name=account_name
        )
        db.session.add(mailed)
        db.session.commit()

        # Store the PDF URL in session so /mail-letter can use it
        session['final_pdf_url'] = pdf_serve_url

    flash("Dispute Package ready! Head to Mail to send it to the bureau.", "success")
    return send_file(
        final_pdf,
        as_attachment=True,
        download_name='DisputePackage.pdf',
        mimetype='application/pdf'
    )


# ─── Dispute Folder Routes ───

@disputes_bp.route('/dispute-folder')
@login_required
@require_pro_or_business
def dispute_folder():
    logs = DailyLogEntry.query.filter_by(user_id=current_user.id).order_by(DailyLogEntry.timestamp.desc()).all()
    letters = MailedLetter.query.filter_by(user_id=current_user.id).order_by(MailedLetter.created_at.desc()).all()
    docs = Correspondence.query.filter_by(user_id=current_user.id).order_by(Correspondence.uploaded_at.desc()).all()
    return render_template('dispute_folder.html', logs=logs, letters=letters, docs=docs)


@disputes_bp.route('/api/dispute-folder-data')
@login_required
@require_pro_or_business
def dispute_folder_data():
    """Return dispute folder contents as an HTML fragment for the AJAX drawer."""
    logs = DailyLogEntry.query.filter_by(user_id=current_user.id).order_by(DailyLogEntry.timestamp.desc()).all()
    letters = MailedLetter.query.filter_by(user_id=current_user.id).order_by(MailedLetter.created_at.desc()).all()
    docs = Correspondence.query.filter_by(user_id=current_user.id).order_by(Correspondence.uploaded_at.desc()).all()
    return render_template('_dispute_folder_fragment.html', logs=logs, letters=letters, docs=docs)


@disputes_bp.route('/add-log', methods=['GET', 'POST'])
@login_required
def add_log():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        if not title or not content:
            flash('Please fill out both title and content', 'error')
            return redirect(url_for('disputes.add_log'))

        entry = DailyLogEntry(user_id=current_user.id, description=f"{title}: {content}")
        db.session.add(entry)
        db.session.commit()

        flash('Logged your entry!', 'success')
        return redirect(request.referrer or url_for('disputes.dispute_folder'))

    return render_template('add_log.html')


@disputes_bp.route('/add-letter', methods=['GET', 'POST'])
@login_required
def add_letter():
    if request.method == 'POST':
        letter_text = request.form['letter_text'].strip()
        if not letter_text:
            flash("Letter text is required.", "error")
            return redirect(url_for('disputes.add_letter'))

        bureau = request.form.get('bureau', '').strip() or None
        account_name = request.form.get('account_name', '').strip() or None
        round_number = request.form.get('round_number', 1)
        try:
            round_number = int(round_number)
        except (ValueError, TypeError):
            round_number = 1

        new = MailedLetter(
            user_id=current_user.id,
            letter_text=letter_text,
            bureau=bureau,
            round_number=round_number,
            account_name=account_name
        )
        db.session.add(new)
        db.session.commit()
        flash("Mailed letter recorded.", "success")
        return redirect(request.referrer or url_for('disputes.dispute_folder'))

    return render_template('add_letter.html')


@disputes_bp.route('/upload-doc', methods=['GET', 'POST'])
@login_required
def upload_doc():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or file.filename == '':
            flash("Please choose a file to upload.", "error")
            return redirect(url_for('disputes.upload_doc'))

        filename = secure_filename(file.filename)
        user_folder = os.path.join(
            current_app.config.get('UPLOAD_FOLDER', 'uploads'),
            str(current_user.id)
        )
        os.makedirs(user_folder, exist_ok=True)
        filepath = os.path.join(user_folder, filename)
        file.save(filepath)

        serve_url = url_for('disputes.serve_upload', filename=filename)

        doc = Correspondence(
            user_id=current_user.id,
            client_id=0,
            filename=filename,
            file_url=serve_url,
            description=request.form.get('description', '').strip()
        )
        db.session.add(doc)
        db.session.commit()

        flash("Document uploaded.", "success")
        # Stay on current page if uploaded from the drawer, otherwise go to folder
        return redirect(request.referrer or url_for('disputes.dispute_folder'))

    return render_template('upload_doc.html')


@disputes_bp.route('/uploads/<filename>')
@login_required
def serve_upload(filename):
    """Serve uploaded documents — checks per-user folder first, then root uploads."""
    upload_base = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    user_folder = os.path.join(upload_base, str(current_user.id))

    # Check per-user folder first (new uploads)
    if os.path.exists(os.path.join(user_folder, filename)):
        return send_from_directory(os.path.abspath(user_folder), filename)

    # Fall back to root uploads folder (old uploads)
    if os.path.exists(os.path.join(upload_base, filename)):
        return send_from_directory(os.path.abspath(upload_base), filename)

    abort(404)


@disputes_bp.route('/public-pdf/<token>/<filename>')
def serve_public_pdf(token, filename):
    """Serve a PDF publicly using a short-lived token — used by DocuPost to fetch PDFs."""
    import hmac, hashlib
    secret = current_app.config.get('SECRET_KEY', '')
    expected = hmac.new(secret.encode(), filename.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(token, expected):
        abort(403)

    upload_base = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    # Search all user folders for the file
    for entry in os.listdir(upload_base):
        full = os.path.join(upload_base, entry)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, filename)):
            return send_from_directory(os.path.abspath(full), filename)

    if os.path.exists(os.path.join(upload_base, filename)):
        return send_from_directory(os.path.abspath(upload_base), filename)

    abort(404)


def generate_public_pdf_url(filename):
    """Generate a public URL with HMAC token for DocuPost to fetch a PDF."""
    import hmac, hashlib
    from flask import current_app, url_for
    secret = current_app.config.get('SECRET_KEY', '')
    token = hmac.new(secret.encode(), filename.encode(), hashlib.sha256).hexdigest()[:32]
    return url_for('disputes.serve_public_pdf', token=token, filename=filename, _external=True)


@disputes_bp.route('/delete-doc/<int:doc_id>', methods=['POST'])
@login_required
def delete_doc(doc_id):
    """Delete an uploaded document."""
    doc = Correspondence.query.get_or_404(doc_id)
    if doc.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # Delete file from disk
    user_folder = os.path.join(
        current_app.config.get('UPLOAD_FOLDER', 'uploads'),
        str(current_user.id)
    )
    filepath = os.path.join(user_folder, doc.filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    db.session.delete(doc)
    db.session.commit()
    return jsonify({"status": "ok"})


# ─── Report Analyzer ───

@disputes_bp.route('/report-analyzer', methods=['GET', 'POST'])
@login_required
def report_analyzer():
    if request.method == 'POST':
        upload = request.files.get('credit_report')
        if not upload or upload.filename == "":
            session['intake'] = {
                'first_name': request.form['first_name'],
                'last_name': request.form['last_name'],
                'phone': request.form['phone'],
                'email': request.form['email']
            }
            return render_template('upload_pdf_analyzer.html', **session['intake'])

        filename = secure_filename(upload.filename)
        path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)

        try:
            upload.save(path)
            if os.path.getsize(path) == 0:
                raise ValueError("Uploaded file is empty.")
        except Exception as e:
            flash(f"File upload error: {e}", "error")
            return render_template('upload_pdf_analyzer.html', **session.get('intake', {}))

        try:
            analysis = run_report_analysis(path)
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            flash("AI error: failed to analyze report. Try another report.", "error")
            return render_template('upload_pdf_analyzer.html', **session.get('intake', {}))

        if os.path.exists(path):
            os.remove(path)

        intake = session.get('intake', {})
        return render_template(
            'analysis_results.html',
            user_name=f"{intake.get('first_name', '')} {intake.get('last_name', '')}".strip(),
            **analysis,
            **intake
        )

    session.pop('intake', None)
    return render_template('report_analyzer.html')


@disputes_bp.route('/funding-sequencer')
@login_required
def funding_sequencer():
    return render_template('funding_sequencer.html')


# ─── Settings (BYOK) ───

@disputes_bp.route('/settings')
@login_required
def settings_page():
    """Settings page — BYOK API keys."""
    setting = UserSetting.query.filter_by(user_id=current_user.id, key='docupost_api_token').first()
    has_key = bool(setting and setting.value)
    masked = ''
    if has_key:
        try:
            from services.encryption import decrypt_value
            raw = decrypt_value(setting.value)
            masked = '•' * (len(raw) - 4) + raw[-4:] if len(raw) > 4 else '•' * len(raw)
        except Exception:
            masked = '••••••••'
    return render_template('settings.html', has_docupost_key=has_key, masked_key=masked)


@disputes_bp.route('/settings/docupost-key', methods=['POST'])
@login_required
def save_docupost_key():
    """Save or update the user's DocuPost API key (encrypted)."""
    data = request.get_json(silent=True) or {}
    key_value = data.get('api_key', '').strip()
    if not key_value:
        return jsonify({'error': 'API key is required'}), 400

    from services.encryption import encrypt_value
    encrypted = encrypt_value(key_value)

    setting = UserSetting.query.filter_by(user_id=current_user.id, key='docupost_api_token').first()
    if setting:
        setting.value = encrypted
        setting.updated_at = datetime.utcnow()
    else:
        setting = UserSetting(user_id=current_user.id, key='docupost_api_token', value=encrypted)
        db.session.add(setting)
    db.session.commit()

    masked = '•' * (len(key_value) - 4) + key_value[-4:] if len(key_value) > 4 else '•' * len(key_value)
    return jsonify({'ok': True, 'masked_key': masked})


@disputes_bp.route('/settings/docupost-key/delete', methods=['POST'])
@login_required
def delete_docupost_key():
    """Remove the user's stored DocuPost API key."""
    UserSetting.query.filter_by(user_id=current_user.id, key='docupost_api_token').delete()
    db.session.commit()
    return jsonify({'ok': True})


@disputes_bp.route('/settings/docupost-key/test', methods=['POST'])
@login_required
def test_docupost_key():
    """Test the user's DocuPost API key by making a lightweight API call."""
    token = get_docupost_token(current_user.id)
    if not token:
        return jsonify({'ok': False, 'error': 'No DocuPost key configured'}), 400

    import requests as req
    try:
        resp = req.get('https://app.docupost.com/api/1.1/wf/account_info',
                       params={'api_token': token}, timeout=10)
        if resp.status_code == 200 and b'<Error>' not in resp.content:
            return jsonify({'ok': True, 'message': 'Key is valid'})
        else:
            return jsonify({'ok': False, 'error': 'Key rejected by DocuPost'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
