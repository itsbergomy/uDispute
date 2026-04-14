"""
Authentication blueprint — login, logout, signup, payment.
Extracted from dispute_ui.py.
"""

import os
import stripe
from datetime import datetime
from urllib.parse import urlparse, urljoin
from flask import Blueprint, request, jsonify, render_template, flash, redirect, url_for, session, abort
from flask_login import login_required, current_user
from dotenv import load_dotenv

from models import User, db, login_user, logout_user, generate_password_hash
from config import limiter, audit_logger, csrf

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
    """Beta tester signup — requires invite code. Free plan."""
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

        if len(pw) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return redirect(url_for('auth.signup'))

        new_user = User(
            first_name=fn, last_name=ln, username=un, email=em,
            password=generate_password_hash(pw, method='pbkdf2:sha256'),
            plan='free',
            is_beta=True,
        )
        db.session.add(new_user)
        db.session.commit()

        # Generate referral code + assign referrer if code provided
        from services.referral import ensure_user_has_code, assign_referrer
        ensure_user_has_code(new_user)
        ref_code = request.form.get('referral_code', '').strip() or session.pop('pending_ref_code', None)
        if ref_code:
            assign_referrer(new_user, ref_code)

        audit_logger.info(f"SIGNUP_SUCCESS user_id={new_user.id} plan=free is_beta=True ip={request.remote_addr}")
        login_user(new_user)
        flash("Welcome! You're on our Free plan.", 'success')
        return redirect(url_for('disputes.index'))

    return render_template('register.html')


@auth_bp.route('/signup/<plan>', methods=['GET', 'POST'])
@_rate_limit("5 per minute")
def signup_paid(plan):
    """Paid signup — no beta code. Plan was already paid via Stripe Checkout."""
    if plan not in ('pro', 'business'):
        flash('Invalid plan.', 'error')
        return redirect(url_for('auth.landing_page'))

    # Verify they actually paid (session flag set by checkout_success)
    if session.get('stripe_paid_plan') != plan:
        flash('Please complete payment first.', 'error')
        return redirect(url_for('auth.checkout', plan=plan))

    plan_label = 'Pro' if plan == 'pro' else 'Business'

    if request.method == 'POST':
        fn = request.form['first_name'].strip()
        ln = request.form['last_name'].strip()
        un = request.form['username'].strip()
        em = request.form['email'].strip().lower()
        pw = request.form['password']

        if User.get_by_username(un):
            flash('Username already taken', 'error')
            return redirect(url_for('auth.signup_paid', plan=plan))

        if len(pw) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return redirect(url_for('auth.signup_paid', plan=plan))

        new_user = User(
            first_name=fn, last_name=ln, username=un, email=em,
            password=generate_password_hash(pw, method='pbkdf2:sha256'),
            plan=plan,
            is_beta=False,
            stripe_customer_id=session.get('stripe_customer_id'),
            stripe_subscription_id=session.get('stripe_subscription_id'),
        )
        db.session.add(new_user)
        db.session.commit()

        # Generate referral code + assign referrer if code provided
        from services.referral import ensure_user_has_code, assign_referrer
        ensure_user_has_code(new_user)
        ref_code = request.form.get('referral_code', '').strip() or session.pop('pending_ref_code', None)
        if ref_code:
            referrer = assign_referrer(new_user, ref_code)
            if referrer:
                # Start 30-day clawback clock — first payment doesn't count for commission
                new_user.referral_paid_first_month_at = datetime.utcnow()
                db.session.commit()

        # Clear Stripe session data
        session.pop('stripe_paid_plan', None)
        session.pop('stripe_customer_id', None)
        session.pop('stripe_subscription_id', None)

        audit_logger.info(f"SIGNUP_SUCCESS user_id={new_user.id} plan={plan} ip={request.remote_addr}")
        login_user(new_user)

        if plan == 'business':
            flash(f"Welcome! You're on the {plan_label} plan.", 'success')
            return redirect(url_for('business.business_dashboard'))
        else:
            flash(f"Welcome! You're on the {plan_label} plan.", 'success')
            return redirect(url_for('disputes.index'))

    return render_template('register_paid.html', plan=plan, plan_label=plan_label)


# ── Stripe Checkout ──────────────────────────────────────

STRIPE_PRICES = {
    'pro': os.getenv('STRIPE_PRO_PRICE_ID'),
    'business': os.getenv('STRIPE_BUSINESS_PRICE_ID'),
}


@auth_bp.route('/checkout/<plan>')
def checkout(plan):
    """Create a Stripe Checkout Session and redirect to Stripe's hosted payment page."""
    # Capture referral code from query param (survives Stripe redirect via session)
    ref_code = request.args.get('ref', '').strip()
    if ref_code:
        session['pending_ref_code'] = ref_code

    if plan not in STRIPE_PRICES:
        flash('Invalid plan.', 'error')
        return redirect(url_for('auth.landing_page'))

    price_id = STRIPE_PRICES[plan]
    if not price_id:
        flash('Payment not configured yet.', 'error')
        return redirect(url_for('auth.landing_page'))

    try:
        base_url = request.host_url.rstrip('/')
        checkout_session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            success_url=base_url + url_for('auth.checkout_success', plan=plan) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=base_url + '/landing#pricing',
        )
        return redirect(checkout_session.url)
    except stripe.error.StripeError as e:
        audit_logger.error(f"STRIPE_CHECKOUT_ERROR plan={plan}: {e}")
        flash('Payment service error. Please try again.', 'error')
        return redirect(url_for('auth.landing_page'))


@auth_bp.route('/checkout/success/<plan>')
def checkout_success(plan):
    """Stripe redirects here after successful payment. Store plan in session, send to signup."""
    session_id = request.args.get('session_id')

    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if checkout_session.payment_status == 'paid':
                session['stripe_paid_plan'] = plan
                session['stripe_customer_id'] = checkout_session.customer
                session['stripe_subscription_id'] = checkout_session.subscription
                audit_logger.info(f"CHECKOUT_SUCCESS plan={plan} session={session_id}")
                flash(f'{plan.title()} plan activated! Create your account to get started.', 'success')
                return redirect(url_for('auth.signup_paid', plan=plan))
        except stripe.error.StripeError as e:
            audit_logger.error(f"STRIPE_VERIFY_ERROR: {e}")

    flash('Payment verification failed. Please try again or contact support.', 'error')
    return redirect(url_for('auth.landing_page'))


@auth_bp.route('/landing')
def landing_page():
    """Serve the landing page. Captures ?ref=CODE for referral attribution."""
    ref_code = request.args.get('ref', '').strip()
    if ref_code:
        session['pending_ref_code'] = ref_code
    return render_template('landing.html', pending_ref_code=session.get('pending_ref_code'))


# ── Pro → Business Upgrade ───────────────────────────────

@auth_bp.route('/upgrade')
@login_required
def upgrade_to_business():
    """Upgrade a Pro user to Business via Stripe Checkout ($45 first month)."""
    if current_user.plan != 'pro':
        flash('Upgrade is only available for Pro users.', 'error')
        return redirect(url_for('disputes.index'))

    upgrade_price_id = os.getenv('STRIPE_UPGRADE_PRICE_ID')
    if not upgrade_price_id:
        flash('Upgrade not configured yet.', 'error')
        return redirect(url_for('disputes.index'))

    try:
        base_url = request.host_url.rstrip('/')
        checkout_session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': upgrade_price_id, 'quantity': 1}],
            success_url=base_url + url_for('auth.upgrade_success') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=base_url + url_for('disputes.index'),
            customer=current_user.stripe_customer_id or None,
        )
        return redirect(checkout_session.url)
    except stripe.error.StripeError as e:
        audit_logger.error(f"STRIPE_UPGRADE_ERROR user={current_user.id}: {e}")
        flash('Payment service error. Please try again.', 'error')
        return redirect(url_for('disputes.index'))


@auth_bp.route('/upgrade/success')
@login_required
def upgrade_success():
    """Handle successful Pro → Business upgrade."""
    session_id = request.args.get('session_id')

    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if checkout_session.payment_status == 'paid':
                current_user.plan = 'business'
                current_user.stripe_subscription_id = checkout_session.subscription
                db.session.commit()
                audit_logger.info(f"UPGRADE_SUCCESS user={current_user.id} pro→business")
                flash("Welcome to Business Mode!", 'success')
                return redirect(url_for('business.business_dashboard'))
        except stripe.error.StripeError as e:
            audit_logger.error(f"STRIPE_UPGRADE_VERIFY_ERROR: {e}")

    flash('Upgrade verification failed. Please contact support.', 'error')
    return redirect(url_for('disputes.index'))


# ── Stripe Webhook: Record commissions on invoice.paid ──

@auth_bp.route('/stripe/webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    """
    Handle Stripe webhooks. Currently only listens for invoice.payment_succeeded
    to record referral commissions.

    Set STRIPE_WEBHOOK_SECRET in env to enable signature verification.
    """
    from services.referral import record_commission

    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            # No secret configured — accept without verification (dev only)
            event = stripe.Event.construct_from(
                stripe.util.json.loads(payload), stripe.api_key
            )
    except Exception as e:
        audit_logger.warning(f"STRIPE_WEBHOOK_VERIFY_FAIL: {e}")
        return jsonify({'error': 'Invalid webhook'}), 400

    if event['type'] == 'invoice.payment_succeeded':
        invoice = event['data']['object']
        customer_id = invoice.get('customer')
        invoice_id = invoice.get('id')
        amount_paid = invoice.get('amount_paid', 0)

        if customer_id:
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user and user.referred_by_user_id:
                record_commission(
                    referred_user=user,
                    stripe_invoice_id=invoice_id,
                    amount_cents=amount_paid,
                )

    return jsonify({'received': True}), 200


# ── Referral: Stripe Connect Onboarding ──────────────────

@auth_bp.route('/referral/connect/start', methods=['POST'])
@login_required
def referral_connect_start():
    """Start Stripe Connect Express onboarding for the current user."""
    if current_user.plan not in ('pro', 'business'):
        flash('Referral payouts are available for Pro and Business users.', 'error')
        return redirect(url_for('disputes.settings_page'))

    from services.referral import create_onboarding_link
    base_url = request.host_url.rstrip('/')
    return_url = base_url + url_for('auth.referral_connect_return')
    refresh_url = base_url + url_for('disputes.settings_page')

    link_url = create_onboarding_link(current_user, return_url, refresh_url)
    if not link_url:
        flash('Failed to start Stripe Connect onboarding. Please try again.', 'error')
        return redirect(url_for('disputes.settings_page'))

    return redirect(link_url)


@auth_bp.route('/referral/connect/return')
@login_required
def referral_connect_return():
    """User returns from Stripe Connect onboarding — verify status."""
    from services.referral import check_connect_status
    status = check_connect_status(current_user)
    if status.get('enabled'):
        flash('Your payouts are now enabled! You\'ll receive commissions monthly.', 'success')
    elif status.get('details_submitted'):
        flash('Your account is under review. Payouts will be enabled soon.', 'info')
    else:
        flash('Onboarding incomplete. Finish the Stripe setup to enable payouts.', 'warning')
    return redirect(url_for('disputes.settings_page'))


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
