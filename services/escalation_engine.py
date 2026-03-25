"""
Smart Escalation Engine — uses creditor intelligence to pick optimal dispute strategy.
Falls back to the hardcoded escalation ladder when no intelligence data exists.
"""

import logging
from services.creditor_intelligence import get_creditor_recommendation, normalize_creditor_name

logger = logging.getLogger(__name__)

# Default escalation ladder (fallback when no creditor intelligence exists)
DEFAULT_ESCALATION = {
    1: 'default',
    2: 'consumer_law',
    3: 'ACDV_response',
    4: 'arbitration',
    5: 'consumer_law',  # cycle back with new evidence
}


def recommend_escalation(business_user_id, account_name, current_round, outcome):
    """
    Get the best escalation strategy for a specific account based on creditor intelligence.

    Args:
        business_user_id: The business user's ID
        account_name: Raw account/creditor name
        current_round: Current round number (the round that just completed)
        outcome: The outcome of the current round (verified, no_response, stall, etc.)

    Returns:
        dict with pack, reason, confidence, source
    """
    next_round = current_round + 1

    # Try creditor intelligence first
    recommendation = get_creditor_recommendation(business_user_id, account_name)

    if recommendation and recommendation['confidence'] >= 0.3 and recommendation['total_disputes'] >= 2:
        # Intelligence-driven recommendation
        return {
            'pack': recommendation['recommended_pack'],
            'reason': recommendation['reason'],
            'confidence': recommendation['confidence'],
            'source': 'creditor_intelligence',
            'win_rate': recommendation['win_rate'],
            'avg_rounds': recommendation['avg_rounds'],
        }

    # Outcome-driven fallback (smarter than pure ladder)
    if outcome == 'no_response':
        return {
            'pack': 'ACDV_response',
            'reason': 'No response in 30+ days — FCRA violation. Demand deletion under 15 U.S.C. 1681i(a)(1).',
            'confidence': 0.8,
            'source': 'outcome_logic',
            'win_rate': None,
            'avg_rounds': None,
        }
    elif outcome == 'verified' and current_round >= 2:
        return {
            'pack': 'arbitration',
            'reason': f'Verified {current_round} times. Arbitration signals legal action — often triggers deletion.',
            'confidence': 0.7,
            'source': 'outcome_logic',
            'win_rate': None,
            'avg_rounds': None,
        }
    elif outcome == 'verified' and current_round == 1:
        return {
            'pack': 'consumer_law',
            'reason': 'Round 1 verified — escalate with FCRA/FDCPA citations and broader statutory foundation.',
            'confidence': 0.6,
            'source': 'outcome_logic',
            'win_rate': None,
            'avg_rounds': None,
        }
    elif outcome == 'stall':
        return {
            'pack': 'consumer_law',
            'reason': 'Stall letter — generic response doesn\'t constitute reasonable investigation (Cushman v. Trans Union).',
            'confidence': 0.6,
            'source': 'outcome_logic',
            'win_rate': None,
            'avg_rounds': None,
        }

    # Pure ladder fallback
    pack = DEFAULT_ESCALATION.get(next_round, 'consumer_law')
    return {
        'pack': pack,
        'reason': f'Default escalation ladder for Round {next_round}.',
        'confidence': 0.3,
        'source': 'default_ladder',
        'win_rate': None,
        'avg_rounds': None,
    }


def calculate_win_probability(business_user_id, account_name, round_number, pack):
    """
    Estimate the probability of winning (removal/update) for a given
    creditor + round + pack combination based on historical data.

    Returns float 0.0-1.0 or None if insufficient data.
    """
    recommendation = get_creditor_recommendation(business_user_id, account_name)
    if not recommendation or recommendation['total_disputes'] < 3:
        return None

    # Simple estimate based on overall win rate
    base_rate = recommendation['win_rate'] / 100.0

    # Adjust for round (earlier rounds historically win more)
    round_multiplier = {1: 1.0, 2: 0.85, 3: 0.7, 4: 0.6, 5: 0.5}.get(round_number, 0.4)

    # Adjust for pack (arbitration has higher pressure)
    pack_multiplier = {
        'default': 0.8,
        'consumer_law': 1.0,
        'ACDV_response': 1.1,
        'arbitration': 1.3,
    }.get(pack, 1.0)

    probability = min(base_rate * round_multiplier * pack_multiplier, 0.95)
    return round(probability, 2)
