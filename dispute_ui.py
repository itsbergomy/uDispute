from flask import Flask, request, jsonify, render_template, flash, redirect, session, url_for, send_file, current_app, send_from_directory, abort
from flask_migrate import Migrate
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.utils import ImageReader
from PyPDF2 import PdfMerger
from PIL import Image
from uuid import uuid4
from flask_login import login_required, LoginManager, current_user
from flask_mail import Mail, Message
from openai import OpenAI
from dotenv import load_dotenv
from models import User, db, login_user, logout_user, generate_password_hash, DisputeRound, Client, DailyLogEntry, MailedLetter, Correspondence, ClientReportAnalysis, WorkflowSetting, CustomLetter, Message, MessageThread
from datetime import datetime, timedelta
import json
import tempfile
import os
import hashlib
import re
import pdfplumber
import stripe
import fitz
import base64
import logging

load_dotenv()

openai_client = OpenAI()


DOCUPOST_API_TOKEN = os.getenv("DOCUPOST_API_TOKEN")
DOCUPOST_SENDLETTER_URL = "https://app.docupost.com/api/1.1/wf/sendletter"

stripe.api_key = os.getenv("STRIPE_TEST_SECRET_KEY")
STRIPE_TEST_PUBLISHABLE_KEY = os.getenv("STRIPE_TEST_PUBLISHABLE_KEY")


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = tempfile.gettempdir()
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dispute.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'smartflow'

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')

mail = Mail(app)

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

print(app.config['UPLOAD_FOLDER'])

db.init_app(app)
migrate = Migrate(app,db)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def extract_pdf_metrics(pdf_path):
    try:
        items = extract_negative_items_from_pdf(pdf_path)
    except Exception:
        return {'negative_count': 0, 'total_collections':0}

    negative_count = len(items)
    total_collections = sum(1 for item in items if 'collection' in (item.get('issue') or '').lower())

    return {
        'negative_count': negative_count,
        'total_collections': total_collections
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


def compute_pdf_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while True:
            data = f.read(8192)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()        

def is_round_complete():
    items = session.get('negative_items', [])
    if not items:
        return False
    disputed = session.get('disputed_accounts', [])
    return all(item['account_number'] in disputed for item in items)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def pdf_to_base64_images(pdf_path, max_pages=5):
    images = []
    try:
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=150)
            image_bytes = pix.tobytes("png")
            b64_image = base64.b64encode(image_bytes).decode("utf-8")
            images.append(f"data:image/png;base64,{b64_image}")
    except Exception as e:
        raise ValueError(f"Failed to open PDF: {e}")
    return images     


def vision_filter_accounts(negative_items, file_path, max_pages=5):

    images = pdf_to_base64_images(file_path, max_pages=max_pages)

    accounts_summary = [
        {
            "account_name": acct["account_name"],
            "account_number": acct["account_number"],
            "status": acct["status"],
            "payment_history": acct.get("raw_payment_lines", [])
        }
        for acct in negative_items
    ]

    vision_prompt = f"""
We have extracted these accounts from a credit report PDF:

{json.dumps(accounts_summary, indent=2)}

IMPORTANT DEFINITIONS:
- A “late bucket” is any entry in the payment-history grid showing:
    • “30” (30 days past due)
    • “60” (60 days past due)
    • “90” (90 days past due)
    • “120” (120 days past due)
    • the words “Charge-off” (or “CO” when used to mean charge-off)
    • "C" (collection)

- A “clean” history line is one showing only:
    • a check-mark ✓
    • a dash “–”

- "CLS" means "closed in good standing" and is normally positive but IF you see any late bucket (30/60/90/120/CO/C) in the same grid, YOU MUST treat that whole account as negative.  

TASK:
For each account above, look at both:
  1. Its status text (e.g. “Paid, Closed/Never Late”, “Current”, “Collection Account”)
  2. Its payment-history grid (using the definitions above)

Mark an account as "skip" ONLY IF:
  • The status is positive (e.g. “Paid”, “Never Late”, “Closed”, “Current”),  
  • and its payment history grid shows **only** clean buckets (✓, or –),
  • and you see no late buckets (30, 60, 90, 120, CO, C).  

Otherwise mark it as "keep".

RETURN ONLY valid JSON in this format:

[
  {{ "account_number": "12345", "action": "keep" }},
  {{ "account_number": "67890", "action": "skip" }},
  ...
]
"""

    vision_inputs = (
        [{"type":"image_url", "image_url":{"url":img,"detail":"high"}} for img in images]
        + [{"type":"text", "text":vision_prompt}]
    )
    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role":"user","content":vision_inputs}],
        temperature=0
    )

    raw = resp.choices[0].message.content or ""
    # Strip code fences, if any
    raw = raw.strip()
    if raw.startswith("```"):
        # remove leading ```json or ```
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    # Extract the first JSON array [...]
    m = re.search(r"\[\s*\{.*\}\s*\]", raw, re.S)
    if not m:
        # fallback: try to parse entire raw if it looks like JSON
        m = re.match(r"\[.*\]$", raw, re.S)
    if not m:
        # can't find JSON — bail out and return original list
        return negative_items

    json_str = m.group(0)
    try:
        decisions = json.loads(json_str)
    except json.JSONDecodeError:
        return negative_items

    # 5) filter
    keep_set = {d["account_number"] for d in decisions if d.get("action") == "keep"}
    return [acct for acct in negative_items if acct["account_number"] in keep_set]



def extract_negative_items_from_pdf(file_path):
    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    blocks = full_text.split("Account name")
    negative_items = []

    # 1) grid only cares about real number buckets now
    grid_regex = re.compile(r'\b(?:30|60|90|120|C|)\b')

    # 2) if status has any of these words, we call it “clean”
    clean_regex = re.compile(
        r'\b(?:open|current|paid(?:\s+as\s+agreed)?|closed|never\s+late|'
        r'exceptional\s+payment\s+history)\b',
        re.IGNORECASE
    )

    # 3) real “negative” statuses
    status_regex = re.compile(
        r'\b(charged\s+off|charge-off|repossession|collection(?:\s+account)?|'
        r'past\s+due|delinquent|settlement|written\s+off)\b',
        re.IGNORECASE
    )

    for block in blocks[1:]:
        lines = block.strip().splitlines()
        data = {
            "account_name": None,
            "account_number": None,
            "account_type": None,
            "balance": None,
            "status": None,
            "issue": None
        }

        # --- Account name on first line ---
        first = lines[0].strip()
        m = re.match(r"(.+?)\s+Balance", first)
        data["account_name"] = m.group(1).strip() if m else first

        # --- Parse fields & payment history lines ---
        payment_history = []
        in_ph = False
        for line in lines:
            low = line.lower()

            if "payment history" in low:
                in_ph = True
                continue
            if in_ph:
                # stop at blank or next header
                if not line.strip() or line.lower().startswith(("account name","account number")):
                    in_ph = False
                else:
                    payment_history.append(line.strip())
                continue

            if "account number" in low and not data["account_number"]:
                mm = re.search(r"account number[:\s-]*(\S+)", line, re.I)
                if mm: data["account_number"] = mm.group(1).strip()

            if "account type" in low and not data["account_type"]:
                mm = re.search(r"account type[:\s]*(.+)", line, re.I)
                if mm: data["account_type"] = mm.group(1).strip()

            if "balance" in low and not data["balance"]:
                mm = re.search(r"balance[:\s-]*\$?([\d,]+)", line, re.I)
                if mm: data["balance"] = f"${mm.group(1).strip()}"

            if "status" in low and not data["status"]:
                mm = re.search(r"status[:\s]*(.+?)(?:\.|$)", line, re.I)
                if mm: data["status"] = mm.group(1).strip()

        status_text = (data["status"] or "").strip()
        grid_text = " ".join(payment_history)

        # ─── NEW CLEAN-ACCOUNT SKIP ───
        # if the status says “paid”, “never late”, “closed”, etc.
        # AND we see absolutely NO 30/60/90/120 buckets in the history,
        # then it’s truly a positive account → skip.
        if clean_regex.search(status_text) and not grid_regex.search(grid_text):
            continue

        # ─── Now detect real issues ───
        grid_issue = bool(grid_regex.search(grid_text))
        status_issue = bool(status_regex.search(status_text))
        acct_issue = "collection" in (data["account_type"] or "").lower()

        if not (grid_issue or status_issue or acct_issue):
            # nothing derogatory here
            continue

        # ─── Assign top-priority issue label ───
        if grid_issue:
            data["issue"] = "Late payments / Charge-off in payment history"
        elif status_issue:
            data["issue"] = status_text
        else:
            data["issue"] = "Collection account"

        negative_items.append(data)

    negative_items = vision_filter_accounts(negative_items, file_path)

    return negative_items

def run_report_analyzer_for_client(client_record):
    if not client_record.pdf_filename:
        raise ValueError("No Experian PDF uploaded for this client.")
    
    pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], client_record.pdf_filename)
    if not os.path.exists(pdf_path):
        raise ValueError("Stored PDF file not found.")

    metrics = extract_pdf_metrics(pdf_path)
    parsed_negative_count = metrics.get("negative_count", 0)
    parsed_collections_count = metrics.get("total_collections", 0)

    base64_images = pdf_to_base64_images(pdf_path)

    vision_prompt = f"""
You are a senior credit analyst trained in U.S. consumer credit laws, FICO scoring models, and bank underwriting data points.

IMPORTANT:
These numbers were deterministically parsed and MUST be used exactly:
- Negative Accounts: {parsed_negative_count}
- Collection Accounts: {parsed_collections_count}

TASK:
1. Summarize FICO, utilization %, total debt, negative & collection counts, avg/oldest age.
2. Classify as "Needs Repair", "Thin Profile", or "Funding Ready".
3. Provide 3–4 aggresive action steps.
4. List 3–5 score factors.
5. Scan the report images and identify:
    Inaccurate Reporting: any account whose payment-history buckets do not progress correctly (e.g., 30, 30, 60 instead of 30, 60, 90).
    Incomplete Information: any account grid missing required fields (e.g., missing monthly payment, missing account type, etc.).

OUTPUT ONLY valid JSON:
{{
  "summary": "...",
  "status": "...",
  "recommendations": [...],
  "score_factors": [...],
  "inaccurate_accounts": [
    {{
      "account_name": "...",
      "account_number": "...",
      "issue": "payment buckets [30,30,60] do not progress"
    }}
  ],
  "incomplete_accounts": [
    {{
      "account_name": "...",
      "account_number": "...",
      "missing_fields": ["monthly payment"]
    }}
  ],
  "numeric_fields": {{
    "credit_score": int|null,
    "utilization":  int|null,
    "total_debt":   int|null,
    "total_collections": {parsed_collections_count},
    "negative_accounts":  {parsed_negative_count},
    "average_age_years": "...",
    "oldest_account_years": "..."
  }}
}}
"""
    vision_inputs = [
        {"type":"image_url","image_url":{"url":img,"detail":"high"}}
        for img in base64_images
    ] + [{"type":"text","text":vision_prompt}]

    # -- Call OpenAI Vision --
    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role":"user","content": vision_inputs}],
        temperature=0.3
    )

    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```json"):
        raw = raw.replace("```json","").replace("```","").strip()
    analysis = json.loads(raw)

    # -- Enforce Numeric Consistency --
    num = analysis.get("numeric_fields", {})
    analysis["num_collections"] = parsed_collections_count
    analysis["negative_count"] = parsed_negative_count
    analysis["fico_score"] = num.get("credit_score", "N/A")
    analysis["utilization"] = num.get("utilization", 0)
    analysis["total_debt"] = num.get("total_debt", 0)
    analysis["average_credit_age"] = num.get("average_age_years", "N/A")
    analysis["oldest_account_age"] = num.get("oldest_account_years", "N/A")
    analysis["summary_text"] = analysis.get("summary", "")
    analysis["status_text"] = analysis.get("status", "")
    analysis["recommendations"] = analysis.get("recommendations", [])

    return analysis  

def save_client_analysis(client, analysis):
    new_analysis = ClientReportAnalysis(
        client_id=client.id,
        analysis_json=json.dumps(analysis)
    )
    db.session.add(new_analysis)
    db.session.commit()
    return new_analysis

def send_analysis_email(client, analysis):
    print(f"Sending email to {client.email} with analysis for {client.first_name} {client.last_name}")
    msg=Message(
        subject=f"DisputeGPT Analysis Results - For {client.first_name} {client.last_name}",
        sender=app.config['MAIL_USERNAME'],
        recipients=[client.email]
    )

    html_body = render_template("email/analysis_summary.html", client=client, analysis=analysis)
    msg.html = html_body
    mail.send(msg)    



@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload-pdf', methods=['GET', 'POST'])
def upload_pdf():
    if request.method == 'POST':
        if current_user.is_authenticated: 
            if current_user.plan == 'free':
                if free_user_limit_for_dispute(current_user):
                    flash("❌ Free plan: You must wait 48 hours between dispute rounds.", "error")
                    return redirect(url_for('index'))
                
        if 'pdfFile' not in request.files:
            return jsonify({"error": 'No file selected'}), 400

        file = request.files['pdfFile']
        if file.filename == '':
            return jsonify({"error": 'No file selected'}), 400

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            pdf_hash = compute_pdf_hash(filepath)
            session['pdf_hash'] = pdf_hash


            negative_items = extract_negative_items_from_pdf(filepath)
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
                flash("✅ New PDF detected. Starting Round 1.", "success")
                return redirect('/select-account')

            else:
                session['current_round'] = existing_round.round_number
                session['disputed_accounts'] = existing_round.get_disputed_accounts()

                if all(item['account_number'] in session['disputed_accounts'] for item in negative_items):
                    return redirect(url_for('confirm_next_round'))
                
                flash(f"✅ Resuming Round {existing_round.round_number}.", "info")
                return redirect('/select-account')
        else:
            return jsonify({"error": "Invalid file type. Only PDFs allowed."}), 400

    return render_template('upload_pdf.html')       

        

@app.route('/confirm-next-round', methods=['GET', 'POST'])
def confirm_next_round():
    pdf_hash = session.get('pdf_hash')
    if not pdf_hash:
        flash("❌ Missing PDF context.", "error")
        return redirect(url_for('upload_pdf'))

    if request.method == 'POST':
        # user confirmed they want the next round
        session['pending_round_upgrade'] = False
        session['current_round'] = session.get('current_round', 1) + 1
        session['disputed_accounts'] = []
        # update your DisputeRound in the database if you need to...
        return redirect(url_for('select_account'))

    # GET: show the “Start Round N+1?” page
    current_round = session.get('current_round', 1)
    return render_template(
        'confirm_next_round.html',
        current_round=current_round
    )


@app.route('/choose-upload-action', methods=['GET'])
def choose_upload_action():
    # Grab the hash of the PDF we're working with
    current_hash = session.get('current_pdf')
    trackers = session.get('pdf_trackers', {})

    # If we don’t have any tracker for this PDF, send them back to upload
    if not current_hash or current_hash not in trackers:
        return redirect('/')

    # Pull this PDF’s tracker
    tracker = trackers[current_hash]
    round_num = tracker.get('round', 1)

    # Render the choice screen
    return render_template('choose_upload_action.html', round=round_num)

    
@app.route('/select-account', methods=['GET'])
def select_account():
    items = session.get('negative_items', [])
    return render_template('select_negative.html', negative_items=items)

@app.route('/confirm-account', methods=['GET'])
def confirm_account():
    account_name = request.args.get('account_name')
    account_number = request.args.get('account_number')
    status = request.args.get('status')

    return render_template('confirm_account.html', 
        account_name=account_name, 
        account_number=account_number, 
        status=status
    )
  

@app.route('/confirm-account/save', methods=['POST'])
def save_confirmed_account():
    print("✅ save_confirmed_account called")
    account_number = request.form.get('account_number')

    pdf_hash = session.get('pdf_hash')
    if not pdf_hash:
        flash("❌ Missing PDF context.", "error")
        return redirect(url_for('upload_pdf'))
    
    round_record = DisputeRound.query.filter_by(
        user_id=current_user.id,
        pdf_hash=pdf_hash
    ).first()

    if not round_record:
        flash("❌ Could not find your dispute round record.", "error")
        return redirect(url_for('upload_pdf'))
    
    disputed_accounts = round_record.get_disputed_accounts()
    if account_number not in disputed_accounts:
        disputed_accounts.append(account_number)
        round_record.set_disputed_accounts(disputed_accounts)
        db.session.commit()

    flash("✅ Account confirmed for dispute.", "success")
    return redirect(url_for('select_entity'))    

@app.route('/select-entity', methods=['GET', 'POST'])
def select_entity():
    if request.method == 'POST':
        account_name = request.form.get('account_name')
        account_number = request.form.get('account_number')
        status = request.form.get('status')

        session['account_name'] = account_name
        session['account_number'] = account_number
        session['status'] = status

    # This will run for both GET (back button) and POST (form submission)
    return render_template('select_entity.html')

@app.route('/handle-entity', methods=['POST'])
def handle_entity():
    # Grab which bureau they clicked (Experian / TransUnion / Equifax)
    selected = request.form.get('entity')
    if not selected:
        flash("Please select an entity.", "error")
        return redirect(url_for('select_entity'))

    # Store it in session and move on to defining details
    session['selected_entity'] = selected
    return redirect(url_for('define_details'))

@app.route('/define-details', methods=['GET','POST'])
@login_required
def define_details():
    pack_key = session.get('prompt_pack', 'default')

    # always these
    core_fields = [
        ('action', 'What would you like them to do?'),
        ('issue',  'Brief summary of the dispute issue'),
    ]

    # extra for ACDV pack
    acdv_fields = [
        ('dispute_date', 'Original Dispute Date (YYYY-MM-DD)'),
        ('days', 'Deadline in business days')
    ] if pack_key == 'ACDV_response' else []

    all_fields = core_fields + acdv_fields

    if request.method == 'POST':
        # save each field into session
        for name, _ in all_fields:
            session[name] = request.form.get(name, '').strip()
        return redirect(url_for('choose_template'))

    # GET → render the form
    return render_template(
      'define_details.html',
      pack_key=pack_key,
      core_fields=core_fields,
      acdv_fields=acdv_fields,
      entity=session.get('selected_entity','')
    )


@app.route('/choose-template', methods=['GET','POST'])
@login_required
def choose_template():
    pack_key = session.get('prompt_pack','default')
    raw_templates = PACKS.get(pack_key, PACKS['default'])

    # build rendering context from session:
    ctx = {
      'entity':         session.get('selected_entity',''),
      'account_name':   session.get('account_name',''),
      'account_number': session.get('account_number',''),
      'marks':          session.get('status',''),
      'action':         session.get('action',''),
      'issue':          session.get('issue',''),
      # these two may be empty strings if not ACDV pack
      'dispute_date':   session.get('dispute_date',''),
      'days':           session.get('days',''),
    }

    filled = [tpl.format(**ctx) for tpl in raw_templates]

    if request.method=='POST':
        session['selected_template'] = request.form['template_text']
        return redirect(url_for('generate_letter_screen'))

    return render_template(
      'choose_template.html',
      templates=filled,
      pack_key=pack_key
    )

PACKS = {
    "default":[
        "Write a letter {action} for {entity} regarding {issue}. The account is {account_name} with account number {account_number} and has the following account status: {marks}",
        "I need a letter {action} for {entity} about an issue regarding {issue}. The account is {account_name} with account number {account_number} and has the following account status: {marks}"
    ],
    "arbitration":[
        "Draft an arbitration demand under 15 U.S.C. 1681e(b) to {entity}, account {account_number}, stating {issue}. I will {action}.",
        "Compose a formal arbitration request for {entity}, referencing {issue}, account {account_number}, and demanding {action}."
    ],
    "consumer_law":[
        "Write a letter to {entity} invoking the Fair Credit Billing Act regarding {issue} on account {account_number}. I request {action}.",
        "Craft a demand under the Fair Debt Collection Practices Act for {entity}, account {account_number}, disputing {issue} and seeking {action}."
    ],
    "ACDV_response": [
        "Compose a formal demand letter {action} to {entity} about the disputed credit file. File/Account No: {account_number}, Dispute Date: {dispute_date}. Invoke *Cushman v. Trans Union Corp.*, 115 F.3d 220 (3d Cir. 1997), and demand immediate production of the full Automated Consumer Dispute Verification (ACDV) record, including **Method of Verification**, **Submission Procedure**, and **FCRA Compliance Policies**, with delivery required within {days} business days or outline available FCRA remedies.",
        "Compose a formal demand letter {action} to {entity} regarding flawed reinvestigation procedures. File/Account No: {account_number}, Dispute Date: {dispute_date}. Invoke *Giliespie v. Equifax Info. Servs.*, 484 F.3d 938, which held CRAs liable for unreasonable investigation processes, and demand documented proof of each step of your reinvestigation protocol—including source contact logs, verification methodologies, and internal quality-control policies—with delivery required within {days} business days or outline available FCRA remedies for failure to comply.",
        "Write a formal demand letter to {entity} demanding immediate production of the full Automated Consumer Dispute Verification (ACDV) record related to the {account_name} and {account_number} and previous {dispute_date}."
    ]

}

@app.route('/prompt-packs', methods=['GET','POST'])
@login_required
def prompt_packs():
    PACKS = [
      {"key":"default", "name":"Default Pack", "description":"Your standard dispute templates."},
      {"key":"arbitration", "name":"Arbitration Pack", "description":"Prompts tailored for arbitration under 15 U.S.C. §1681e(b)."},
      {"key":"consumer_law","name":"Consumer Law Pack","description":"Templates citing various consumer-protection statutes."},
      {"key": "ACDV_response", "name":"ACDV Enforcement Pack", "description":"Prompts tailored to generate letters demanding the Automated Consumer Dispute Verification record, specifiying the method of verification, the submission procedure, and disclosure of the credit reporting agency's FCRA compliance policies."}
    ]

    if request.method == 'POST':
        session['prompt_pack'] = request.form['pack_key']
        # kick back into the normal flow:
        return redirect(url_for('index'))

    return render_template('prompt_packs.html', packs=PACKS)

@app.route('/generate-letter-screen', methods=['POST'])
def generate_letter_screen():
    template = request.form.get('template_text')
    session['selected_template'] = template

    return render_template('generate_letter.html')

    
@app.route('/generate-process')
def generate_letter():
    template = session['selected_template']
    data = {
        "action": session['action'],
        "issue": session['issue'],
        "entity": session['selected_entity'],
        "account_name": session['account_name'],
        "account_number": session['account_number'],
        "marks": session['status']
    }
    prompt = template.format(**data)
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    session['generated_letter'] = response.choices[0].message.content
    return redirect(url_for('final_review'))

@app.route('/final-review')
def final_review():
    letter = session.get('generated_letter')
    return render_template('final_review.html', letter=letter)
    


@app.route('/manual-mode', methods=['GET', 'POST'])
def manual_mode():
    if request.method == 'POST':
        # Capture form values
        form_data = request.form.to_dict()
        # Save session or pass to template/render next screen
        return jsonify(form_data)  # placeholder for now
    return render_template('manual_mode.html')

@app.route('/manual-mode', methods=['GET', 'POST'])
def define_details_manual():
    if request.method == 'POST':

        if current_user.is_authenticated: 
            if current_user.plan == 'free':
                now = datetime.utcnow()

            if current_user.last_round_time is None or (now - current_user.last_round_time > timedelta(hours=48)):
                current_user.manual_accounts_used = 0
                current_user.last_round_time = now
                db.session.commit()

            if current_user.manual_accounts_used >= 3:
                flash("❌ Free plan: You can only dispute 3 accounts in manual mode every 48 hours.", "error")
                return redirect(url_for('index'))    

        session['account_name'] = request.form.get('account_name', '').strip()
        session['account_number'] = request.form.get('account_number', '').strip()
        session['status'] = request.form.get('account_status', '').strip()
        session['selected_entity'] = request.form.get('entity', '').strip()
        session['action'] = request.form.get('action', '').strip()
        session['issue'] = request.form.get('issue', '').strip()

        # ✅ Flag this session as manual mode
        session['manual_mode'] = True

        if current_user.is_authenticated and current_user.plan == 'free':
            current_user.manual_accounts_used += 1
            current_user.last_round_time = datetime.utcnow()
            db.session.commit()

        return redirect(url_for('choose_template'))

    return render_template(
        'manual_mode.html',
        account_name=session.get('account_name', ''),
        account_number=session.get('account_number', ''),
        status=session.get('status', ''),
        selected_entity=session.get('selected_entity', ''),
        action=session.get('action', ''),
        issue=session.get('issue', '')
    )



@app.route('/mail-letter', methods=['GET', 'POST'])
def mail_letter():
    if request.method == 'GET':
        # Render a form that includes fields for all of these:
        #   to_name, to_company, to_address1, to_address2, to_city, to_state, to_zip,
        #   from_name, from_company, from_address1, from_address2,
        #   from_city, from_state, from_zip,
        #   class, servicelevel, color, doublesided, return_envelope, description
        return render_template('mail_letter.html',
            # you can prefill from_… from session if desired
            from_name=session.get('user_name',''),
            from_address1=session.get('user_address_line1',''),
            from_city=session.get('user_city',''),
            from_state=session.get('user_state',''),
            from_zip=session.get('user_zip','')
        )

    # POST: collect everything
    params = {
        'api_token': DOCUPOST_API_TOKEN,
        'pdf':       session.get('final_pdf_url'),   # your publicly hosted merged PDF
        # Recipient
        'to_name':     request.form.get('to_name',''),
        'to_company':  request.form.get('to_company',''),
        'to_address1': request.form.get('to_address1',''),
        'to_address2': request.form.get('to_address2',''),
        'to_city':     request.form.get('to_city',''),
        'to_state':    request.form.get('to_state',''),
        'to_zip':      request.form.get('to_zip',''),
        # Sender
        'from_name':     request.form.get('from_name', session.get('user_name','')),
        'from_company':  request.form.get('from_company',''),
        'from_address1': request.form.get('from_address1', session.get('user_address_line1','')),
        'from_address2': request.form.get('from_address2',''),
        'from_city':     request.form.get('from_city', session.get('user_city','')),
        'from_state':    request.form.get('from_state', session.get('user_state','')),
        'from_zip':      request.form.get('from_zip', session.get('user_zip','')),
        # Mail options
        'class':          request.form.get('class','usps_first_class'),
        'servicelevel':   request.form.get('servicelevel',''),
        'color':          request.form.get('color','false'),
        'doublesided':    request.form.get('doublesided','true'),
        'return_envelope':request.form.get('return_envelope','false'),
        'description':    request.form.get('description',''),
    }

    # post to DocuPost
    resp = request.post(DOCUPOST_SENDLETTER_URL, params=params)
    if resp.status_code == 200 and b"<Error>" not in resp.content:
        flash("✅ Your letter has been sent!", "success")
        return redirect(url_for('final_review'))
    else:
        flash(f"❌ DocuPost error: {resp.text}", "error")
        return redirect(url_for('mail_letter'))

@app.route('/convert-pdf', methods=['POST'])
def convert_pdf():
    # 1) Grab and validate the letter text
    letter_text = request.form.get('letter', '').strip()
    if not letter_text:
        return "Letter content is missing.", 400

    # Ensure upload folder exists
    upload_folder = app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)

    # 2) Generate the letter PDF via Platypus
    letter_pdf_path = os.path.join(upload_folder, 'letter.pdf')
    doc = SimpleDocTemplate(
        letter_pdf_path, pagesize=LETTER,
        leftMargin=inch, rightMargin=inch,
        topMargin=inch, bottomMargin=inch
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='Body', parent=styles['Normal'],
        fontSize=11, leading=14
    ))
    story = []
    for line in letter_text.split('\n'):
        if not line.strip():
            story.append(Spacer(1, 12))
        else:
            story.append(Paragraph(line.strip(), styles['Body']))
    doc.build(story)

    # 3) Convert uploads to individual PDFs with proper scaling
    page_w, page_h = LETTER
    margin = inch
    max_w = page_w - 2*margin
    max_h = page_h - 2*margin

    pdf_paths = [letter_pdf_path]
    for field in ('id_file', 'ssn_file', 'utility_file'):
        file = request.files.get(field)
        if not file or not file.filename:
            continue

        filename = secure_filename(file.filename)
        raw_path = os.path.join(upload_folder, filename)
        file.save(raw_path)
        ext = filename.rsplit('.',1)[-1].lower()

        # Images → PDF
        if ext in ('png','jpg','jpeg'):
            img = Image.open(raw_path).convert('RGB')
            orig_w, orig_h = img.size  # in pixels ~ points at 72dpi

            # base scale to fit within margins
            raw_scale = min(max_w/orig_w, max_h/orig_h)

            # allow moderate up-scale for SSN, clamp for others
            if field == 'ssn_file':
                scale = min(raw_scale, 1.3)  # up to 130%
            else:
                scale = min(raw_scale, 1.0)  # no up-scale

            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)

            # high quality resize
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            reader = ImageReader(resized)

            img_pdf = os.path.splitext(raw_path)[0] + '.pdf'
            c = pdfcanvas.Canvas(img_pdf, pagesize=LETTER)
            x = (page_w - new_w) / 2
            y = (page_h - new_h) / 2
            c.drawImage(reader, x, y, width=new_w, height=new_h)
            c.showPage()
            c.save()

            pdf_paths.append(img_pdf)

        # Already-PDF → append directly
        elif ext == 'pdf':
            pdf_paths.append(raw_path)

    # 4) Merge everything into one DisputePackage.pdf
    final_pdf = os.path.join(upload_folder, 'DisputePackage.pdf')
    merger = PdfMerger()
    for p in pdf_paths:
        merger.append(p)
    merger.write(final_pdf)
    merger.close()

    # 5) Stream the merged PDF back
    return send_file(
        final_pdf,
        as_attachment=True,
        download_name='DisputePackage.pdf',
        mimetype='application/pdf'
    )    
   
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        # 1) collect & validate
        fn = request.form['first_name'].strip()
        ln = request.form['last_name'].strip()
        un = request.form['username'].strip()
        em = request.form['email'].strip().lower()
        pw = request.form['password']

        if User.get_by_username(un):
            flash('Username already taken', 'error')
            return redirect(url_for('signup'))

        # 2) create user
        new_user = User(
            first_name=fn,
            last_name=ln,
            username=un,
            email=em,
            password=generate_password_hash(pw),
            plan='free'
        )
        db.session.add(new_user)
        db.session.commit()

        # 3) log them in
        login_user(new_user)
        flash("Welcome! You're on our Free plan.", 'success')

        # 4) redirect back to where they came from or home
        
        return redirect(url_for('index'))

    return render_template('register.html')


@app.route('/join-pro')
@login_required
def join_pro():
    return render_template('join_pro.html', stripe_test_publishable_key=STRIPE_TEST_PUBLISHABLE_KEY)

@app.route('/join-business')
@login_required
def join_business():
    return redirect(url_for('join_pro'))

@app.route('/business-dashboard')
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

    # Add stats block
    total_clients = len(clients)
    total_workflows_enabled = WorkflowSetting.query.filter_by(
        business_user_id=current_user.id,
        enabled=True
    ).count()

    stats = {
        'total_clients': total_clients,
        'workflows_enabled': total_workflows_enabled
    }

    # Optional: add letters and correspondence placeholders
    letters = []  # Replace with actual logic if used
    correspondence = []  # Replace with actual logic if used

    active_tab = request.args.get('tab', 'clients')

    return render_template("business_dashboard.html",
                           clients=clients,
                           selected_client=selected_client,
                           workflow_enabled=workflow_enabled,
                           stats=stats,
                           letters=letters,
                           correspondence=correspondence,
                           active_tab=active_tab)



@app.route('/clients/create', methods=['POST'])
@login_required
def create_client():
    first_name = request.form.get('first_name')
    last_name = request.form.get('last_name')
    email = request.form.get('email')

    if not all([first_name, last_name, email]):
        flash("All fields are required.")
        return redirect(url_for("business_dashboard"))

    client = Client(
        first_name=first_name,
        last_name=last_name,
        email=email,
        business_user_id=current_user.id  # ✅ Don't skip this
    )
    db.session.add(client)
    db.session.commit()

    thread = MessageThread(client_id=client.id)
    db.session.add(thread)
    db.session.commit()

    return redirect(url_for("business_dashboard"))


@app.route('/clients/<int:client_id>')
@login_required
def view_client(client_id):
    # Pull that client out of the database
    client = Client.query.get_or_404(client_id)

    # Clear stale parsed accounts if viewing a different client
    if session.get("parsed_accounts_client_id") != client_id:
        session.pop("client_parsed_accounts", None)
        session["parsed_accounts_client_id"] = client_id

    client_parsed_accounts = session.get("client_parsed_accounts", [])

    settings = WorkflowSetting.query.filter_by(client_id=client.id).all()
    workflow_settings = {s.key: s.enabled for s in settings}

    return render_template("view_client.html", client=client, client_parsed_accounts=client_parsed_accounts, workflow_settings=workflow_settings)


@app.route('/clients/<int:client_id>/upload-correspondence', methods=['POST'])
@login_required
def upload_correspondence(client_id):
    client = Client.query.get_or_404(client_id)
    file = request.files.get('correspondence_file')

    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        new_file = Correspondence(client_id=client.id, filename=filename)
        db.session.add(new_file)
        db.session.commit()

    return redirect(url_for('view_client', client_id=client_id))


@app.route('/view-correspondence/<filename>')
@login_required
def view_correspondence_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)



@app.route('/clients/<int:client_id>/edit', methods=['GET','POST'])
@login_required
def edit_client(client_id):
    client = Client.query.get_or_404(client_id)

    # ✅ Update this to match new schema
    if client.business_user_id != current_user.id:
        flash("Unauthorized", "error")
        return redirect(url_for('business_dashboard'))

    if request.method == 'POST':
        # --- 1) Basic profile fields ---
        client.first_name    = request.form['first_name']
        client.last_name     = request.form['last_name']
        client.email         = request.form['email']
        client.address_line1 = request.form.get('address_line1')
        client.address_line2 = request.form.get('address_line2')
        client.city          = request.form.get('city')
        client.state         = request.form.get('state')
        client.zip_code      = request.form.get('zip_code')
        client.round_status  = request.form.get('round_status')
        client.notes         = request.form.get('notes')

        # --- 2) File uploads ---
        uploads = [
            ('id_file', 'id_filename'),
            ('ssn_file', 'ssn_filename'),
            ('utility_file', 'utility_filename'),
            ('pdf_file', 'pdf_filename'),
        ]
        for field_name, model_attr in uploads:
            f = request.files.get(field_name)
            if f and f.filename:
                filename = f"{client.id}_{field_name}_{secure_filename(f.filename)}"
                full_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                f.save(full_path)
                setattr(client, model_attr, filename)

        db.session.commit()
        flash("Client updated", "success")
        return redirect(url_for('view_client', client_id=client.id))

    # GET → render form
    return render_template('edit_client.html', client=client)


@app.route('/client-files/<int:client_id>/<filetype>')
@login_required
def client_file(client_id, filetype):
    c = Client.query.get_or_404(client_id)
    if c.business_user_id != current_user.id:
        abort(403)

    # map the filetype to the correct attribute
    mapping = {
        'id':   c.id_filename,
        'ssn':  c.ssn_filename,
        'util': c.utility_filename,
        'pdf':  c.pdf_filename
    }
    fn = mapping.get(filetype)
    if not fn:
        abort(404)

    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        fn,
        as_attachment=False
    )

@app.route('/clients/<int:client_id>/run-analysis', methods=['POST'])
@login_required
def run_analysis_for_client(client_id):
    logging.warn(f"run-analysis called with client_id={client_id!r}")
    client = Client.query.get_or_404(client_id)
    logging.warn(f" loaded client object: {client!r}")
    if client.business_user_id != current_user.id:
        abort(403)

    if not client.pdf_filename:
        flash("No credit report uploaded!", "error")
        return redirect(url_for('view_client', client_id=client.id))
    
    pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], client.pdf_filename)
    analysis_data = run_report_analyzer_for_client(pdf_path)

    analysis = ClientReportAnalysis(
        client_id = client_id,
        analysis_json = json.dumps(analysis_data)
    )   
    db.session.add(analysis)
    db.session.commit()

    flash("✅ Report analysis complete!", "success")
    return redirect(url_for('view_client', client_id=client.id))


@app.route('/clients/<int:client_id>/messages', methods=['GET','POST'])
@login_required
def messages_thread(client_id):
    # 1) permission guard
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:
        abort(403)

    # 2) fetch or create the one thread per client
    thread = MessageThread.query.filter_by(client_id=client.id).first()
    if not thread:
        thread = MessageThread(client_id=client.id, business_user_id=current_user.id)
        db.session.add(thread)
        db.session.commit()

    if request.method == 'POST':
        # 3) save new message
        body = request.form.get('body','').strip()
        if body:
            msg = Message(thread_id=thread.id,
                          from_business=True,
                          body=body)
            db.session.add(msg)
            db.session.commit()
        # 4) Redirect back to GET so we never re‑render without thread
        return redirect(url_for('messages_thread', client_id=client.id))

    # 5) GET → render with `thread` in context
    return render_template('messages_thread.html', thread=thread)


@app.route("/analyses/<int:analysis_id>/update-recommendations", methods=["POST"])
@login_required
def update_recommendations(analysis_id):
    analysis = ClientReportAnalysis.query.get_or_404(analysis_id)
    client = Client.query.get_or_404(analysis.client_id)

    if client.business_user_id != current_user.id:
        abort(403)

    # Get the submitted recommendations, split by new lines
    raw_text = request.form.get("recommendations", "")
    updated_recs = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]

    try:
        # Parse, update, and save the JSON
        data = json.loads(analysis.analysis_json)
        data["recommendations"] = updated_recs
        analysis.analysis_json = json.dumps(data)
        db.session.commit()
        flash("✅ Recommendations updated successfully!", "success")
    except Exception as e:
        flash(f"❌ Error updating recommendations: {str(e)}", "error")

    return redirect(url_for("view_client", client_id=client.id))

@app.route('/analyses/<int:analysis_id>/send-email', methods=['POST'])
@login_required
def send_analysis_email_route(analysis_id):
    analysis_record = ClientReportAnalysis.query.get_or_404(analysis_id)
    client = Client.query.get_or_404(analysis_record.client_id)

    # ✅ Fix this line to reflect the new foreign key
    if client.business_user_id != current_user.id:
        abort(403)

    analysis = json.loads(analysis_record.analysis_json)
    send_analysis_email(client, analysis)
    flash("📧 Email sent to client!", "success")
    return redirect(url_for('view_client', client_id=client.id))


@app.route('/clients/<int:client_id>/mail-analysis', methods=['POST'])
@login_required
def mail_analysis_to_client(client_id):
    client = Client.query.get_or_404(client_id)
    if client.business_user_id != current_user.id:  # ✅ Updated check
        abort(403)

    latest_analysis = ClientReportAnalysis.query.filter_by(client_id=client.id).order_by(ClientReportAnalysis.created_at.desc()).first()
    if not latest_analysis:
        flash("No analysis found to email.", "error")
        return redirect(url_for('view_client', client_id=client.id))

    try:
        analysis_data = json.loads(latest_analysis.analysis_json)
        send_analysis_email(client, analysis_data)
        flash("📬 Analysis emailed to client!", "success")
    except Exception as e:
        flash(f"❌ Failed to send email: {e}", "error")

    return redirect(url_for('view_client', client_id=client.id))


@app.route("/client/<int:client_id>/run-disputegpt", methods=["POST"])
@login_required
def run_disputegpt_flow(client_id):
    client = Client.query.get_or_404(client_id)

    # Get user input
    account_number = request.form["account_number"]
    entity = request.form["entity"]
    action = request.form["action"]
    issue = request.form["issue"]
    prompt_pack = request.form.get("prompt_pack", "default")

    # Parse PDF → get account details
    parsed_accounts = extract_negative_items_from_pdf(os.path.join(app.config["UPLOAD_FOLDER"], client.pdf_filename))
    selected = next((acc for acc in parsed_accounts if acc["account_number"] == account_number), None)

    if not selected:
        flash("❌ Couldn't find the selected account.", "error")
        return redirect(url_for("view_client", client_id=client.id))

    # Build prompt + generate letter
    ctx = {
        "entity": entity,
        "account_name": selected["account_name"],
        "account_number": selected["account_number"],
        "marks": selected["status"],
        "action": action,
        "issue": issue,
        "dispute_date": "",  # optional
        "days": "",          # optional
    }

    custom_id = request.form.get("custom_letter_id")

    if custom_id:
        tpl = CustomLetter.query.get(int(custom_id))
        if not tpl or tpl.user_id != current_user.id:
            flash("❌ Invalid custom template.", "error")
            return redirect(url_for("view_client", client_id=client.id))
        prompt = tpl.body
    else:
        prompt = PACKS.get(prompt_pack, PACKS["default"])[0].format(**ctx)


    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    letter = response.choices[0].message.content

    # TODO: convert to PDF, attach ID + SSN card, send via DocuPost

    flash("✅ Letter generated!", "success")
    return render_template("disputegpt_result.html", client=client, letter=letter, custom_letters=current_user.custom_letters, custom_id=custom_id)

# in your Flask app

@app.route("/custom-letters")
@login_required
def list_custom_letters():
    letters = CustomLetter.query.filter_by(user_id=current_user.id).all()
    return render_template("custom_letters/list.html", letters=letters)

@app.route("/custom-letters/new", methods=["GET","POST"])
@login_required
def new_custom_letter():
    if request.method == "POST":
        name = request.form["name"]
        subject = request.form.get("subject","")
        body = request.form["body"]
        letter = CustomLetter(
          user_id=current_user.id,
          name=name, subject=subject, body=body
        )
        db.session.add(letter)
        db.session.commit()
        flash("✨ Custom letter saved!", "success")
        return redirect(url_for("list_custom_letters"))
    return render_template("custom_letters/new.html")

@app.route("/custom-letters/<int:letter_id>/edit", methods=["GET","POST"])
@login_required
def edit_custom_letter(letter_id):
    letter = CustomLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)
    if request.method=="POST":
        letter.name    = request.form["name"]
        letter.subject = request.form.get("subject","")
        letter.body    = request.form["body"]
        db.session.commit()
        flash("✏️ Custom letter updated.", "success")
        return redirect(url_for("list_custom_letters"))
    return render_template("custom_letters/edit.html", letter=letter)

@app.route("/custom-letters/<int:letter_id>/delete", methods=["POST"])
@login_required
def delete_custom_letter(letter_id):
    letter = CustomLetter.query.get_or_404(letter_id)
    if letter.user_id != current_user.id:
        abort(403)
    db.session.delete(letter)
    db.session.commit()
    flash("🗑️ Custom letter deleted.", "info")
    return redirect(url_for("list_custom_letters"))



@app.route("/client/<int:client_id>/finalize-disputegpt", methods=["POST"])
@login_required
def finalize_disputegpt_letter(client_id):
    client = Client.query.get_or_404(client_id)
    final_text = request.form["edited_letter"].strip()

    if not final_text:
        flash("❌ No letter content to finalize.", "error")
        return redirect(url_for("view_client", client_id=client.id))

    upload_folder = app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    # 1️⃣ Convert the final letter text into PDF
    letter_pdf_path = os.path.join(upload_folder, 'letter.pdf')
    doc = SimpleDocTemplate(letter_pdf_path, pagesize=LETTER,
        leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Body', parent=styles['Normal'], fontSize=11, leading=14))
    story = []
    for line in final_text.split('\n'):
        if not line.strip():
            story.append(Spacer(1, 12))
        else:
            story.append(Paragraph(line.strip(), styles['Body']))
    doc.build(story)

    # 2️⃣ Load ID and SSN and add to final PDF
    pdf_paths = [letter_pdf_path]
    page_w, page_h = LETTER
    margin = inch
    max_w = page_w - 2*margin
    max_h = page_h - 2*margin

    for attr in ("id_filename", "ssn_filename"):
        filename = getattr(client, attr)
        if not filename:
            continue
        path = os.path.join(upload_folder, filename)
        ext = filename.rsplit('.', 1)[-1].lower()

        # Image → Convert to PDF
        if ext in ("jpg", "jpeg", "png"):
            img = Image.open(path).convert("RGB")
            raw_scale = min(max_w / img.width, max_h / img.height)
            scale = min(raw_scale, 1.3 if attr == "ssn_filename" else 1.0)
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)

            resized = img.resize((new_w, new_h), Image.LANCZOS)
            reader = ImageReader(resized)

            img_pdf = os.path.splitext(path)[0] + ".pdf"
            c = pdfcanvas.Canvas(img_pdf, pagesize=LETTER)
            x = (page_w - new_w) / 2
            y = (page_h - new_h) / 2
            c.drawImage(reader, x, y, width=new_w, height=new_h)
            c.showPage()
            c.save()

            pdf_paths.append(img_pdf)

        elif ext == "pdf":
            pdf_paths.append(path)

    # 3️⃣ Merge into DisputePackage
    final_pdf = os.path.join(upload_folder, "DisputePackage.pdf")
    merger = PdfMerger()
    for p in pdf_paths:
        merger.append(p)
    merger.write(final_pdf)
    merger.close()

    # 4️⃣ Host the final PDF and stash in session
    final_url = url_for('client_file', client_id=client.id, filetype='DisputePackage', _external=True)
    session['final_pdf_url'] = final_url

    flash("✅ Letter finalized! Ready to mail.", "success")
    return redirect(url_for('mail_letter'))

@app.route('/client/<int:client_id>/extract-disputegpt', methods=['POST'])
@login_required
def extract_for_disputegpt(client_id):
    client = Client.query.get_or_404(client_id)

    if not client.pdf_filename:
        flash("❌ No PDF found for this client.", "error")
        return redirect(url_for("view_client", client_id=client.id))

    # Parse and save accounts in session
    pdf_path = os.path.join(app.config["UPLOAD_FOLDER"], client.pdf_filename)
    parsed_accounts = extract_negative_items_from_pdf(pdf_path)
    session["client_parsed_accounts"] = parsed_accounts
    session["parsed_accounts_client_id"] = client.id

    flash(f"✅ Found {len(parsed_accounts)} negative account(s) from the PDF.", "success")
    return redirect(url_for("view_client", client_id=client.id))

# routes/ai_agent.py or wherever your business routes are

@app.route('/toggle-workflow', methods=['POST'])
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

    return redirect(url_for('business_dashboard', client_id=client_id))


@app.template_filter('loads')
def loads_filter(s):
    return json.loads(s)


@app.route('/create-payment-intent', methods=['POST'])
@login_required
def create_payment_intent():
    data = request.get_json()
    amount = data.get('amount')
    plan = data.get('plan')

    if amount is None or plan not in ('pro', 'business'):
        return jsonify({"error": "Invalid parameters"}), 400
    
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(amount *100),
            currency='usd',
            metadata={'user_id': current_user.id, 'plan': plan}
        )
        return jsonify({"clientSecret": intent.client_secret})
    except stripe.error.StripeError as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/update-plan', methods=['POST'])
@login_required
def update_plan():
    data = request.get_json()
    plan = data.get('plan')

    if plan not in ('pro', 'business'):
        return jsonify({"error": "Invalid plan"}), 400

    current_user.plan = plan
    db.session.commit()

    return jsonify({"status": "success"})    

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        un = request.form['username']
        pw = request.form['password']
        u  = User.get_by_username(un)

        if u and u.check_password(pw):
            login_user(u)
            flash(f'Welcome back, {u.first_name}!', 'success')

            # first, honor any "next" override:
            next_page = session.pop('next', None)
            if next_page:
                return redirect(next_page)

            # then branch by plan:
            if u.plan == 'business':
                return redirect(url_for('business_dashboard'))
            else:
                # both free & Pro users go home
                return redirect(url_for('index'))

        flash('Invalid username or password', 'error')
        return redirect(url_for('login'))

    return render_template('login.html')
  

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))  

@app.route('/report-analyzer', methods=['GET', 'POST'])
@login_required
def report_analyzer():
    # If they just submitted the intake form (no file), save that and show the upload page
    if request.method == 'POST':
        upload = request.files.get('credit_report')
        if not upload or upload.filename == "":
            # Save intake fields in session
            session['intake'] = {
                'first_name': request.form['first_name'],
                'last_name': request.form['last_name'],
                'phone': request.form['phone'],
                'email': request.form['email']
            }
            # Render upload page, passing each field in as its own context var
            return render_template(
                'upload_pdf_analyzer.html',
                **session['intake']
            )

        # --- At this point 'upload' exists and you can save/process the PDF ---
        filename = secure_filename(upload.filename)
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        try:
            upload.save(path)
            if os.path.getsize(path) == 0:
                raise ValueError("Uploaded file is empty.")
        except Exception as e:
            flash(f"File upload error: {e}", "error")
            return render_template('upload_pdf_analyzer.html', **session.get('intake', {}))

        try:
            negative_items = extract_negative_items_from_pdf(path)
            session['negative_items'] = negative_items
        except Exception:
            session['negative_items'] = []     

        try:
            metrics = extract_pdf_metrics(path)
            parsed_negative_count = metrics.get("negative_count", 0)
            parsed_collections_count = metrics.get("total_collections", 0)
        except Exception as e:
            os.remove(path)
            flash("Error parsing PDF. Make sure it's a valid report.", "error")
            return render_template('upload_pdf_analyzer.html', **session.get('intake', {}))    

        try:
            base64_images = pdf_to_base64_images(path)
        except Exception as e:
            os.remove(path)
            flash("error converting PDF to images.", "error")
            return render_template('upload_pdf_analyzer.html', **session.get('intake', {}))
        finally:
            if os.path.exists(path):
                os.remove(path)

        vision_prompt = f"""
You are a senior credit analyst trained in U.S. consumer credit laws, FICO scoring models, and bank underwriting data points.

IMPORTANT:
These numbers were deterministically parsed and MUST be used exactly:
- Negative Accounts: {parsed_negative_count}
- Collection Accounts: {parsed_collections_count}

TASK:
1. Summarize FICO, utilization %, total debt, negative & collection counts, avg/oldest age.
2. Classify as "Needs Repair", "Thin Profile", or "Funding Ready".
3. Provide 3–4 recommendations.
4. List 3–5 score factors.
5. Scan the report images and identify:
    Inaccurate Reporting: any account whose payment-history buckets do not progress correctly (e.g., 30, 30, 60 instead of 30, 60, 90). For each, record its account_name and account_number.
    Incomplete Information: any account grid missing required fields (e.g., missing monthly payment, missing account type, etc.). For each, record its account_name, account_number, and list of missing fields.

OUTPUT ONLY valid JSON:
{{
  "summary": "...",
  "status": "...",
  "recommendations": [...],
  "score_factors": [...],
  "inaccurate_accounts": [
    {{
      "account_name": "...",
      "account_number": "...",
      "issue": "payment buckets [30,30,60] do not progress"
    }},
    // …
  ],
  "incomplete_accounts": [
    {{
      "account_name": "...",
      "account_number": "...",
      "missing_fields": ["monthly payment"]
    }},
    // …
  ],
  "numeric_fields": {{
    "credit_score": int|null,
    "utilization":  int|null,
    "total_debt":   int|null,
    "total_collections": {parsed_collections_count},
    "negative_accounts":  {parsed_negative_count},
    "average_age_years": "...",
    "oldest_account_years": "..."
  }}
}}
"""
        # 5) call Vision model
        vision_inputs = [
            {"type":"image_url","image_url":{"url":img,"detail":"high"}}
            for img in base64_images
        ] + [{"type":"text","text":vision_prompt}]

        try:
            resp = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role":"user","content": vision_inputs}],
                temperature=0.3
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```json"):
                raw = raw.replace("```json","").replace("```","").strip()
            analysis = json.loads(raw)
        except Exception as e:
            flash("AI error: failed to parse JSON. Try another report.", "error")
            return render_template('upload_pdf_analyzer.html', **session.get('intake', {}))

        # 6) enforce numeric consistency
        num = analysis.get("numeric_fields", {})
        analysis["num_collections"] = parsed_collections_count
        analysis["negative_count"] = parsed_negative_count
        analysis["fico_score"] = num.get("credit_score", "N/A")
        analysis["utilization"] = num.get("utilization", 0)
        analysis["total_debt"] = num.get("total_debt", 0)
        analysis["average_credit_age"] = num.get("average_age_years", "N/A")
        analysis["oldest_account_age"] = num.get("oldest_account_years", "N/A")
        analysis["summary_text"] = analysis.get("summary", "")
        analysis["status_text"] = analysis.get("status", "")
        analysis["recommendations"] = analysis.get("recommendations", [])

        # 7) render results, passing both analysis + intake fields
        intake = session.get('intake', {})
        return render_template(
            'analysis_results.html',
            user_name = f"{intake.get('first_name','')} {intake.get('last_name','')}".strip(),
            **analysis,
            **intake
        )

    # GET: clear any stale intake & show the intake form
    session.pop('intake', None)
    return render_template('report_analyzer.html')    


@app.route('/funding-sequencer')
@login_required
def funding_sequencer():
    return render_template('funding_sequencer.html')

@app.route('/dispute-folder')
@login_required
def dispute_folder():
    logs = DailyLogEntry.query.filter_by(user_id=current_user.id).order_by(DailyLogEntry.timestamp.desc()).all()
    letters = MailedLetter.query.filter_by(user_id=current_user.id).order_by(MailedLetter.created_at.desc()).all()
    docs = Correspondence.query.filter_by(user_id=current_user.id).order_by(Correspondence.uploaded_at.desc()).all()
    return render_template('dispute_folder.html', logs=logs, letters=letters, docs=docs)




@app.route('/add-log', methods=['GET', 'POST'])
@login_required
def add_log():
    if request.method == 'POST':
        # 1️⃣ collect & validate form data
        title   = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        if not title or not content:
            flash('Please fill out both title and content', 'error')
            return redirect(url_for('add_log'))

        # 2️⃣ save to DB (you’ll need to import and define DailyLogEntry in models.py)
        entry = DailyLogEntry(
            user_id=current_user.id,
            title=title,
            content=content
        )
        db.session.add(entry)
        db.session.commit()

        flash('✅ Logged your entry!', 'success')
        return redirect(url_for('daily_log'))  # or wherever you show the full log

    # GET → show the form
    return render_template('add_log.html')

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

@app.route('/add-letter', methods=['GET', 'POST'])
@login_required
def add_letter():
    if request.method == 'POST':
        recipient = request.form['recipient'].strip()
        account_number = request.form['account_number'].strip()
        letter_text = request.form['letter_text'].strip()

        if not (recipient and account_number and letter_text):
            flash("All fields are required.", "error")
            return redirect(url_for('add_letter'))

        new = MailedLetter(
            user_id = current_user.id,
            recipient = recipient,
            account_number = account_number,
            letter_text = letter_text
        )
        db.session.add(new)
        db.session.commit()
        flash("✅ Mailed letter recorded.", "success")
        return redirect(url_for('dispute_folder'))

    return render_template('add_letter.html')

@app.route('/upload-doc', methods=['GET', 'POST'])
@login_required
def upload_doc():
    if request.method == 'POST':
        # 1) Validate upload
        file = request.files.get('file')
        if not file or file.filename == '':
            flash("Please choose a file to upload.", "error")
            return redirect(url_for('upload_doc'))

        # 2) Save to disk
        filename = secure_filename(file.filename)
        upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        filepath = os.path.join(upload_folder, filename)
        file.save(filepath)

        # 3) Record in DB
        doc = Correspondence(
            user_id = current_user.id,
            filename = filename,
            file_url = filepath,
            description = request.form.get('description', '').strip()
        )
        db.session.add(doc)
        db.session.commit()

        flash("✅ Document uploaded.", "success")
        return redirect(url_for('dispute_folder'))

    # GET → show upload form
    return render_template('upload_doc.html')




if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)

