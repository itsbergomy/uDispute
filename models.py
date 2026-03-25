from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime
from sqlalchemy.orm import backref
import json
import secrets


db = SQLAlchemy()

class User(db.Model, UserMixin):
    __tablename__ = 'Users'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    plan = db.Column(db.String(20), default='free')
    last_round_time = db.Column(db.DateTime, nullable=True)
    manual_accounts_used = db.Column(db.Integer, default=0)
    manual_reset_time = db.Column(db.DateTime, nullable=True)
    round_type = db.Column(db.String(10), default='auto')



    def check_password(self, password):
        return check_password_hash(self.password, password)

    @staticmethod
    def get_by_username(u):
        return User.query.filter_by(username=u).first()

class DisputeRound(db.Model):
    __tablename__ = 'dispute_rounds'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
    pdf_hash = db.Column(db.String(64), nullable=False)
    round_number = db.Column(db.Integer, default=1)
    disputed_accounts_json = db.Column(db.Text, default='[]')

    def get_disputed_accounts(self):
        try:
            return json.loads(self.disputed_accounts_json)
        except Exception:
            return []

    def set_disputed_accounts(self, accounts):
        self.disputed_accounts_json = json.dumps(accounts)

class DailyLogEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    description =db.Column(db.Text, nullable=False)

class MailedLetter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    letter_text = db.Column(db.Text, nullable=False)
    pdf_url = db.Column(db.String, nullable=True)
    bureau = db.Column(db.String(50), nullable=True)        # Equifax, TransUnion, Experian
    round_number = db.Column(db.Integer, default=1)
    account_name = db.Column(db.String(200), nullable=True)  # e.g. "Capital One #4821"
    account_number = db.Column(db.String(100), nullable=True)
    tier = db.Column(db.String(20), nullable=True)           # 'notice' (Tier 1) or 'inaccuracy' (Tier 2)

    # Response tracking
    outcome = db.Column(db.String(20), default='pending')    # pending, removed, updated, verified, stall, no_response
    response_file_url = db.Column(db.String, nullable=True)  # uploaded correspondence PDF/image
    response_text = db.Column(db.Text, nullable=True)        # extracted or pasted response text
    response_received_at = db.Column(db.DateTime, nullable=True)
    legal_research_json = db.Column(db.Text, nullable=True)  # cached CFPB + case law results

    # DocuPost mailing
    docupost_letter_id = db.Column(db.String(100), nullable=True)
    docupost_cost = db.Column(db.Float, nullable=True)
    delivery_status = db.Column(db.String(50), nullable=True)  # submitted / processing / in_transit / delivered / error
    delivery_status_updated_at = db.Column(db.DateTime, nullable=True)
    tracking_number = db.Column(db.String(100), nullable=True)
    mailed_at = db.Column(db.DateTime, nullable=True)
    mail_class = db.Column(db.String(50), nullable=True)
    service_level = db.Column(db.String(50), nullable=True)

    # Round chaining
    previous_letter_id = db.Column(db.Integer, db.ForeignKey('mailed_letter.id'), nullable=True)
    previous_letter = db.relationship('MailedLetter', remote_side=[id], backref='next_letters')

class Correspondence(db.Model):
     id = db.Column(db.Integer, primary_key=True)
     client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
     user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
     uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
     filename = db.Column(db.String(255), nullable=False)
     file_url = db.Column(db.String(255), nullable=False)
     description = db.Column(db.Text, nullable=True)
     round_number = db.Column(db.Integer, default=1)

class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    business_user_id = db.Column(db.Integer, db.ForeignKey('Users.id'))
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False)

    address_line1 = db.Column(db.String(200), nullable=True)
    address_line2 = db.Column(db.String(200), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(50), nullable=True)
    zip_code = db.Column(db.String(20), nullable=True)


    id_filename = db.Column(db.String(200), nullable=True)
    ssn_filename = db.Column(db.String(200), nullable=True)
    utility_filename = db.Column(db.String(200), nullable=True)
    pdf_filename = db.Column(db.String(200), nullable=True)
    round_status = db.Column(db.String(50), default='Not Started')
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    business_user = db.relationship('User', backref='clients', foreign_keys=[business_user_id])
    analyses = db.relationship(
        'ClientReportAnalysis',
        backref=backref('client', lazy='joined'),
        order_by='ClientReportAnalysis.created_at.desc()',
        cascade='all, delete-orphan',
    )

class ClientReportAnalysis(db.Model):
    __tablename__ = 'client_report_analysis'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    analysis_json = db.Column(db.Text, nullable=False)


class ClientDisputeLetter(db.Model):
    __tablename__ = 'client_dispute_letters'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    letter_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default='Draft')  # Draft / Approved / Sent
    template_name = db.Column(db.String(150), nullable=True)
    pdf_url = db.Column(db.String(500), nullable=True)
    round_number = db.Column(db.Integer, default=1)
    mail_class = db.Column(db.String(50), default='usps_first_class')
    service_level = db.Column(db.String(50), nullable=True)
    # DocuPost tracking
    docupost_letter_id = db.Column(db.String(100), nullable=True)
    docupost_cost = db.Column(db.Float, nullable=True)
    delivery_status = db.Column(db.String(50), nullable=True)  # queued / processing / in_transit / delivered / error
    delivery_status_updated_at = db.Column(db.DateTime, nullable=True)
    tracking_number = db.Column(db.String(100), nullable=True)
    mailed_at = db.Column(db.DateTime, nullable=True)
    client = db.relationship('Client', backref='letters')

class WorkflowSetting(db.Model):
    __tablename__ = 'workflow_settings'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    key = db.Column(db.String(50), nullable=False)
    enabled = db.Column(db.Boolean, default=False)
    business_user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)

    client = db.relationship('Client', backref='workflow_settings')
    business_user = db.relationship("User", backref="workflow_settings")

class CustomLetter(db.Model):
    __tablename__ = "custom_letters"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("Users.id"), nullable=False)
    name       = db.Column(db.String(100), nullable=False)
    subject    = db.Column(db.String(200), nullable=True)
    body       = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="custom_letters")


# ─── Pipeline Models (Autonomous Dispute System) ───

class DisputePipeline(db.Model):
    """Central state machine for each client's autonomous dispute cycle."""
    __tablename__ = 'dispute_pipelines'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)

    # State machine
    state = db.Column(db.String(30), default='intake')
    # States: intake -> analysis -> strategy -> generation -> review ->
    #         delivery -> awaiting_response -> response_received ->
    #         completed | failed

    round_number = db.Column(db.Integer, default=1)
    pdf_hash = db.Column(db.String(64), nullable=True)

    # Strategy decisions stored as JSON
    strategy_json = db.Column(db.Text, default='{}')

    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = db.relationship('Client', backref='pipelines')
    user = db.relationship('User', backref='pipelines')
    tasks = db.relationship('PipelineTask', backref='pipeline', order_by='PipelineTask.created_at')
    dispute_accounts = db.relationship('DisputeAccount', backref='pipeline', order_by='DisputeAccount.created_at')


class PipelineTask(db.Model):
    """Individual tasks within a pipeline (each step of the state machine)."""
    __tablename__ = 'pipeline_tasks'

    id = db.Column(db.Integer, primary_key=True)
    pipeline_id = db.Column(db.Integer, db.ForeignKey('dispute_pipelines.id'), nullable=False)

    task_type = db.Column(db.String(30), nullable=False)
    # Types: parse_pdf, analyze_report, pick_strategy, generate_letter,
    #        merge_package, mail_letter, file_cfpb, check_response

    state = db.Column(db.String(20), default='pending')
    # States: pending -> running -> completed | failed | skipped

    input_json = db.Column(db.Text, default='{}')
    output_json = db.Column(db.Text, default='{}')
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class DisputeAccount(db.Model):
    """Per-account tracking across rounds and bureaus."""
    __tablename__ = 'dispute_accounts'

    id = db.Column(db.Integer, primary_key=True)
    pipeline_id = db.Column(db.Integer, db.ForeignKey('dispute_pipelines.id'), nullable=False)

    account_name = db.Column(db.String(200), nullable=False)
    account_number = db.Column(db.String(100), nullable=False)
    bureau = db.Column(db.String(20), nullable=False)  # experian, transunion, equifax

    status = db.Column(db.Text, nullable=True)
    issue = db.Column(db.Text, nullable=True)
    balance = db.Column(db.String(50), nullable=True)

    # Strategy fields
    template_pack = db.Column(db.String(50), default='default')
    dispute_reason = db.Column(db.Text, nullable=True)
    escalation_level = db.Column(db.Integer, default=1)
    # 1=standard dispute, 2=consumer_law, 3=ACDV_response, 4=arbitration, 5=CFPB complaint

    # Outcome tracking
    letter_id = db.Column(db.Integer, db.ForeignKey('client_dispute_letters.id'), nullable=True)
    mailed_at = db.Column(db.DateTime, nullable=True)
    response_received_at = db.Column(db.DateTime, nullable=True)
    outcome = db.Column(db.String(30), default='pending')
    # Outcomes: pending, removed, updated, verified, no_response

    round_number = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    letter = db.relationship('ClientDisputeLetter', backref='dispute_account')


class BureauResponse(db.Model):
    """Tracks uploaded response letters from credit bureaus."""
    __tablename__ = 'bureau_responses'

    id = db.Column(db.Integer, primary_key=True)
    dispute_account_id = db.Column(db.Integer, db.ForeignKey('dispute_accounts.id'), nullable=False)

    filename = db.Column(db.String(255), nullable=False)
    response_type = db.Column(db.String(30), nullable=True)
    # Types: removed, updated, verified, stall_letter, no_response

    analysis_json = db.Column(db.Text, default='{}')
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    dispute_account = db.relationship('DisputeAccount', backref='responses')


class CreditorProfile(db.Model):
    """Cross-client creditor intelligence — aggregated dispute outcomes per creditor per business user."""
    __tablename__ = 'creditor_profiles'

    id = db.Column(db.Integer, primary_key=True)
    business_user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
    creditor_name = db.Column(db.String(200), nullable=False)  # normalized

    # Aggregated stats
    total_disputes = db.Column(db.Integer, default=0)
    removed_count = db.Column(db.Integer, default=0)
    updated_count = db.Column(db.Integer, default=0)
    verified_count = db.Column(db.Integer, default=0)
    no_response_count = db.Column(db.Integer, default=0)

    # Strategy intelligence
    avg_rounds_to_remove = db.Column(db.Float, nullable=True)
    best_pack = db.Column(db.String(50), nullable=True)  # which template_pack wins most

    # CFPB cache
    cfpb_complaint_count = db.Column(db.Integer, nullable=True)
    cfpb_win_rate = db.Column(db.Float, nullable=True)
    cfpb_last_checked = db.Column(db.DateTime, nullable=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('business_user_id', 'creditor_name', name='uq_creditor_per_user'),
    )
    user = db.relationship('User', backref='creditor_profiles')


class MessageThread(db.Model):
    __tablename__ = 'message_threads'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship('Message', backref='thread', cascade='all, delete-orphan', order_by='Message.created_at')


class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('message_threads.id'), nullable=False)
    from_business = db.Column(db.Boolean, nullable=False)  # True if business -> client
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─── Beta Features: BYOK, Supporting Docs, Client Portal ───

class UserSetting(db.Model):
    """Encrypted key-value settings per user (BYOK API keys, preferences)."""
    __tablename__ = 'user_settings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
    key = db.Column(db.String(50), nullable=False)
    value = db.Column(db.Text, nullable=False)  # encrypted for sensitive keys
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'key', name='uq_user_setting'),)
    user = db.relationship('User', backref='settings')


class SupportingDoc(db.Model):
    """Supporting documents attached to specific dispute accounts."""
    __tablename__ = 'supporting_docs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('Users.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    dispute_account_id = db.Column(db.Integer, db.ForeignKey('dispute_accounts.id'), nullable=True)
    round_number = db.Column(db.Integer, default=1)
    filename = db.Column(db.String(255), nullable=False)
    file_url = db.Column(db.String(255), nullable=False)
    doc_type = db.Column(db.String(50), nullable=True)  # bill, prior_response, identity, correspondence, other
    description = db.Column(db.Text, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    include_in_package = db.Column(db.Boolean, default=True)  # merge into mailed PDF

    user = db.relationship('User', backref='supporting_docs')
    client = db.relationship('Client', backref='supporting_docs')
    dispute_account = db.relationship('DisputeAccount', backref='supporting_docs')


class ClientPortalToken(db.Model):
    """Unique public link for clients to view their dispute status (no login)."""
    __tablename__ = 'client_portal_tokens'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False, unique=True)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)  # null = never expires
    is_active = db.Column(db.Boolean, default=True)

    client = db.relationship('Client', backref=backref('portal_token', uselist=False))

    @staticmethod
    def generate_token():
        return secrets.token_urlsafe(32)
