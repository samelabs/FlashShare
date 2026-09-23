"""Single money/denomination authority (S3).

Two asset worlds meet at exactly one boundary — Tool settlement:

  Consumer Spendable Credits  — consumer-side unit: tool usage, grants,
      packages, membership, consume/refund. NOT touched here.
  USD-millis                  — provider/platform cash unit:
      1000 USD-millis = $1.00, 1 USD-milli = $0.001.

Tool settlement conversion authority (the ONLY place credits convert
to cash): 1000 Tool Credits = $1.00, i.e. 1 Tool Credit = 1 USD-milli
at settlement. Integer math only — no float ever enters the money
path. All provider-facing amounts (provider_earn, platform_commission,
platform_revenue, withdrawals.amount, the earn pool) are USD-millis.
"""

MILLIS_PER_USD = 1000          # 1 USD = 1000 USD-millis
TOOL_CREDITS_PER_USD = 1000    # 1000 Tool Credits = $1.00 at settlement
CENT_MILLIS = 10               # 1 cent = 10 USD-millis


def strict_int(value) -> int:
    """Money-path type gate: the value must be a real int. bool is NOT a
    valid money integer; float/str/None fail closed — no coercion, so a
    float like 10.999999 can never be truncated into an amount path."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_amount_type")
    return value


def credits_to_millis(credits: int) -> int:
    """Tool settlement boundary: discrete consumer Credits → USD-millis
    cash. Integer-only; the single conversion point in the codebase."""
    credits = strict_int(credits)
    if credits < 0:
        raise ValueError("negative amount")
    return credits * MILLIS_PER_USD // TOOL_CREDITS_PER_USD


def millis_to_usd_str(millis: int) -> str:
    """USD-millis → exact display string, e.g. 10250 → '$10.25',
    10000 → '$10', 25500 → '$25.50'. Cents always shown for withdrawal
    amounts; whole dollars stay clean. Integer math only. Historical
    facts render losslessly — sub-cent millis keep the exact third
    decimal (10001 → '$10.001')."""
    millis = strict_int(millis)
    sign = "-" if millis < 0 else ""
    millis = abs(millis)
    dollars, rem = divmod(millis, MILLIS_PER_USD)
    cents = rem // CENT_MILLIS
    sub_cent = rem % CENT_MILLIS
    if sub_cent:
        # exact milli precision (display-only path; never a money authority)
        return f"{sign}${dollars:,}.{rem:03d}"
    if cents:
        return f"{sign}${dollars:,}.{cents:02d}"
    return f"{sign}${dollars:,}"


def millis_to_usd_cents_str(millis: int) -> str:
    """USD-millis → USD string with fixed two decimals ('10000' → '$10.00').
    Same authority as millis_to_usd_str; ONLY for values that are
    contractually cent-aligned (e.g. the withdrawal minimum). Fails
    closed on non-cent-aligned input (millis % 10 != 0) so a historical
    sub-cent fact can never be silently truncated to two decimals."""
    millis = strict_int(millis)
    if abs(millis) % CENT_MILLIS:
        raise ValueError("sub_cent_amount")
    sign = "-" if millis < 0 else ""
    millis = abs(millis)
    dollars, rem = divmod(millis, MILLIS_PER_USD)
    cents = rem // CENT_MILLIS
    return f"{sign}${dollars:,}.{cents:02d}"


def parse_usd_decimal(text) -> int:
    """Exact USD decimal STRING → USD-millis. Whole-cent only.

    Strict string-only input: any non-str type (number, bool, None,
    list, dict) is rejected outright — no str() coercion, so a JSON
    number 10.25 can never masquerade as a valid amount.

    '10.25' → 10250; '10' → 10000; '10.2' → 10200;
    '10.2500' → 10250 (trailing zeros fine);
    '10.001' → ValueError('sub_cent_amount') (rejected, never rounded);
    malformed / '-5' / '' / exponent text → ValueError('invalid_usd_amount').
    No float() anywhere.
    """
    if not isinstance(text, str):
        raise ValueError("invalid_usd_amount")
    s = text.strip()
    if not s or s.startswith("-") or s.startswith("+"):
        raise ValueError("invalid_usd_amount")
    if "." in s:
        whole, _, frac = s.partition(".")
        if frac.count(".") or not frac:
            raise ValueError("invalid_usd_amount")
    else:
        whole, frac = s, ""
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise ValueError("invalid_usd_amount")
    if len(frac) > 2 and frac[2:].strip("0"):
        raise ValueError("sub_cent_amount")   # non-zero sub-cent: reject
    frac = frac[:2].ljust(2, "0")             # pad/truncate at cent
    millis = int(whole) * MILLIS_PER_USD + int(frac) * CENT_MILLIS
    if millis <= 0:
        raise ValueError("invalid_usd_amount")
    return millis
