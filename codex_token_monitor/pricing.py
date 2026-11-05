"""Current public API token-price equivalents, never subscription billing.

Sources checked 2026-11-05: https://developers.openai.com/api/docs/pricing
and https://developers.openai.com/api/docs/models/<model> for each entry below.
Cache-write accounting: https://developers.openai.com/api/docs/guides/prompt-caching
Prices are USD per million tokens, Standard processing, non-regional endpoints.
"""
from __future__ import annotations

from decimal import Decimal
import re

PRICE_DATE = '2026-11-05'
PRICE_SOURCE = 'https://developers.openai.com/api/docs/pricing'
# ordinary input, cached read, output. Cache writes use 1.25x / 2.5x input.
RATES = {
    'gpt-6.1-sol': ('2', '.10', '10'),
    'gpt-6.1-terra': ('1', '.05', '5'),
    'gpt-6-astra': ('10', '1', '50'),
    'gpt-6-sol': ('2', '.20', '10'),
    'gpt-6-luna': ('.10', '.01', '.50'),
    'gpt-5.6-sol': ('4', '.40', '20'),  # promotional rate through November 30
    'gpt-5.6-terra': ('2', '.20', '12'),
    'gpt-5.6-luna': ('.20', '.02', '1.20'),
    'gpt-5.5': ('5', '.50', '30'),
}
COMPONENTS = ('input', 'cached_input', 'cache_write', 'output')
BASIS = 'Standard API token equivalent; current rates, not a bill or internal resource cost'
EXCLUSIONS = 'Separate web/image/file/container/tool fees, service-tier and regional adjustments are not included.'


def unavailable(reason):
    return {'usd': None, 'known_usd': '0', 'complete': False, 'calls': 0,
            'unpriced_calls': 1, 'long_context_calls': 0,
            'components_usd': {k: '0' for k in COMPONENTS}, 'notes': [reason],
            'rate_date': PRICE_DATE, 'source': PRICE_SOURCE, 'basis': BASIS,
            'exclusions': EXCLUSIONS}


def price_call(model, value):
    """Price one model request; apply the long-context tier before aggregation."""
    name = model if isinstance(model, str) else ''
    if name not in RATES:
        # Only dated snapshots of an explicitly verified model share its rate.
        name = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', name)
    if name not in RATES:
        return unavailable(f'No verified API price for {model or "unknown model"}.')
    try:
        details = value.get('input_tokens_details') or {}
        ins = int(value['input_tokens'])
        outs = int(value['output_tokens'])
        cached = int(value.get('cached_input_tokens', details.get('cached_tokens', 0)))
        writes = int(value.get('cache_write_input_tokens', details.get('cache_write_tokens', 0)))
        lifetimes = details.get('cache_write_tokens_details') or {}
        write30 = int(lifetimes.get('ttl_30m_tokens', 0))
        write12 = int(lifetimes.get('ttl_12h_tokens', 0))
        if min(ins, outs, cached, writes, write30, write12) < 0 or cached + writes > ins:
            return unavailable('Invalid/overlapping token counters; cost unavailable.')
        if 'total_tokens' in value and int(value['total_tokens']) != ins + outs:
            return unavailable('Token total disagrees with input + output; cost unavailable.')
        if write30 + write12 > writes:
            return unavailable('Cache-write lifetime counters exceed recorded writes.')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return unavailable('Incomplete token counters; cost unavailable.')
    if writes and name == 'gpt-5.5':
        return unavailable('Cache-write price for GPT-5.5 is not verified.')
    notes = []
    if writes > write30 + write12:
        notes.append('Unspecified cache-write lifetime priced at the default 30-minute rate.')
        write30 += writes - write30 - write12
    ordinary = ins - cached - writes
    inp, cache, out = map(Decimal, RATES[name])
    long = ins > 272000
    if long:
        inp *= 2
        cache *= 2
        out *= Decimal('1.5')
    million = Decimal(1000000)
    parts = {
        'input': ordinary * inp / million,
        'cached_input': cached * cache / million,
        'cache_write': (write30 * Decimal('1.25') + write12 * Decimal('2.5')) * inp / million,
        'output': outs * out / million,
    }
    total = str(sum(parts.values(), Decimal(0)))
    return {'usd': total, 'known_usd': total, 'complete': True, 'calls': 1,
            'unpriced_calls': 0, 'long_context_calls': int(long),
            'components_usd': {k: str(v) for k, v in parts.items()}, 'notes': notes,
            'rate_date': PRICE_DATE, 'source': PRICE_SOURCE, 'basis': BASIS,
            'exclusions': EXCLUSIONS}


def combine_costs(costs):
    """Keep known subtotals visible without labeling partial data as a total."""
    costs = list(costs)
    parts = {k: sum((Decimal(c['components_usd'][k]) for c in costs), Decimal(0))
             for k in COMPONENTS}
    known = sum(parts.values(), Decimal(0))
    complete = all(c['complete'] for c in costs)
    return {'usd': str(known) if complete else None, 'known_usd': str(known),
            'complete': complete, 'calls': sum(c['calls'] for c in costs),
            'unpriced_calls': sum(c['unpriced_calls'] for c in costs),
            'long_context_calls': sum(c['long_context_calls'] for c in costs),
            'components_usd': {k: str(v) for k, v in parts.items()},
            'notes': list(dict.fromkeys(n for c in costs for n in c['notes'])),
            'models': list(dict.fromkeys(m for c in costs for m in c.get('models', []))),
            'rate_date': PRICE_DATE, 'source': PRICE_SOURCE, 'basis': BASIS,
            'exclusions': EXCLUSIONS}
