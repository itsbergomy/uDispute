"""
Business Rules Engine — configurable automation rules for Business plan users.
Evaluates user-defined rules against dispute events and executes actions.
"""

import re
import json
import logging
from datetime import datetime

from models import db, BusinessRule, DisputeAccount, DisputePipeline

logger = logging.getLogger(__name__)


def evaluate_rules(user_id, trigger, context):
    """
    Find and execute matching rules for a given trigger event.

    Args:
        user_id: Business user ID
        trigger: Event type (response_received, round_completed, etc.)
        context: Dict with event data:
            - account_name, account_number, bureau (for response_received)
            - outcome (verified, removed, etc.)
            - round_number
            - pipeline_id
            - creditor_name (normalized)

    Returns:
        List of executed actions
    """
    rules = BusinessRule.query.filter_by(
        user_id=user_id,
        trigger=trigger,
        enabled=True,
    ).all()

    if not rules:
        return []

    executed = []
    for rule in rules:
        try:
            if _check_conditions(rule, context):
                result = _execute_action(rule, context)
                if result:
                    executed.append({
                        'rule_name': rule.name,
                        'action': rule.action,
                        'result': result,
                    })
                    logger.info(f"[RULES] Executed rule '{rule.name}' (trigger: {trigger})")
        except Exception as e:
            logger.warning(f"[RULES] Rule '{rule.name}' failed: {e}")

    return executed


def _check_conditions(rule, context):
    """Evaluate rule conditions against the event context."""
    try:
        conditions = json.loads(rule.conditions_json or '{}')
    except (json.JSONDecodeError, TypeError):
        return True  # No conditions = always match

    if not conditions:
        return True

    # Check outcome match
    if 'outcome' in conditions:
        expected = conditions['outcome']
        actual = context.get('outcome', '')
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False

    # Check creditor pattern (glob-style matching)
    if 'creditor_pattern' in conditions:
        pattern = conditions['creditor_pattern'].replace('*', '.*')
        creditor = context.get('account_name', '') or context.get('creditor_name', '')
        if not re.search(pattern, creditor, re.IGNORECASE):
            return False

    # Check round number conditions
    if 'round_gte' in conditions:
        if (context.get('round_number', 1) or 1) < conditions['round_gte']:
            return False

    if 'round_lte' in conditions:
        if (context.get('round_number', 1) or 1) > conditions['round_lte']:
            return False

    if 'round_eq' in conditions:
        if (context.get('round_number', 1) or 1) != conditions['round_eq']:
            return False

    # Check bureau
    if 'bureau' in conditions:
        if context.get('bureau', '').lower() != conditions['bureau'].lower():
            return False

    return True


def _execute_action(rule, context):
    """Execute the action defined by the rule."""
    try:
        action_config = json.loads(rule.action_config_json or '{}')
    except (json.JSONDecodeError, TypeError):
        action_config = {}

    pipeline_id = context.get('pipeline_id')

    if rule.action == 'auto_escalate':
        return _action_auto_escalate(pipeline_id, action_config, context)
    elif rule.action == 'pause_pipeline':
        return _action_pause_pipeline(pipeline_id)
    elif rule.action == 'send_to_creditor':
        return _action_send_to_creditor(pipeline_id, action_config, context)
    elif rule.action == 'file_cfpb':
        return _action_file_cfpb(context)
    else:
        logger.warning(f"[RULES] Unknown action: {rule.action}")
        return None


def _action_auto_escalate(pipeline_id, config, context):
    """Auto-start the next round with a specified pack."""
    if not pipeline_id:
        return {'error': 'No pipeline_id in context'}

    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline:
        return {'error': 'Pipeline not found'}

    # Only escalate from round_review state
    if pipeline.state != 'round_review':
        return {'skipped': True, 'reason': f'Pipeline not in round_review state (current: {pipeline.state})'}

    pack = config.get('pack', 'consumer_law')
    round_packs = json.loads(pipeline.strategy_json or '{}').get('round_packs', [])

    # Set the pack for the next round
    next_round = pipeline.round_number + 1
    while len(round_packs) < next_round:
        round_packs.append('default')
    round_packs[next_round - 1] = pack

    strategy_data = json.loads(pipeline.strategy_json or '{}')
    strategy_data['round_packs'] = round_packs
    pipeline.strategy_json = json.dumps(strategy_data)
    pipeline.round_number = next_round
    pipeline.state = 'strategy'
    pipeline.updated_at = datetime.utcnow()
    db.session.commit()

    # Advance pipeline in background
    try:
        from blueprints.pipeline_api import _advance
        _advance(pipeline.id)
    except Exception as e:
        logger.warning(f"[RULES] Auto-escalate advance failed: {e}")

    return {'escalated': True, 'pack': pack, 'round': next_round}


def _action_pause_pipeline(pipeline_id):
    """Force pipeline to round_review state (hard pause)."""
    if not pipeline_id:
        return {'error': 'No pipeline_id'}

    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline:
        return {'error': 'Pipeline not found'}

    pipeline.state = 'round_review'
    pipeline.updated_at = datetime.utcnow()
    db.session.commit()

    return {'paused': True}


def _action_send_to_creditor(pipeline_id, config, context):
    """Flag that the next round should also send to the creditor (dual letter)."""
    if not pipeline_id:
        return {'error': 'No pipeline_id'}

    pipeline = DisputePipeline.query.get(pipeline_id)
    if not pipeline:
        return {'error': 'Pipeline not found'}

    strategy_data = json.loads(pipeline.strategy_json or '{}')
    agent_config = strategy_data.get('agent_config', {})
    agent_config['send_to'] = 'both'
    agent_config['strategy'] = 'dual'
    strategy_data['agent_config'] = agent_config
    pipeline.strategy_json = json.dumps(strategy_data)
    db.session.commit()

    return {'dual_letter_enabled': True}


def _action_file_cfpb(context):
    """Placeholder — flag for CFPB complaint filing (future integration)."""
    return {
        'cfpb_flagged': True,
        'account_name': context.get('account_name'),
        'note': 'CFPB auto-filing will be available in a future update. Account has been flagged for manual complaint.',
    }


# ═══════════════════════════════════════════════════════════
#  Preset Rules — common configurations
# ═══════════════════════════════════════════════════════════

PRESET_RULES = [
    {
        'name': 'Auto-escalate verified accounts',
        'trigger': 'round_completed',
        'conditions': {'outcome': ['verified', 'no_response']},
        'action': 'auto_escalate',
        'action_config': {'pack': 'consumer_law'},
    },
    {
        'name': 'Pause on stall letters',
        'trigger': 'response_received',
        'conditions': {'outcome': 'stall'},
        'action': 'pause_pipeline',
        'action_config': {},
    },
    {
        'name': 'Send to creditor after Round 2 verification',
        'trigger': 'response_received',
        'conditions': {'outcome': 'verified', 'round_gte': 2},
        'action': 'send_to_creditor',
        'action_config': {},
    },
]


def create_preset_rules(user_id):
    """Create default preset rules for a new business user."""
    for preset in PRESET_RULES:
        rule = BusinessRule(
            user_id=user_id,
            name=preset['name'],
            trigger=preset['trigger'],
            conditions_json=json.dumps(preset['conditions']),
            action=preset['action'],
            action_config_json=json.dumps(preset['action_config']),
            enabled=False,  # Disabled by default — user must opt in
        )
        db.session.add(rule)
    db.session.commit()
