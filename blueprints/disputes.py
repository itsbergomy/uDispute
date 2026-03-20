"""
Core dispute workflow blueprint — the consumer-facing dispute flow.
Extracted from dispute_ui.py.
"""

import os
import json
from datetime import datetime, timedelta
from flask import (
    Blueprint, request, jsonify, render_template, flash,
    abort, redirect, url_for, session, send_file, send_from_directory, current_app, make_response
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from models import db, User, UserSetting, DisputeRound, DailyLogEntry, MailedLetter, Correspondence
from services.pdf_parser import (
    extract_negative_items_from_pdf, compute_pdf_hash,
    extract_pdf_metrics, pdf_to_base64_images
)
from services.letter_generator import (
    PACKS, PACK_INFO, generate_letter, build_prompt,
    build_notice_of_dispute_prompt, letter_to_pdf,
    image_to_pdf, merge_dispute_package
)
from services.delivery import mail_letter_via_docupost, get_docupost_token
from services.report_analyzer import run_report_analysis
from services.cloud_storage import upload_file, upload_from_path, get_file_url, download_to_temp, delete_file, is_configured as cloud_configured

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

            if cloud_configured():
                # Upload to Cloudinary, download temp copy for parsing
                result = upload_file(file, folder=f"users/{current_user.id}/reports", resource_type="raw")
                if not result:
                    flash("File upload failed. Please try again.", "error")
                    return redirect(url_for('disputes.upload_pdf'))
                session['cloud_pdf_url'] = result['secure_url']
                filepath = download_to_temp(result['secure_url'], suffix='.pdf')
                if not filepath:
                    flash("Could not process uploaded file.", "error")
                    return redirect(url_for('disputes.upload_pdf'))
            else:
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


# ─── Issue / Solution card definitions for Bureau Assault ───

ISSUE_CARDS = [
    {"key": "status_contradicts_history", "name": "Status Contradicts Payment History",
     "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
     "description": "The account status doesn't match the payment history grid — e.g., 'Pays as agreed' but shows late payments."},
    {"key": "account_type_mismatch", "name": "Account Type Mismatch",
     "section": "15 U.S.C. § 1681e(b)",
     "description": "The account is classified with the wrong type (e.g., listed as 'Open' when it should be 'Collection')."},
    {"key": "original_creditor_not_reflected", "name": "Original Creditor Not Reflected",
     "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
     "description": "A transferred/sold debt doesn't properly identify the original creditor."},
    {"key": "closed_account_with_balance", "name": "Closed Account With Balance",
     "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
     "description": "A closed or paid account still shows an outstanding balance."},
    {"key": "chargeoff_not_in_status", "name": "Charge-Off Not In Status",
     "section": "15 U.S.C. § 1681s-2(a)(1)(A)",
     "description": "Payment history shows charge-off entries but the account status doesn't reflect it."},
    {"key": "balance_exceeds_limit", "name": "Balance Exceeds Credit Limit",
     "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
     "description": "The reported balance is higher than the original credit limit."},
    {"key": "double_reporting", "name": "Double/Duplicative Reporting",
     "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
     "description": "The same debt appears from both the original creditor and a collector."},
    {"key": "missing_due_date", "name": "Missing Due Date",
     "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
     "description": "The account is missing a due date, which is required for accurate reporting."},
    {"key": "missing_payment_amount", "name": "Missing Payment Amount",
     "section": "15 U.S.C. § 1681s-2(a)(1)(B)",
     "description": "The scheduled monthly payment amount is missing, making debt-to-income calculations inaccurate."},
]

SOLUTION_CARDS = [
    {"key": "remove", "name": "Remove Account",
     "description": "Remove this account entirely from my credit report."},
    {"key": "update_status", "name": "Update Account Status",
     "description": "Correct the account status to reflect accurate information."},
    {"key": "delete_history", "name": "Delete Inaccurate Payment History",
     "description": "Remove incorrect late payment or delinquency entries from the payment history."},
    {"key": "correct_balance", "name": "Correct Balance to $0",
     "description": "Update the balance to $0 for a paid or closed account."},
    {"key": "remove_duplicate", "name": "Remove Duplicate Entry",
     "description": "Delete the duplicate reporting of this debt."},
    {"key": "add_missing_info", "name": "Add Missing Information",
     "description": "Add the missing data fields (due date, payment amount, creditor name)."},
]


@disputes_bp.route('/select-account', methods=['GET'])
@login_required
def select_account():
    items = session.get('negative_items', [])
    return render_template('select_negative.html', negative_items=items)


# ─── Tier 1: Notice of Dispute ───

@disputes_bp.route('/tier1-notice', methods=['GET'])
@login_required
def tier1_notice():
    """Show the Tier 1 Notice of Dispute screen with accounts grouped by bureau."""
    items = session.get('negative_items', [])
    if not items:
        flash("No accounts found. Please upload a credit report first.", "error")
        return redirect(url_for('disputes.upload_pdf'))

    # Normalize bureau names to match BUREAU_ADDRESSES keys
    BUREAU_NAME_MAP = {
        'experian': 'Experian',
        'transunion': 'TransUnion',
        'equifax': 'Equifax',
    }

    accounts_by_bureau = {}
    for item in items:
        raw = (item.get('bureau') or 'Unknown').lower().strip()
        bureau = BUREAU_NAME_MAP.get(raw, raw.title())
        if bureau not in accounts_by_bureau:
            accounts_by_bureau[bureau] = []
        accounts_by_bureau[bureau].append(item)

    return render_template('tier1_notice.html', accounts_by_bureau=accounts_by_bureau)


@disputes_bp.route('/tier1-notice', methods=['POST'])
@login_required
@require_pro_or_business
def generate_tier1_notices():
    """Generate one Notice of Dispute letter per bureau."""
    import traceback
    import tempfile

    items = session.get('negative_items', [])
    if not items:
        flash("No accounts found. Please upload a credit report first.", "error")
        return redirect(url_for('disputes.upload_pdf'))

    # Normalize bureau names
    BUREAU_NAME_MAP = {
        'experian': 'Experian',
        'transunion': 'TransUnion',
        'equifax': 'Equifax',
    }
    accounts_by_bureau = {}
    for item in items:
        raw = (item.get('bureau') or 'Unknown').lower().strip()
        bureau = BUREAU_NAME_MAP.get(raw, raw.title())
        if bureau not in accounts_by_bureau:
            accounts_by_bureau[bureau] = []
        accounts_by_bureau[bureau].append(item)

    # Build client context from current user
    user = current_user
    base_context = {
        'client_full_name': f"{user.first_name} {user.last_name}",
        'client_address': '',
        'client_city_state_zip': '',
        'today_date': datetime.utcnow().strftime('%B %d, %Y'),
    }

    generated_ids = []
    errors = []

    for bureau, accounts in accounts_by_bureau.items():
        # Fresh copy per bureau so address doesn't bleed across iterations
        client_context = dict(base_context)

        # Get bureau mailing address
        bureau_info = BUREAU_ADDRESSES.get(bureau, {})
        if bureau_info:
            client_context['bureau_address'] = (
                f"{bureau_info.get('address1', '')}, "
                f"{bureau_info.get('city', '')} {bureau_info.get('state', '')} {bureau_info.get('zip', '')}"
            )

        # ── Generate letter via GPT with error handling ──
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

        # ── Convert letter to PDF and upload ──
        pdf_url = None
        try:
            upload_folder = current_app.config['UPLOAD_FOLDER']
            os.makedirs(upload_folder, exist_ok=True)

            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f'Notice_{bureau}_{timestamp}.pdf'
            pdf_path = letter_to_pdf(letter_text, os.path.join(upload_folder, pdf_filename))

            if cloud_configured():
                cloud_result = upload_from_path(
                    pdf_path,
                    folder=f"users/{user.id}/notices",
                    filename=pdf_filename.rsplit('.', 1)[0]
                )
                if cloud_result:
                    pdf_url = cloud_result['secure_url']
            else:
                # Local storage — copy to user folder
                user_folder = os.path.join(upload_folder, str(user.id))
                os.makedirs(user_folder, exist_ok=True)
                import shutil
                shutil.copy2(pdf_path, os.path.join(user_folder, pdf_filename))
                pdf_url = generate_public_pdf_url(pdf_filename)
        except Exception as e:
            traceback.print_exc()
            # PDF generation failed — still save the letter, just without a PDF
            pdf_url = None

        # ── Save as MailedLetter ──
        account_names = ', '.join(a.get('account_name', '') for a in accounts)
        ml = MailedLetter(
            user_id=user.id,
            letter_text=letter_text,
            bureau=bureau,
            round_number=session.get('current_round', 1),
            account_name=account_names,
            tier='notice',
            outcome='pending',
            pdf_url=pdf_url,
        )
        db.session.add(ml)
        db.session.flush()  # Get the ID before commit
        generated_ids.append(ml.id)

    db.session.commit()

    if errors and not generated_ids:
        # All bureaus failed
        flash(f"Failed to generate notices: {'; '.join(errors)}", "error")
        return redirect(url_for('disputes.tier1_notice'))

    if errors:
        # Some succeeded, some failed
        flash(f"Generated {len(generated_ids)} notice(s), but had errors: {'; '.join(errors)}", "warning")

    # Store only the DB IDs in session — not full letter text (avoids cookie overflow)
    session['tier1_letter_ids'] = generated_ids

    flash(f"Generated {len(generated_ids)} Notice of Dispute letter(s).", "success")
    return redirect(url_for('disputes.tier1_review'))


@disputes_bp.route('/tier1-review', methods=['GET'])
@login_required
def tier1_review():
    """Show generated Tier 1 letters for review, queried from DB by IDs."""
    letter_ids = session.get('tier1_letter_ids', [])
    if not letter_ids:
        # Fallback: show most recent notice-tier letters for this user
        letters = MailedLetter.query.filter_by(
            user_id=current_user.id, tier='notice'
        ).order_by(MailedLetter.created_at.desc()).limit(3).all()
        if not letters:
            flash("No Notice of Dispute letters found. Generate them first.", "info")
            return redirect(url_for('disputes.tier1_notice'))
    else:
        letters = MailedLetter.query.filter(
            MailedLetter.id.in_(letter_ids),
            MailedLetter.user_id == current_user.id
        ).all()
        if not letters:
            flash("Could not find the generated letters. Please try again.", "error")
            return redirect(url_for('disputes.tier1_notice'))

    return render_template(
        'tier1_review.html',
        letters=letters,
        bureau_addresses=BUREAU_ADDRESSES,
    )


@disputes_bp.route('/tier1-mail/<int:letter_id>', methods=['POST'])
@login_required
@require_pro_or_business
def tier1_mail(letter_id):
    """Mail a single Tier 1 Notice of Dispute via DocuPost."""
    ml = MailedLetter.query.filter_by(id=letter_id, user_id=current_user.id).first()
    if not ml:
        flash("Letter not found.", "error")
        return redirect(url_for('disputes.tier1_review'))

    # ── Resolve PDF URL ──
    pdf_url = ml.pdf_url
    if not pdf_url:
        # PDF wasn't generated earlier — try generating now
        try:
            upload_folder = current_app.config['UPLOAD_FOLDER']
            os.makedirs(upload_folder, exist_ok=True)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f'Notice_{ml.bureau}_{timestamp}.pdf'
            pdf_path = letter_to_pdf(ml.letter_text, os.path.join(upload_folder, pdf_filename))

            if cloud_configured():
                cloud_result = upload_from_path(
                    pdf_path,
                    folder=f"users/{current_user.id}/notices",
                    filename=pdf_filename.rsplit('.', 1)[0]
                )
                if cloud_result:
                    pdf_url = cloud_result['secure_url']
            else:
                user_folder = os.path.join(upload_folder, str(current_user.id))
                os.makedirs(user_folder, exist_ok=True)
                import shutil
                shutil.copy2(pdf_path, os.path.join(user_folder, pdf_filename))
                pdf_url = generate_public_pdf_url(pdf_filename)

            if pdf_url:
                ml.pdf_url = pdf_url
                db.session.commit()
        except Exception as e:
            flash(f"Could not generate PDF for {ml.bureau}: {str(e)}", "error")
            return redirect(url_for('disputes.tier1_review'))

    if not pdf_url:
        flash(f"No PDF available for {ml.bureau}. Please try regenerating.", "error")
        return redirect(url_for('disputes.tier1_review'))

    # ── Build recipient from bureau addresses ──
    bureau_info = BUREAU_ADDRESSES.get(ml.bureau, {})
    if not bureau_info:
        flash(f"No mailing address found for {ml.bureau}.", "error")
        return redirect(url_for('disputes.tier1_review'))

    recipient = {
        'name': bureau_info.get('name', ''),
        'company': bureau_info.get('company', ''),
        'address1': bureau_info.get('address1', ''),
        'address2': bureau_info.get('address2', ''),
        'city': bureau_info.get('city', ''),
        'state': bureau_info.get('state', ''),
        'zip': bureau_info.get('zip', ''),
    }

    # ── Build sender from form data ──
    sender = {
        'name': request.form.get('from_name', f"{current_user.first_name} {current_user.last_name}"),
        'company': '',
        'address1': request.form.get('from_address1', ''),
        'address2': request.form.get('from_address2', ''),
        'city': request.form.get('from_city', ''),
        'state': request.form.get('from_state', ''),
        'zip': request.form.get('from_zip', ''),
    }

    # Validate sender has at least an address
    if not sender['address1'] or not sender['city'] or not sender['state'] or not sender['zip']:
        flash("Please fill in your return address before mailing.", "error")
        return redirect(url_for('disputes.tier1_review'))

    mail_options = {
        'mail_class': request.form.get('mail_class', 'usps_first_class'),
        'servicelevel': request.form.get('servicelevel', ''),
        'color': 'false',
        'doublesided': 'false',
        'return_envelope': 'false',
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
        # Update the MailedLetter record with delivery info
        ml.delivery_status = 'submitted'
        if result.get('letter_id'):
            ml.docupost_letter_id = str(result['letter_id'])
        if result.get('cost'):
            ml.docupost_cost = float(result['cost'])
        ml.mailed_at = datetime.utcnow()
        ml.mail_class = mail_options['mail_class']
        ml.service_level = mail_options['servicelevel'] or None
        db.session.commit()

        flash(f"{ml.bureau} Notice of Dispute submitted for mailing! Track it in your Dispute Folder.", "success")
    else:
        flash(f"DocuPost error for {ml.bureau}: {result.get('error', 'Unknown error')}", "error")

    return redirect(url_for('disputes.tier1_review'))


# ─── Bureau Assault: Issue/Solution Selection ───

@disputes_bp.route('/tier2-issues', methods=['GET'])
@login_required
def tier2_issues():
    """Show selectable issue/solution cards for the current account."""
    account_name = session.get('account_name', '')
    account_number = session.get('account_number', '')
    items = session.get('negative_items', [])

    # Find the specific account
    account = None
    for item in items:
        if item.get('account_number') == account_number or item.get('account_name') == account_name:
            account = item
            break

    if not account:
        account = {
            'account_name': account_name,
            'account_number': account_number,
            'account_type': session.get('account_type', ''),
            'status': session.get('status', ''),
            'inaccuracies': [],
        }

    # Determine which issues were auto-detected
    detected_keys = set()
    for inac_text in account.get('inaccuracies', []):
        text = inac_text.lower()
        if 'status' in text and ('contradict' in text or 'paying as agreed' in text):
            detected_keys.add('status_contradicts_history')
        elif 'account type' in text and ('mismatch' in text or 'open account' in text):
            detected_keys.add('account_type_mismatch')
        elif 'original creditor' in text:
            detected_keys.add('original_creditor_not_reflected')
        elif 'closed' in text and 'balance' in text:
            detected_keys.add('closed_account_with_balance')
        elif 'charge-off' in text and 'status' in text:
            detected_keys.add('chargeoff_not_in_status')
        elif 'exceeds' in text and 'limit' in text:
            detected_keys.add('balance_exceeds_limit')
        elif 'double' in text or 'duplicat' in text:
            detected_keys.add('double_reporting')
        elif 'missing' in text and 'due date' in text:
            detected_keys.add('missing_due_date')
        elif 'missing' in text and 'payment amount' in text:
            detected_keys.add('missing_payment_amount')

    return render_template('tier2_issues.html',
        account=account,
        all_issues=ISSUE_CARDS,
        detected_keys=detected_keys,
        solutions=SOLUTION_CARDS,
    )


@disputes_bp.route('/tier2-issues', methods=['POST'])
@login_required
def save_tier2_issues():
    """Save selected issues/solutions and proceed to template selection."""
    selected_issues = request.form.getlist('issues')
    selected_solutions = request.form.getlist('solutions')

    # Build the issue text from selected cards
    issue_parts = []
    for issue_key in selected_issues:
        for card in ISSUE_CARDS:
            if card['key'] == issue_key:
                issue_parts.append(card['name'])
                break

    # Build the action text from selected solutions
    action_parts = []
    for sol_key in selected_solutions:
        for card in SOLUTION_CARDS:
            if card['key'] == sol_key:
                action_parts.append(card['name'])
                break

    # Store in session for the existing define_details → choose_template flow
    session['account_name'] = request.form.get('account_name', session.get('account_name', ''))
    session['account_number'] = request.form.get('account_number', session.get('account_number', ''))
    session['status'] = request.form.get('status', session.get('status', ''))
    session['issue'] = '; '.join(issue_parts) if issue_parts else 'Inaccurate reporting'
    session['action'] = '; '.join(action_parts) if action_parts else 'Remove this account from my credit report'
    session['selected_issues'] = selected_issues
    session['selected_solutions'] = selected_solutions

    return redirect(url_for('disputes.choose_template'))


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

    flash("Account confirmed! Next: Select who you're disputing with.", "success")
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
    return redirect(url_for('disputes.tier2_issues'))


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

    # Pull parser results from session to get inaccuracy details
    parsed_accounts = session.get('negative_items', [])
    target_number = session.get('account_number', '')

    # Filter to the account being disputed
    relevant_accounts = [
        acct for acct in parsed_accounts
        if acct.get('account_number') == target_number
        and acct.get('inaccuracies')
    ]

    if relevant_accounts:
        # Use build_prompt to inject inaccuracy details with FCRA citations
        pack_key = session.get('prompt_pack', 'default')
        prompt, has_inaccuracies, has_legal = build_prompt(pack_key, 0, data, parsed_accounts=relevant_accounts)
        letter_text = generate_letter(prompt, has_inaccuracies=has_inaccuracies, has_legal_research=has_legal)
    else:
        # No inaccuracies found — use the template as-is
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
        if cloud_configured():
            from datetime import datetime as dt
            timestamp = dt.utcnow().strftime('%Y%m%d_%H%M%S')
            result = upload_file(uploaded, folder=f"users/{current_user.id}/mail", resource_type="raw")
            if result:
                pdf_url = result['secure_url']
        else:
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
        session_pdf = session.get('final_pdf_url', '')
        if session_pdf:
            if session_pdf.startswith('http'):
                # Already a full URL (Cloudinary or external)
                pdf_url = session_pdf
            else:
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

        if cloud_configured():
            # Upload to Cloudinary — the URL is publicly accessible
            cloud_result = upload_from_path(final_pdf, folder=f"users/{current_user.id}/packages", filename=backup_name.rsplit('.', 1)[0])
            pdf_serve_url = cloud_result['secure_url'] if cloud_result else None
        else:
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

        if cloud_configured():
            result = upload_file(file, folder=f"users/{current_user.id}/docs", resource_type="raw")
            if result:
                serve_url = result['secure_url']
            else:
                flash("Upload failed.", "error")
                return redirect(url_for('disputes.upload_doc'))
        else:
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
    """Serve uploaded documents — checks Cloudinary first, then local filesystem."""
    # Check if the filename is actually a Cloudinary URL stored in a Correspondence record
    doc = Correspondence.query.filter_by(user_id=current_user.id, filename=filename).first()
    if doc and doc.file_url and doc.file_url.startswith('http'):
        import requests as http_req
        try:
            resp = http_req.get(doc.file_url, timeout=15)
            response = make_response(resp.content)
            ext = doc.file_url.rsplit('.', 1)[-1].lower().split('?')[0] if '.' in doc.file_url.split('?')[0] else 'pdf'
            content_types = {'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}
            response.headers['Content-Type'] = content_types.get(ext, 'application/pdf')
            response.headers['Content-Disposition'] = 'inline'
            return response
        except Exception:
            return redirect(doc.file_url)

    upload_base = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    user_folder = os.path.join(upload_base, str(current_user.id))

    # Check per-user folder first (new uploads)
    if os.path.exists(os.path.join(user_folder, filename)):
        return send_from_directory(os.path.abspath(user_folder), filename)

    # Fall back to root uploads folder (old uploads)
    if os.path.exists(os.path.join(upload_base, filename)):
        return send_from_directory(os.path.abspath(upload_base), filename)

    abort(404)


# ═══════════════════════════════════════════════════════════════════
# RESPONSE MODE — Log bureau/creditor responses and escalate disputes
# ═══════════════════════════════════════════════════════════════════

@disputes_bp.route('/dispute/<int:letter_id>/log-response', methods=['GET', 'POST'])
@login_required
def log_response(letter_id):
    """Log the bureau/creditor response for a mailed letter."""
    letter = MailedLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)

    if request.method == 'POST':
        outcome = request.form.get('outcome', '').strip()
        response_text = request.form.get('response_text', '').strip()

        if outcome not in ('removed', 'updated', 'verified', 'stall', 'no_response'):
            flash("Please select a valid outcome.", "error")
            return redirect(url_for('disputes.log_response', letter_id=letter_id))

        letter.outcome = outcome
        letter.response_received_at = datetime.utcnow()

        if response_text:
            letter.response_text = response_text

        # Handle file upload (response letter PDF/image)
        file = request.files.get('response_file')
        if file and file.filename:
            filename = secure_filename(file.filename)
            if cloud_configured():
                result = upload_file(file, folder=f"users/{current_user.id}/responses", resource_type="auto")
                if result:
                    letter.response_file_url = result['secure_url']
            else:
                user_folder = os.path.join(
                    current_app.config.get('UPLOAD_FOLDER', 'uploads'),
                    str(current_user.id), 'responses'
                )
                os.makedirs(user_folder, exist_ok=True)
                filepath = os.path.join(user_folder, filename)
                file.save(filepath)
                letter.response_file_url = f"responses/{filename}"

        db.session.commit()

        # Route based on outcome
        if outcome in ('removed', 'updated'):
            flash(f"Account marked as {outcome}. Great progress!", "success")
            return redirect(url_for('disputes.dispute_folder'))
        else:
            # Escalation triggers — run Legal Research Agent
            return redirect(url_for('disputes.research_results', letter_id=letter_id))

    return render_template('log_response.html', letter=letter)


@disputes_bp.route('/dispute/<int:letter_id>/research-results')
@login_required
def research_results(letter_id):
    """Show Legal Research Agent findings before generating escalation letter."""
    letter = MailedLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)

    # Run the Legal Research Agent
    from services.legal_research import research_dispute
    import json as json_mod

    inaccuracy_detail = None

    # Check if we have parsed account data in session
    parsed_accounts = session.get('negative_items', [])
    target_name = (letter.account_name or '').split('#')[0].strip().upper()

    for acct in parsed_accounts:
        acct_name = (acct.get('account_name') or '').upper()
        if target_name and (target_name in acct_name or acct_name in target_name):
            if acct.get('inaccuracies'):
                inaccuracy_detail = acct['inaccuracies'][0] if isinstance(acct['inaccuracies'][0], str) else acct['inaccuracies'][0].get('description', '')
                break

    package = research_dispute(
        company_name=target_name or letter.account_name or '',
        inaccuracy_detail=inaccuracy_detail,
        bureau_response=letter.response_text,
        round_number=(letter.round_number or 1) + 1,
    )

    # Cache results on the letter
    letter.legal_research_json = json_mod.dumps({
        'cfpb_summary': package.get('cfpb_summary'),
        'case_law': package.get('case_law'),
        'fcra_citation': package.get('fcra_citation'),
    }, default=str)
    db.session.commit()

    return render_template('research_results.html',
                           letter=letter,
                           package=package,
                           next_round=(letter.round_number or 1) + 1)


@disputes_bp.route('/dispute/<int:letter_id>/escalate', methods=['POST'])
@login_required
def escalate_dispute(letter_id):
    """Generate an escalated Round 2+ letter using Legal Research Agent findings."""
    letter = MailedLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)

    from services.legal_research import research_for_prompt

    next_round = (letter.round_number or 1) + 1
    pack_key = request.form.get('prompt_pack', 'consumer_law')
    target_name = (letter.account_name or '').split('#')[0].strip()

    # Try to get inaccuracies from session
    inaccuracies = None
    parsed_accounts = session.get('negative_items', [])
    for acct in parsed_accounts:
        acct_name = (acct.get('account_name') or '').upper()
        if target_name.upper() in acct_name or acct_name in target_name.upper():
            if acct.get('inaccuracies'):
                inaccuracies = acct['inaccuracies']
                break

    legal_context = research_for_prompt(
        account_name=target_name,
        inaccuracies=inaccuracies,
        bureau_response=letter.response_text,
        round_number=next_round,
    )

    ctx = {
        'entity': letter.bureau or 'Bureau',
        'account_name': letter.account_name or '',
        'account_number': letter.account_number or '',
        'marks': '',
        'action': 'Remove this inaccurate account from my credit report',
        'issue': f'Previously disputed (Round {letter.round_number}) — response inadequate',
    }

    ctx['client_full_name'] = current_user.username
    ctx['today_date'] = datetime.now().strftime('%B %d, %Y')

    prompt, has_inaccuracies, has_legal = build_prompt(
        pack_key, 0, ctx,
        parsed_accounts=[acct for acct in parsed_accounts if inaccuracies] if inaccuracies else None,
        legal_research_context=legal_context,
    )

    letter_text = generate_letter(prompt, has_inaccuracies=has_inaccuracies, has_legal_research=has_legal)

    new_letter = MailedLetter(
        user_id=current_user.id,
        letter_text=letter_text,
        bureau=letter.bureau,
        round_number=next_round,
        account_name=letter.account_name,
        account_number=letter.account_number,
        previous_letter_id=letter.id,
    )
    db.session.add(new_letter)
    db.session.commit()

    session['generated_letter'] = letter_text
    session['escalation_letter_id'] = new_letter.id

    return render_template('escalation_review.html',
                           letter=new_letter,
                           previous=letter,
                           round_number=next_round)


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

    # Delete file — Cloudinary or local
    if doc.file_url and doc.file_url.startswith('http'):
        delete_file(doc.file_url)
    else:
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

        # Save temporarily for analysis (Cloudinary or local)
        import tempfile
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            upload.save(tmp.name)
            tmp.close()
            path = tmp.name
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
