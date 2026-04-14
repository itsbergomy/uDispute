"""
Cross-Bureau Credit Report Comparison Engine

Matches accounts across Experian, TransUnion, and Equifax reports
and detects discrepancies that strengthen dispute letters.

Usage:
    from services.cross_reference import cross_reference

    bureau_results = {
        "experian": [parsed_accounts_from_experian],
        "transunion": [parsed_accounts_from_transunion],
        "equifax": [parsed_accounts_from_equifax],
    }
    merged_items, summary = cross_reference(bureau_results)
"""

import re
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  Account Number Normalization
# ═══════════════════════════════════════════════════════════

def normalize_account_number(raw):
    """
    Strip masks and extract trailing digits for matching.
    '****1234' → '1234', 'XXXX-XXXX-1234' → '1234', '' → ''
    """
    if not raw:
        return ''
    digits = re.sub(r'[^0-9]', '', str(raw))
    # Return last 4-8 digits (most bureaus mask everything but the last 4)
    return digits[-8:] if len(digits) > 8 else digits


def normalize_creditor_name(name):
    """
    Normalize creditor name for fuzzy matching.
    'CAPITAL ONE, N.A.' → 'CAPITAL ONE'
    """
    if not name:
        return ''
    n = name.upper().strip()
    # Remove common suffixes
    for suffix in [', N.A.', ' N.A.', ', LLC', ' LLC', ', INC', ' INC',
                   ', CORP', ' CORP', ' CO', ', CO', ' BANK', ' FINANCIAL',
                   ' SERVICES', ' CREDIT', ' AUTO']:
        n = n.replace(suffix, '')
    # Remove punctuation and extra whitespace
    n = re.sub(r'[^A-Z0-9\s]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def name_similarity(a, b):
    """Simple token overlap similarity (0.0 to 1.0)."""
    if not a or not b:
        return 0.0
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = tokens_a & tokens_b
    return len(overlap) / max(len(tokens_a), len(tokens_b))


# ═══════════════════════════════════════════════════════════
#  Account Matching Across Bureaus
# ═══════════════════════════════════════════════════════════

def match_accounts_across_bureaus(bureau_results):
    """
    Match accounts across bureaus by account number + creditor name.

    Args:
        bureau_results: {"experian": [accounts], "transunion": [accounts], ...}

    Returns:
        matched_groups: list of lists, each inner list = [(bureau, account), ...]
                        for the same underlying tradeline
        orphans: list of (bureau, account) tuples that couldn't be matched
    """
    # Index all accounts by normalized trailing digits
    by_number = defaultdict(list)
    all_entries = []

    for bureau, accounts in bureau_results.items():
        for acct in accounts:
            norm_num = normalize_account_number(acct.get('account_number', ''))
            norm_name = normalize_creditor_name(acct.get('account_name', ''))
            entry = (bureau, acct, norm_num, norm_name)
            all_entries.append(entry)
            if norm_num:
                by_number[norm_num].append(entry)

    matched_groups = []
    matched_ids = set()  # Track which entries have been grouped

    # Pass 1: Match by account number (trailing digits)
    for num, entries in by_number.items():
        if len(entries) < 2:
            continue

        # Group entries that share this number AND have similar names
        groups_for_num = []
        for entry in entries:
            eid = id(entry)
            if eid in matched_ids:
                continue

            found_group = None
            for group in groups_for_num:
                # Check name similarity against any member of the group
                for _, _, _, gname in group:
                    if name_similarity(entry[3], gname) > 0.5:
                        found_group = group
                        break
                if found_group:
                    break

            if found_group:
                found_group.append(entry)
                matched_ids.add(eid)
            else:
                new_group = [entry]
                groups_for_num.append(new_group)
                matched_ids.add(eid)

        for group in groups_for_num:
            if len(group) >= 2:
                matched_groups.append([(b, a) for b, a, _, _ in group])

    # Pass 2: Name-only matching for unmatched accounts (high threshold)
    unmatched = [e for e in all_entries if id(e) not in matched_ids]
    name_index = defaultdict(list)
    for entry in unmatched:
        name_index[entry[3]].append(entry)

    for name, entries in name_index.items():
        if len(entries) >= 2 and name:
            # Ensure entries are from different bureaus
            bureaus_in_group = set(e[0] for e in entries)
            if len(bureaus_in_group) >= 2:
                matched_groups.append([(b, a) for b, a, _, _ in entries])
                for e in entries:
                    matched_ids.add(id(e))

    # Orphans: accounts that appear on only one bureau
    orphans = [(b, a) for b, a, _, _ in all_entries if id((b, a, _, _)) not in matched_ids]
    # Rebuild orphan check properly
    orphans = []
    for entry in all_entries:
        if id(entry) not in matched_ids:
            orphans.append((entry[0], entry[1]))

    return matched_groups, orphans


# ═══════════════════════════════════════════════════════════
#  Discrepancy Detection
# ═══════════════════════════════════════════════════════════

def _parse_balance(balance_str):
    """Parse a balance string to float. '$4,521' → 4521.0"""
    if not balance_str:
        return None
    cleaned = re.sub(r'[^\d.]', '', str(balance_str))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def detect_discrepancies(matched_group):
    """
    Compare a matched group of accounts across bureaus and return
    discrepancy findings as plain strings.

    Args:
        matched_group: [(bureau_name, account_dict), ...]

    Returns:
        list of discrepancy strings prefixed with [CROSS-BUREAU]
    """
    if len(matched_group) < 2:
        return []

    findings = []
    bureaus = [b for b, _ in matched_group]
    accounts = [a for _, a in matched_group]
    account_name = accounts[0].get('account_name', 'Unknown')

    # 1. Balance mismatch
    balances = {}
    for bureau, acct in matched_group:
        bal = _parse_balance(acct.get('balance'))
        if bal is not None:
            balances[bureau] = bal
    if len(balances) >= 2:
        vals = list(balances.values())
        if max(vals) != min(vals):
            parts = [f"{b} reports ${v:,.0f}" for b, v in balances.items()]
            findings.append(
                f"[CROSS-BUREAU] Balance discrepancy for {account_name}: "
                f"{' while '.join(parts)} — inconsistent balance reporting across "
                f"bureaus constitutes inaccurate reporting under 15 U.S.C. "
                f"§ 1681s-2(a)(1)(A)"
            )

    # 2. Status conflict
    statuses = {}
    for bureau, acct in matched_group:
        status = (acct.get('status') or '').strip().lower()
        if status:
            statuses[bureau] = status
    if len(statuses) >= 2:
        unique = set(statuses.values())
        if len(unique) > 1:
            parts = [f"{b} reports '{v}'" for b, v in statuses.items()]
            findings.append(
                f"[CROSS-BUREAU] Status conflict for {account_name}: "
                f"{' while '.join(parts)} — contradictory status reporting across "
                f"bureaus demonstrates at least one CRA is furnishing inaccurate data"
            )

    # 3. Date opened mismatch
    dates = {}
    for bureau, acct in matched_group:
        d = acct.get('date_opened', '')
        if d:
            dates[bureau] = d
    if len(dates) >= 2:
        unique = set(dates.values())
        if len(unique) > 1:
            parts = [f"{b} reports '{v}'" for b, v in dates.items()]
            findings.append(
                f"[CROSS-BUREAU] Date opened mismatch for {account_name}: "
                f"{' while '.join(parts)} — inconsistent date reporting affects "
                f"credit age calculations and violates accuracy requirements "
                f"under 15 U.S.C. § 1681e(b)"
            )

    # 4. Account type mismatch
    types = {}
    for bureau, acct in matched_group:
        t = (acct.get('account_type') or '').strip().lower()
        if t:
            types[bureau] = t
    if len(types) >= 2:
        unique = set(types.values())
        if len(unique) > 1:
            parts = [f"{b} reports '{v}'" for b, v in types.items()]
            findings.append(
                f"[CROSS-BUREAU] Account type mismatch for {account_name}: "
                f"{' while '.join(parts)} — inconsistent account classification "
                f"across bureaus under 15 U.S.C. § 1681e(b)"
            )

    # 5. Payment history conflict
    for i, (b1, a1) in enumerate(matched_group):
        for b2, a2 in matched_group[i+1:]:
            lines1 = a1.get('raw_payment_lines', [])
            lines2 = a2.get('raw_payment_lines', [])
            if lines1 and lines2:
                text1 = ' '.join(str(l) for l in lines1).lower()
                text2 = ' '.join(str(l) for l in lines2).lower()
                # Check if one has late markers and the other doesn't
                late_markers = ['30', '60', '90', '120', 'co', 'charge']
                has_late_1 = any(m in text1 for m in late_markers)
                has_late_2 = any(m in text2 for m in late_markers)
                if has_late_1 != has_late_2:
                    late_bureau = b1 if has_late_1 else b2
                    clean_bureau = b2 if has_late_1 else b1
                    findings.append(
                        f"[CROSS-BUREAU] Payment history conflict for {account_name}: "
                        f"{late_bureau} reports delinquent payment history while "
                        f"{clean_bureau} shows no late payments — contradictory "
                        f"payment data across bureaus"
                    )
                    break  # One finding per group is enough

    return findings


# ═══════════════════════════════════════════════════════════
#  Main Entry Point
# ═══════════════════════════════════════════════════════════

def cross_reference(bureau_results):
    """
    Compare parsed accounts across bureaus, detect discrepancies,
    and inject findings into each account's inaccuracies list.

    Args:
        bureau_results: {"experian": [accounts], "transunion": [accounts], ...}
                        Only include bureaus that were actually parsed.

    Returns:
        (merged_items, summary):
            merged_items: flat list of all accounts with cross-ref inaccuracies injected
                          and `source_bureau` field added to each
            summary: dict with metadata about the cross-referencing
    """
    # Filter to bureaus that actually have data
    active = {b: accts for b, accts in bureau_results.items() if accts}

    if len(active) <= 1:
        # Single report — no cross-referencing possible, return as-is
        items = []
        for bureau, accts in active.items():
            for acct in accts:
                acct['source_bureau'] = bureau
                acct['bureaus'] = [bureau]
                acct['bureau_data'] = {bureau: {
                    k: acct.get(k, '') for k in ['account_name', 'account_number', 'balance',
                                                   'status', 'account_type', 'date_opened',
                                                   'original_creditor', 'comments', 'raw_payment_lines',
                                                   'inaccuracies']
                }}
                items.append(acct)
        return items, {'bureaus_parsed': list(active.keys()), 'matched_groups': 0,
                       'discrepancies_found': 0, 'single_report': True}

    logger.info(f"[CROSS-REF] Cross-referencing {len(active)} bureau reports: {list(active.keys())}")

    # Match accounts across bureaus
    matched_groups, orphans = match_accounts_across_bureaus(active)

    total_discrepancies = 0

    # Detect discrepancies in matched groups and inject findings
    for group in matched_groups:
        findings = detect_discrepancies(group)
        total_discrepancies += len(findings)

        # Inject findings into EVERY account in the group
        for bureau, acct in group:
            if 'inaccuracies' not in acct:
                acct['inaccuracies'] = []
            acct['inaccuracies'].extend(findings)

    # Detect orphan accounts (selective reporting)
    all_bureaus = set(active.keys())
    for bureau, acct in orphans:
        missing_from = all_bureaus - {bureau}
        if missing_from:
            account_name = acct.get('account_name', 'Unknown')
            finding = (
                f"[CROSS-BUREAU] {account_name} appears on {bureau.title()} but is "
                f"NOT reported on {', '.join(b.title() for b in missing_from)} — "
                f"selective/inconsistent reporting across bureaus suggests this "
                f"account may not be verifiable and should be investigated under "
                f"15 U.S.C. § 1681i(a)"
            )
            if 'inaccuracies' not in acct:
                acct['inaccuracies'] = []
            acct['inaccuracies'].append(finding)
            total_discrepancies += 1

    # Build DEDUPLICATED merged list — 1 entry per account, not per bureau
    merged_items = []

    for group in matched_groups:
        # Use the first bureau's data as the primary entry
        first_bureau, first_acct = group[0]
        bureaus = [b for b, _ in group]

        # Build bureau_data dict with each bureau's specific account data
        bureau_data = {}
        for bureau, acct in group:
            bureau_data[bureau] = {
                'account_name': acct.get('account_name', ''),
                'account_number': acct.get('account_number', ''),
                'balance': acct.get('balance', ''),
                'status': acct.get('status', ''),
                'account_type': acct.get('account_type', ''),
                'date_opened': acct.get('date_opened', ''),
                'original_creditor': acct.get('original_creditor', ''),
                'comments': acct.get('comments', ''),
                'raw_payment_lines': acct.get('raw_payment_lines', []),
                'inaccuracies': acct.get('inaccuracies', []),
            }

        # Collect ALL inaccuracies (per-bureau + cross-ref) into one list
        all_inaccuracies = []
        seen_inac = set()
        for bureau, acct in group:
            for inac in acct.get('inaccuracies', []):
                if inac not in seen_inac:
                    all_inaccuracies.append(inac)
                    seen_inac.add(inac)

        merged_items.append({
            'account_name': first_acct.get('account_name', ''),
            'account_number': first_acct.get('account_number', ''),
            'balance': first_acct.get('balance', ''),
            'status': first_acct.get('status', ''),
            'account_type': first_acct.get('account_type', ''),
            'date_opened': first_acct.get('date_opened', ''),
            'original_creditor': first_acct.get('original_creditor', ''),
            'issue': first_acct.get('issue', ''),
            'comments': first_acct.get('comments', ''),
            'raw_payment_lines': first_acct.get('raw_payment_lines', []),
            'inaccuracies': all_inaccuracies,
            'bureaus': bureaus,
            'bureau_data': bureau_data,
            'source_bureau': first_bureau,
        })

    for bureau, acct in orphans:
        acct['source_bureau'] = bureau
        acct['bureaus'] = [bureau]
        acct['bureau_data'] = {
            bureau: {
                'account_name': acct.get('account_name', ''),
                'account_number': acct.get('account_number', ''),
                'balance': acct.get('balance', ''),
                'status': acct.get('status', ''),
                'account_type': acct.get('account_type', ''),
                'date_opened': acct.get('date_opened', ''),
                'original_creditor': acct.get('original_creditor', ''),
                'comments': acct.get('comments', ''),
                'raw_payment_lines': acct.get('raw_payment_lines', []),
                'inaccuracies': acct.get('inaccuracies', []),
            }
        }
        merged_items.append(acct)

    summary = {
        'bureaus_parsed': list(active.keys()),
        'matched_groups': len(matched_groups),
        'orphan_accounts': len(orphans),
        'discrepancies_found': total_discrepancies,
        'total_accounts': len(merged_items),
        'single_report': False,
    }

    logger.info(f"[CROSS-REF] Done: {len(matched_groups)} matched groups, "
                f"{len(orphans)} orphans, {total_discrepancies} discrepancies, "
                f"{len(merged_items)} deduplicated accounts")

    return merged_items, summary
