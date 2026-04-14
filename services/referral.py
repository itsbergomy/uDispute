"""
Referral System — Stripe Connect Express + Recurring Commissions

Flow:
1. User signs up → gets a unique referral_code (username uppercase)
2. User shares code/link
3. New user signs up using that code → referred_by_user_id is set
4. New user pays first month → 30-day clawback period starts
5. After 30 days of active subscription, referrer starts earning 15%
   of each subsequent monthly payment
6. Referrer enables Stripe Connect Express → linked Stripe account
7. Monthly batch job or webhook pays out accumulated commissions

Commission: 15% of the referred user's monthly subscription, recurring
for as long as they stay subscribed.
"""

import os
import re
import logging
import secrets
import stripe
from datetime import datetime, timedelta

from models import db, User, ReferralCommission
from config import Config

stripe.api_key = os.getenv('STRIPE_TEST_SECRET_KEY') or os.getenv('STRIPE_SECRET_KEY')

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.15  # 15%
CLAWBACK_DAYS = 30
PLAN_PRICES_CENTS = {'pro': 8000, 'business': 12500}


# ═══════════════════════════════════════════════════════════
#  Referral Code Generation
# ═══════════════════════════════════════════════════════════

def generate_referral_code(username):
    """
    Generate a unique referral code from username (uppercase).
    If collision, append random 4-char suffix.
    """
    base = re.sub(r'[^A-Z0-9]', '', username.upper())[:16] or 'USER'

    # Check if base is available
    existing = User.query.filter_by(referral_code=base).first()
    if not existing:
        return base

    # Collision — append random suffix
    for _ in range(10):
        suffix = secrets.token_hex(2).upper()
        candidate = f"{base}{suffix}"
        if not User.query.filter_by(referral_code=candidate).first():
            return candidate

    # Extreme fallback
    return f"{base}{secrets.token_hex(4).upper()}"


def ensure_user_has_code(user):
    """Set a referral code on a user if they don't have one."""
    if not user.referral_code:
        user.referral_code = generate_referral_code(user.username)
        db.session.commit()
    return user.referral_code


# ═══════════════════════════════════════════════════════════
#  Referral Lookup / Assignment
# ═══════════════════════════════════════════════════════════

def find_referrer_by_code(code):
    """Look up a user by their referral code (case-insensitive)."""
    if not code:
        return None
    code = code.strip().upper()
    return User.query.filter(
        db.func.upper(User.referral_code) == code
    ).first()


def assign_referrer(new_user, code):
    """
    Set referred_by_user_id on a new user during signup.
    Returns the referrer if found, None otherwise.
    """
    if not code or not new_user:
        return None

    referrer = find_referrer_by_code(code)
    if not referrer:
        return None

    # Can't refer yourself
    if referrer.id == new_user.id:
        return None

    # Referrer must be a paid user (not beta)
    if referrer.plan not in ('pro', 'business'):
        logger.info(f"[REFERRAL] Skipping — referrer {referrer.id} is not on a paid plan")
        return None

    new_user.referred_by_user_id = referrer.id
    db.session.commit()
    logger.info(f"[REFERRAL] User {new_user.id} referred by {referrer.id} (code: {referrer.referral_code})")
    return referrer


# ═══════════════════════════════════════════════════════════
#  Commission Recording (called on invoice.paid webhook or manually)
# ═══════════════════════════════════════════════════════════

def record_commission(referred_user, stripe_invoice_id=None, amount_cents=None):
    """
    Record a commission for the referrer of a paid subscription payment.

    Rules:
    - Referred user must have a referred_by_user_id
    - Referrer must be on a paid plan
    - Referred user must have paid first month (30-day clawback cleared)
    - No duplicate commissions per invoice
    """
    if not referred_user.referred_by_user_id:
        return None

    # Check for duplicate by invoice ID
    if stripe_invoice_id:
        existing = ReferralCommission.query.filter_by(
            stripe_invoice_id=stripe_invoice_id
        ).first()
        if existing:
            return existing

    referrer = User.query.get(referred_user.referred_by_user_id)
    if not referrer or referrer.plan not in ('pro', 'business'):
        return None

    # 30-day clawback: first payment is free for commissions
    now = datetime.utcnow()
    if not referred_user.referral_paid_first_month_at:
        referred_user.referral_paid_first_month_at = now
        db.session.commit()
        logger.info(f"[REFERRAL] First payment recorded for user {referred_user.id} — clawback period starts")
        return None

    days_since_first = (now - referred_user.referral_paid_first_month_at).days
    if days_since_first < CLAWBACK_DAYS:
        logger.info(f"[REFERRAL] Skipping commission — still in {CLAWBACK_DAYS}-day clawback for user {referred_user.id}")
        return None

    # Calculate commission
    sub_amount = amount_cents or PLAN_PRICES_CENTS.get(referred_user.plan, 0)
    if sub_amount <= 0:
        return None

    commission_cents = round(sub_amount * COMMISSION_RATE)

    commission = ReferralCommission(
        referrer_id=referrer.id,
        referred_user_id=referred_user.id,
        stripe_invoice_id=stripe_invoice_id,
        plan=referred_user.plan,
        subscription_amount_cents=sub_amount,
        commission_cents=commission_cents,
        commission_rate=COMMISSION_RATE,
        status='pending',
    )
    db.session.add(commission)

    # Update referrer's pending earnings
    referrer.referral_earnings_pending = (referrer.referral_earnings_pending or 0) + (commission_cents / 100)
    db.session.commit()

    logger.info(f"[REFERRAL] Commission recorded: ${commission_cents/100:.2f} to user {referrer.id} "
                f"from user {referred_user.id}'s {referred_user.plan} payment")
    return commission


# ═══════════════════════════════════════════════════════════
#  Stripe Connect Express
# ═══════════════════════════════════════════════════════════

def create_connect_account(user):
    """Create a Stripe Connect Express account for a user."""
    if user.stripe_connect_account_id:
        return user.stripe_connect_account_id

    try:
        account = stripe.Account.create(
            type='express',
            email=user.email,
            capabilities={
                'transfers': {'requested': True},
            },
            business_type='individual',
            metadata={'user_id': str(user.id), 'username': user.username},
        )
        user.stripe_connect_account_id = account.id
        db.session.commit()
        logger.info(f"[REFERRAL] Created Stripe Connect account for user {user.id}: {account.id}")
        return account.id
    except stripe.error.StripeError as e:
        logger.error(f"[REFERRAL] Failed to create Connect account: {e}")
        return None


def create_onboarding_link(user, return_url, refresh_url):
    """
    Generate a Stripe Connect onboarding link for the user to complete KYC.
    They get redirected to Stripe, fill out their info, then come back.
    """
    account_id = create_connect_account(user)
    if not account_id:
        return None

    try:
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type='account_onboarding',
        )
        return link.url
    except stripe.error.StripeError as e:
        logger.error(f"[REFERRAL] Failed to create onboarding link: {e}")
        return None


def check_connect_status(user):
    """
    Check if a user's Stripe Connect account is ready to receive payouts.
    Returns dict with 'enabled' and 'details_submitted'.
    """
    if not user.stripe_connect_account_id:
        return {'enabled': False, 'details_submitted': False}

    try:
        account = stripe.Account.retrieve(user.stripe_connect_account_id)
        return {
            'enabled': account.charges_enabled and account.payouts_enabled,
            'details_submitted': account.details_submitted,
            'requirements': getattr(account, 'requirements', None),
        }
    except stripe.error.StripeError as e:
        logger.error(f"[REFERRAL] Failed to check Connect status: {e}")
        return {'enabled': False, 'details_submitted': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  Payout Processing
# ═══════════════════════════════════════════════════════════

def process_user_payout(user, min_threshold_cents=1000):
    """
    Pay out all pending commissions for a user via Stripe transfer.

    Args:
        user: The referrer to pay out
        min_threshold_cents: Minimum balance to trigger a payout ($10 default)

    Returns:
        dict with 'paid' bool, 'amount_cents', 'error'
    """
    if not user.stripe_connect_account_id:
        return {'paid': False, 'error': 'No Connect account'}

    pending = ReferralCommission.query.filter_by(
        referrer_id=user.id, status='pending'
    ).all()

    if not pending:
        return {'paid': False, 'error': 'No pending commissions'}

    total_cents = sum(c.commission_cents for c in pending)
    if total_cents < min_threshold_cents:
        return {'paid': False, 'error': f'Below ${min_threshold_cents/100:.0f} threshold',
                'pending_cents': total_cents}

    # Verify Connect account is ready
    status = check_connect_status(user)
    if not status.get('enabled'):
        return {'paid': False, 'error': 'Connect account not enabled'}

    try:
        transfer = stripe.Transfer.create(
            amount=total_cents,
            currency='usd',
            destination=user.stripe_connect_account_id,
            description=f'uDispute referral payout — {len(pending)} commissions',
            metadata={
                'user_id': str(user.id),
                'commission_count': str(len(pending)),
            },
        )

        # Mark all as paid
        now = datetime.utcnow()
        for c in pending:
            c.status = 'paid'
            c.paid_at = now
            c.stripe_transfer_id = transfer.id

        # Update user totals
        user.referral_earnings_pending = 0
        user.referral_earnings_paid = (user.referral_earnings_paid or 0) + (total_cents / 100)
        db.session.commit()

        logger.info(f"[REFERRAL] Paid out ${total_cents/100:.2f} to user {user.id} "
                    f"({len(pending)} commissions, transfer: {transfer.id})")
        return {'paid': True, 'amount_cents': total_cents, 'transfer_id': transfer.id}

    except stripe.error.StripeError as e:
        logger.error(f"[REFERRAL] Payout failed for user {user.id}: {e}")
        return {'paid': False, 'error': str(e)}


def process_all_payouts(min_threshold_cents=1000):
    """
    Run payouts for all eligible users. Call this from a cron or manually.
    """
    users = User.query.filter(
        User.stripe_connect_account_id.isnot(None),
        User.referral_earnings_pending > 0
    ).all()

    results = []
    for user in users:
        result = process_user_payout(user, min_threshold_cents)
        results.append({
            'user_id': user.id,
            'username': user.username,
            **result,
        })

    return results


# ═══════════════════════════════════════════════════════════
#  Stats for Settings page
# ═══════════════════════════════════════════════════════════

def get_referral_stats(user):
    """Get referral stats for display in Settings."""
    referred_count = User.query.filter_by(referred_by_user_id=user.id).count()
    active_count = User.query.filter(
        User.referred_by_user_id == user.id,
        User.plan.in_(['pro', 'business'])
    ).count()

    recent_commissions = ReferralCommission.query.filter_by(
        referrer_id=user.id
    ).order_by(ReferralCommission.created_at.desc()).limit(10).all()

    return {
        'code': user.referral_code,
        'referred_count': referred_count,
        'active_count': active_count,
        'earnings_pending': round(user.referral_earnings_pending or 0, 2),
        'earnings_paid': round(user.referral_earnings_paid or 0, 2),
        'commission_rate_pct': int(COMMISSION_RATE * 100),
        'connect_account_id': user.stripe_connect_account_id,
        'recent_commissions': [{
            'amount': round(c.commission_cents / 100, 2),
            'plan': c.plan,
            'status': c.status,
            'created_at': c.created_at.strftime('%b %d, %Y'),
        } for c in recent_commissions],
    }
