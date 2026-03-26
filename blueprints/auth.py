"""
Authentication blueprint — login, logout, signup, payment.
Extracted from dispute_ui.py.
"""

import os
import stripe
from urllib.parse import urlparse, urljoin
from flask import Blueprint, request, jsonify, render_template, flash, redirect, url_for, session, abort
from flask_login import login_required, current_user
from dotenv import load_dotenv

from models import User, db, login_user, logout_user, generate_password_hash
from config import limiter, audit_logger

load_dotenv()

stripe.api_key = os.getenv("STRIPE_TEST_SECRET_KEY")
STRIPE_TEST_PUBLISHABLE_KEY = os.getenv("STRIPE_TEST_PUBLISHABLE_KEY")

auth_bp = Blueprint('auth', __name__)


def _rate_limit(rule):
    """Apply rate limit if flask-limiter is available, otherwise no-op."""
    if limiter:
        return limiter.limit(rule)
    return lambda f: f


def _is_safe_redirect(target):
    """Validate that a redirect target stays on our domain."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

# ── Beta invite codes ────────────────────────────────────
# Add codes here as plain uppercase strings. Users can type
# them in any case — input is uppercased before comparison.
BETA_CODES = {
    'UDISPUTE2026',
    'EARLYACCESS',
    'GLASSGANG',
    'LIQUIDGLASS',
    'CREDITFIX',
    'SKOOLBETA',
    'FIRSTROUND',
    'UPOWER',
    'BETAWAVE',
    'REPAIRMODE',
}


@auth_bp.route('/signup', methods=['GET', 'POST'])
@_rate_limit("5 per minute")
def signup():
    if request.method == 'POST':
        fn = request.form['first_name'].strip()
        ln = request.form['last_name'].strip()
        un = request.form['username'].strip()
        em = request.form['email'].strip().lower()
        pw = request.form['password']
        beta_code = request.form.get('beta_code', '').strip().upper()

        if beta_code not in BETA_CODES:
            audit_logger.warning(f"SIGNUP_BAD_CODE ip={request.remote_addr} code={beta_code}")
            flash('Invalid beta invite code.', 'error')
            return redirect(url_for('auth.signup'))

        if User.get_by_username(un):
            flash('Username already taken', 'error')
            return redirect(url_for('auth.signup'))

        # Password strength check
        if len(pw) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return redirect(url_for('auth.signup'))

        new_user = User(
            first_name=fn,
            last_name=ln,
            username=un,
            email=em,
            password=generate_password_hash(pw, method='pbkdf2:sha256'),
            plan='free'
        )
        db.session.add(new_user)
        db.session.commit()

        audit_logger.info(f"SIGNUP_SUCCESS user_id={new_user.id} ip={request.remote_addr}")
        login_user(new_user)
        flash("Welcome! You're on our Free plan.", 'success')
        return redirect(url_for('disputes.index'))

    return render_template('register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
@_rate_limit("10 per minute")
def login():
    if request.method == 'POST':
        un = request.form['username']
        pw = request.form['password']
        u = User.get_by_username(un)

        if u and u.check_password(pw):
            login_user(u)
            audit_logger.info(f"LOGIN_SUCCESS user_id={u.id} ip={request.remote_addr}")
            flash(f'Welcome back, {u.first_name}!', 'success')

            next_page = session.pop('next', None)
            if _is_safe_redirect(next_page):
                return redirect(next_page)

            if u.plan == 'business':
                return redirect(url_for('business.business_dashboard'))
            else:
                return redirect(url_for('disputes.index'))

        audit_logger.warning(f"LOGIN_FAILED username={un} ip={request.remote_addr}")
        flash('Invalid username or password', 'error')
        return redirect(url_for('auth.login'))

    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('disputes.index'))


@auth_bp.route('/join-pro')
@login_required
def join_pro():
    return render_template('join_pro.html', stripe_test_publishable_key=STRIPE_TEST_PUBLISHABLE_KEY)


@auth_bp.route('/join-business')
@login_required
def join_business():
    return redirect(url_for('auth.join_pro'))


@auth_bp.route('/create-payment-intent', methods=['POST'])
@login_required
def create_payment_intent():
    data = request.get_json()
    plan = data.get('plan')

    # Server-side price enforcement — never trust client-sent amounts
    PLAN_PRICES = {'pro': 8000, 'business': 12500}  # cents
    if plan not in PLAN_PRICES:
        return jsonify({"error": "Invalid plan"}), 400

    amount_cents = PLAN_PRICES[plan]

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency='usd',
            metadata={'plan': plan},
            idempotency_key=f"user-{current_user.id}-{plan}-signup"
        )
        audit_logger.info(f"PAYMENT_INTENT user_id={current_user.id} plan={plan} amount={amount_cents}")
        return jsonify({"clientSecret": intent.client_secret})
    except stripe.error.StripeError as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route('/update-plan', methods=['POST'])
@login_required
def update_plan():
    data = request.get_json()
    plan = data.get('plan')

    if plan not in ('pro', 'business'):
        return jsonify({"error": "Invalid plan"}), 400

    current_user.plan = plan
    db.session.commit()

    return jsonify({"status": "success"})


@auth_bp.route('/dev/switch-plan/<plan>')
@login_required
def dev_switch_plan(plan):
    """Dev-only: quickly toggle between free/pro/business plans."""
    if os.getenv('FLASK_ENV') != 'development':
        abort(404)  # Hide this route in production
    if plan not in ('free', 'pro', 'business'):
        flash('Invalid plan.', 'error')
        return redirect(url_for('disputes.index'))
    current_user.plan = plan
    db.session.commit()
    flash(f'Switched to {plan} plan.', 'success')
    return redirect(request.referrer or url_for('disputes.index'))
