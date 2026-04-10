"""
Application configuration and factory.
Centralizes all config and provides create_app() for use by task workers and blueprints.
"""

import os
import secrets
import tempfile
from datetime import timedelta
from flask import Flask
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_mail import Mail
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv

load_dotenv()

# ── Security: rate limiter (imported here so blueprints can use it) ──
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address, default_limits=[])
except ImportError:
    limiter = None


class Config:
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB upload limit

    # Use PostgreSQL on Render (DATABASE_URL), SQLite locally
    _db_url = os.environ.get('DATABASE_URL', '')
    # Render gives postgres:// but SQLAlchemy needs postgresql://
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

    SQLALCHEMY_DATABASE_URI = _db_url or 'sqlite:///dispute.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SQLite needs special options; PostgreSQL doesn't
    if not _db_url:
        SQLALCHEMY_ENGINE_OPTIONS = {
            'connect_args': {'check_same_thread': False},
            'pool_pre_ping': True,
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            'pool_pre_ping': True,
            'pool_recycle': 120,  # Aggressive recycle — Render kills idle connections
            'pool_size': 10,
            'max_overflow': 5,
        }

    # ── Security: SECRET_KEY ──
    # Require SECRET_KEY in production; generate a random one for local dev only
    _secret = os.getenv('SECRET_KEY')
    if _secret:
        SECRET_KEY = _secret
    elif _db_url:
        # Production (has DATABASE_URL) but no SECRET_KEY — refuse to start
        raise RuntimeError(
            "SECRET_KEY environment variable is required in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    else:
        # Local dev only — generate ephemeral key (sessions won't persist across restarts)
        SECRET_KEY = secrets.token_hex(32)

    # ── Security: session settings ──
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=60)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    # Only set Secure flag when not on localhost
    SESSION_COOKIE_SECURE = bool(_db_url)  # True in production (PostgreSQL), False locally

    # Mail
    MAIL_SERVER = 'smtp.gmail.com'
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.getenv('MAIL_USERNAME')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD')


mail = Mail()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'

# ── Security audit logger ──
import logging
audit_logger = logging.getLogger('security.audit')
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter('%(asctime)s [AUDIT] %(message)s'))
if not audit_logger.handlers:
    audit_logger.addHandler(_handler)
    audit_logger.setLevel(logging.INFO)


def create_app():
    """Application factory."""
    app = Flask(__name__)
    app.config.from_object(Config)

    # Make sessions permanent so PERMANENT_SESSION_LIFETIME applies
    @app.before_request
    def _make_session_permanent():
        from flask import session
        session.permanent = True

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.environ['UPLOAD_FOLDER'] = app.config['UPLOAD_FOLDER']

    from models import db
    db.init_app(app)
    Migrate(app, db)
    csrf.init_app(app)
    if limiter:
        limiter.init_app(app)

    # ── Security headers ──
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if app.config.get('SESSION_COOKIE_SECURE'):
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    # Enable WAL mode for SQLite so background threads can read/write concurrently
    from sqlalchemy import event
    with app.app_context():
        if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
            @event.listens_for(db.engine, 'connect')
            def _set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute('PRAGMA journal_mode=WAL')
                cursor.close()

        # Auto-add new columns to existing SQLite tables (lightweight migration)
        db.create_all()
        with db.engine.connect() as conn:
            from sqlalchemy import text, inspect
            inspector = inspect(db.engine)
            # ── client_dispute_letters ──
            if 'client_dispute_letters' in inspector.get_table_names():
                existing = [c['name'] for c in inspector.get_columns('client_dispute_letters')]
                new_cols = {
                    'docupost_letter_id': 'VARCHAR(100)',
                    'docupost_cost': 'FLOAT',
                    'delivery_status': 'VARCHAR(50)',
                    'mailed_at': 'DATETIME',
                    'pdf_url': 'VARCHAR(500)',
                    'round_number': 'INTEGER DEFAULT 1',
                    'mail_class': "VARCHAR(50) DEFAULT 'usps_first_class'",
                    'service_level': 'VARCHAR(50)',
                    'delivery_status_updated_at': 'DATETIME',
                    'tracking_number': 'VARCHAR(100)',
                    'quality_score': 'INTEGER',
                    'quality_warnings': 'TEXT',
                }
                for col_name, col_type in new_cols.items():
                    if col_name not in existing:
                        conn.execute(text(f'ALTER TABLE client_dispute_letters ADD COLUMN {col_name} {col_type}'))
                conn.commit()

            # ── mailed_letter ──
            if 'mailed_letter' in inspector.get_table_names():
                existing = [c['name'] for c in inspector.get_columns('mailed_letter')]
                new_cols = {
                    'account_number': 'VARCHAR(100)',
                    'outcome': "VARCHAR(20) DEFAULT 'pending'",
                    'response_file_url': 'VARCHAR(500)',
                    'response_text': 'TEXT',
                    'response_received_at': 'TIMESTAMP',
                    'legal_research_json': 'TEXT',
                    'previous_letter_id': 'INTEGER REFERENCES mailed_letter(id)',
                    'tier': 'VARCHAR(20)',
                    'docupost_letter_id': 'VARCHAR(100)',
                    'docupost_cost': 'FLOAT',
                    'delivery_status': 'VARCHAR(50)',
                    'delivery_status_updated_at': 'TIMESTAMP',
                    'tracking_number': 'VARCHAR(100)',
                    'mailed_at': 'TIMESTAMP',
                    'mail_class': 'VARCHAR(50)',
                    'service_level': 'VARCHAR(50)',
                }
                for col_name, col_type in new_cols.items():
                    if col_name not in existing:
                        conn.execute(text(f'ALTER TABLE mailed_letter ADD COLUMN {col_name} {col_type}'))
                # Backfill NULL outcomes to 'pending'
                conn.execute(text("UPDATE mailed_letter SET outcome = 'pending' WHERE outcome IS NULL"))
                conn.commit()

            # ── dispute_accounts: widen status/issue columns ──
            if 'dispute_accounts' in inspector.get_table_names():
                da_cols = {c['name']: c for c in inspector.get_columns('dispute_accounts')}
                if 'status' in da_cols:
                    col_type = str(da_cols['status'].get('type', ''))
                    if 'VARCHAR' in col_type.upper() or 'CHAR' in col_type.upper():
                        try:
                            conn.execute(text('ALTER TABLE dispute_accounts ALTER COLUMN status TYPE TEXT'))
                            conn.commit()
                        except Exception:
                            conn.rollback()
                if 'issue' in da_cols:
                    col_type = str(da_cols['issue'].get('type', ''))
                    if 'VARCHAR' in col_type.upper() or 'CHAR' in col_type.upper():
                        try:
                            conn.execute(text('ALTER TABLE dispute_accounts ALTER COLUMN issue TYPE TEXT'))
                            conn.commit()
                        except Exception:
                            conn.rollback()

            # ── correspondence ──
            if 'correspondence' in inspector.get_table_names():
                existing = [c['name'] for c in inspector.get_columns('correspondence')]
                if 'round_number' not in existing:
                    conn.execute(text('ALTER TABLE correspondence ADD COLUMN round_number INTEGER DEFAULT 1'))
                    conn.commit()

            # ── Users (is_beta, stripe fields) ──
            if 'Users' in inspector.get_table_names():
                existing = [c['name'] for c in inspector.get_columns('Users')]
                user_new_cols = {
                    'is_beta': 'BOOLEAN DEFAULT FALSE',
                    'stripe_customer_id': 'VARCHAR(100)',
                    'stripe_subscription_id': 'VARCHAR(100)',
                }
                for col_name, col_type in user_new_cols.items():
                    if col_name not in existing:
                        conn.execute(text(f'ALTER TABLE "Users" ADD COLUMN {col_name} {col_type}'))
                conn.commit()

    mail.init_app(app)
    login_manager.init_app(app)

    from models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register blueprints
    from blueprints.auth import auth_bp
    from blueprints.disputes import disputes_bp
    from blueprints.business import business_bp
    from blueprints.pipeline_api import pipeline_bp
    from blueprints.portal import portal_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(disputes_bp)
    app.register_blueprint(business_bp)
    app.register_blueprint(pipeline_bp, url_prefix='/api')
    app.register_blueprint(portal_bp)

    # Template filter
    import json
    @app.template_filter('loads')
    def loads_filter(s):
        return json.loads(s)

    # Backward-compatible endpoint aliases so existing templates
    # using url_for('index') etc. still work without blueprint prefix
    _aliases = {
        # disputes blueprint
        'index': 'disputes.index',
        'upload_pdf': 'disputes.upload_pdf',
        'select_account': 'disputes.select_account',
        'confirm_account': 'disputes.confirm_account',
        'save_confirmed_account': 'disputes.save_confirmed_account',
        'select_entity': 'disputes.select_entity',
        'handle_entity': 'disputes.handle_entity',
        'define_details': 'disputes.define_details',
        'choose_template': 'disputes.choose_template',
        'prompt_packs': 'disputes.prompt_packs',
        'generate_letter_screen': 'disputes.generate_letter_screen',
        'generate_process': 'disputes.generate_process',
        'final_review': 'disputes.final_review',
        'manual_mode': 'disputes.manual_mode',
        'mail_letter': 'disputes.mail_letter',
        'convert_pdf': 'disputes.convert_pdf',
        'confirm_next_round': 'disputes.confirm_next_round',
        'dispute_folder': 'disputes.dispute_folder',
        'add_log': 'disputes.add_log',
        'add_letter': 'disputes.add_letter',
        'upload_doc': 'disputes.upload_doc',
        'report_analyzer': 'disputes.report_analyzer',
        'funding_sequencer': 'disputes.funding_sequencer',
        # auth blueprint
        'login': 'auth.login',
        'logout': 'auth.logout',
        'signup': 'auth.signup',
        'join_pro': 'auth.join_pro',
        'join_business': 'auth.join_business',
        'create_payment_intent': 'auth.create_payment_intent',
        'update_plan': 'auth.update_plan',
        # business blueprint
        'business_dashboard': 'business.business_dashboard',
        'create_client': 'business.create_client',
        'view_client': 'business.view_client',
        'edit_client': 'business.edit_client',
        'client_file': 'business.client_file',
        'upload_correspondence': 'business.upload_correspondence',
        'view_correspondence_file': 'business.view_correspondence_file',
        'run_analysis_for_client': 'business.run_analysis_for_client',
        'messages_thread': 'business.messages_thread',
        'update_recommendations': 'business.update_recommendations',
        'send_analysis_email_route': 'business.send_analysis_email_route',
        'mail_analysis_to_client': 'business.mail_analysis_to_client',
        'run_udispute_flow': 'business.run_udispute_flow',
        'finalize_udispute_letter': 'business.finalize_udispute_letter',
        'extract_for_udispute': 'business.extract_for_udispute',
        # Legacy aliases (backward compat)
        'run_disputegpt_flow': 'business.run_udispute_flow',
        'finalize_disputegpt_letter': 'business.finalize_udispute_letter',
        'extract_for_disputegpt': 'business.extract_for_udispute',
        'toggle_workflow': 'business.toggle_workflow',
        'list_custom_letters': 'business.list_custom_letters',
        'new_custom_letter': 'business.new_custom_letter',
        'edit_custom_letter': 'business.edit_custom_letter',
        'delete_custom_letter': 'business.delete_custom_letter',
    }

    from flask import url_for as _original_url_for
    @app.url_build_error_handlers.append
    def _handle_url_build_error(error, endpoint, values):
        """Redirect old endpoint names to blueprint-prefixed versions."""
        if endpoint in _aliases:
            return _original_url_for(_aliases[endpoint], **values)
        raise error

    return app
