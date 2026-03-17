"""
Authentication blueprint — login, logout, signup, payment.
Extracted from dispute_ui.py.
"""

import os
import stripe
from flask import Blueprint, request, jsonify, render_template, flash, redirect, url_for, session
from flask_login import login_required, current_user
from dotenv import load_dotenv

from models import User, db, login_user, logout_user, generate_password_hash

load_dotenv()

stripe.api_key = os.getenv("STRIPE_TEST_SECRET_KEY")
STRIPE_TEST_PUBLISHABLE_KEY = os.getenv("STRIPE_TEST_PUBLISHABLE_KEY")

auth_bp = Blueprint('auth', __name__)

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
def signup():
    if request.method == 'POST':
        fn = request.form['first_name'].strip()
        ln = request.form['last_name'].strip()
        un = request.form['username'].strip()
        em = request.form['email'].strip().lower()
        pw = request.form['password']
        beta_code = request.form.get('beta_code', '').strip().upper()

        if beta_code not in BETA_CODES:
            flash('Invalid beta invite code.', 'error')
            return redirect(url_for('auth.signup'))

        if User.get_by_username(un):
            flash('Username already taken', 'error')
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

        login_user(new_user)
        flash("Welcome! You're on our Free plan.", 'success')
        return redirect(url_for('disputes.index'))

    return render_template('register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        un = request.form['username']
        pw = request.form['password']
        u = User.get_by_username(un)

        if u and u.check_password(pw):
            login_user(u)
            flash(f'Welcome back, {u.first_name}!', 'success')

            next_page = session.pop('next', None)
            if next_page:
                return redirect(next_page)

            if u.plan == 'business':
                return redirect(url_for('business.business_dashboard'))
            else:
                return redirect(url_for('disputes.index'))

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
    amount = data.get('amount')
    plan = data.get('plan')

    if amount is None or plan not in ('pro', 'business'):
        return jsonify({"error": "Invalid parameters"}), 400

    try:
        intent = stripe.PaymentIntent.create(
            amount=int(amount * 100),
            currency='usd',
            metadata={'user_id': current_user.id, 'plan': plan}
        )
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
    if plan not in ('free', 'pro', 'business'):
        flash('Invalid plan.', 'error')
        return redirect(url_for('disputes.index'))
    current_user.plan = plan
    db.session.commit()
    flash(f'Switched to {plan} plan.', 'success')
    return redirect(request.referrer or url_for('disputes.index'))
