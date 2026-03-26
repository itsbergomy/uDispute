"""
Creditor Intelligence — cross-client analytics for dispute strategy optimization.
Tracks win/loss rates per creditor across all clients for a business user,
enabling data-driven escalation recommendations.
"""

import re
import logging
from datetime import datetime

from models import db, CreditorProfile, DisputeAccount, DisputePipeline

logger = logging.getLogger(__name__)

# Common alias mappings for creditor name normalization
_ALIASES = {
    'CAP ONE': 'CAPITAL ONE',
    'CAPITAL ONE BANK': 'CAPITAL ONE',
    'CAPITAL ONE NA': 'CAPITAL ONE',
    'MIDLAND CREDIT': 'MIDLAND CREDIT MANAGEMENT',
    'MIDLAND CREDIT MGMT': 'MIDLAND CREDIT MANAGEMENT',
    'MCM': 'MIDLAND CREDIT MANAGEMENT',
    'PRA GROUP': 'PORTFOLIO RECOVERY ASSOCIATES',
    'PORTFOLIO RECOVERY': 'PORTFOLIO RECOVERY ASSOCIATES',
    'LVNV FUNDING': 'LVNV FUNDING LLC',
    'TRANSUNION': 'TRANS UNION',
    'EDFINANCIAL SERVICES L': 'EDFINANCIAL SERVICES',
}


def normalize_creditor_name(raw_name):
    """
    Normalize a creditor name for consistent matching.
    Strips account numbers, normalizes casing, resolves common aliases.
    """
    if not raw_name:
        return ''

    # Strip everything after # (account number suffix)
    name = re.split(r'[#]', raw_name)[0].strip()

    # Remove trailing account-like patterns (digits/X at end)
    name = re.sub(r'\s+[\dX]{4,}$', '', name).strip()

    # Uppercase for comparison
    name = name.upper().strip()

    # Remove common suffixes
    for suffix in [' LLC', ' INC', ' CORP', ' CO', ' NA', ' NATIONAL ASSOCIATION']:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()

    # Check aliases
    if name in _ALIASES:
        name = _ALIASES[name]

    return name


def update_creditor_profile(business_user_id, account_name, outcome, round_number=1, template_pack='default'):
    """
    Upsert creditor profile with new outcome data.
    Called after a dispute account receives a response.
    """
    creditor_name = normalize_creditor_name(account_name)
    if not creditor_name:
        return None

    # Find or create profile
    profile = CreditorProfile.query.filter_by(
        business_user_id=business_user_id,
        creditor_name=creditor_name,
    ).first()

    if not profile:
        profile = CreditorProfile(
            business_user_id=business_user_id,
            creditor_name=creditor_name,
        )
        db.session.add(profile)

    # Increment outcome counter (guard against None from unflushed defaults)
    profile.total_disputes = (profile.total_disputes or 0) + 1
    if outcome == 'removed':
        profile.removed_count = (profile.removed_count or 0) + 1
    elif outcome == 'updated':
        profile.updated_count = (profile.updated_count or 0) + 1
    elif outcome == 'verified':
        profile.verified_count = (profile.verified_count or 0) + 1
    elif outcome == 'no_response':
        profile.no_response_count = (profile.no_response_count or 0) + 1

    # Update avg rounds to remove (only for successful removals)
    if outcome == 'removed' and round_number:
        if profile.avg_rounds_to_remove is None:
            profile.avg_rounds_to_remove = float(round_number)
        else:
            # Running average
            total_removed = profile.removed_count
            profile.avg_rounds_to_remove = (
                (profile.avg_rounds_to_remove * (total_removed - 1) + round_number) / total_removed
            )

    # Track best pack (which pack leads to most removals)
    if outcome == 'removed' and template_pack:
        # Simple approach: store the pack that was used for the most recent removal
        # A more sophisticated approach would track pack-specific win rates
        profile.best_pack = template_pack

    profile.updated_at = datetime.utcnow()

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.warning(f"Failed to update creditor profile for {creditor_name}")

    return profile


def get_creditor_recommendation(business_user_id, account_name):
    """
    Get an escalation recommendation based on historical data for this creditor.

    Returns:
        dict with recommended_pack, win_rate, avg_rounds, total_disputes, confidence
        or None if no data exists.
    """
    creditor_name = normalize_creditor_name(account_name)
    if not creditor_name:
        return None

    profile = CreditorProfile.query.filter_by(
        business_user_id=business_user_id,
        creditor_name=creditor_name,
    ).first()

    if not profile or profile.total_disputes < 1:
        return None

    win_rate = (profile.removed_count + profile.updated_count) / profile.total_disputes * 100
    confidence = min(profile.total_disputes / 10.0, 1.0)  # 10+ disputes = full confidence

    # Determine recommended pack
    if profile.best_pack and profile.removed_count >= 2:
        recommended_pack = profile.best_pack
        reason = f"Pack '{profile.best_pack}' has led to {profile.removed_count} removals for this creditor."
    elif profile.verified_count > profile.removed_count and profile.total_disputes >= 3:
        recommended_pack = 'arbitration'
        reason = f"This creditor verifies often ({profile.verified_count} times). Arbitration applies maximum pressure."
    elif profile.no_response_count > 2:
        recommended_pack = 'ACDV_response'
        reason = f"This creditor frequently ignores disputes ({profile.no_response_count} non-responses). ACDV enforcement demands compliance."
    else:
        recommended_pack = 'consumer_law'
        reason = "Consumer Law provides broad statutory coverage for escalation."

    return {
        'recommended_pack': recommended_pack,
        'reason': reason,
        'win_rate': round(win_rate, 1),
        'avg_rounds': round(profile.avg_rounds_to_remove, 1) if profile.avg_rounds_to_remove else None,
        'total_disputes': profile.total_disputes,
        'removed_count': profile.removed_count,
        'verified_count': profile.verified_count,
        'confidence': round(confidence, 2),
    }


def rebuild_all_profiles(business_user_id):
    """
    Rebuild all creditor profiles from scratch by scanning DisputeAccount records.
    Useful for data migration or correcting drift.
    """
    # Clear existing profiles
    CreditorProfile.query.filter_by(business_user_id=business_user_id).delete()
    db.session.flush()

    # Get all pipelines for this user
    pipelines = DisputePipeline.query.filter_by(user_id=business_user_id).all()
    pipeline_ids = [p.id for p in pipelines]

    if not pipeline_ids:
        db.session.commit()
        return 0

    # Get all accounts with final outcomes
    accounts = DisputeAccount.query.filter(
        DisputeAccount.pipeline_id.in_(pipeline_ids),
        DisputeAccount.outcome != 'pending',
    ).all()

    count = 0
    for acct in accounts:
        update_creditor_profile(
            business_user_id=business_user_id,
            account_name=acct.account_name,
            outcome=acct.outcome,
            round_number=acct.round_number,
            template_pack=acct.template_pack,
        )
        count += 1

    logger.info(f"Rebuilt creditor profiles for user {business_user_id}: {count} accounts processed")
    return count
