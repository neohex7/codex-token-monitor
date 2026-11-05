from decimal import Decimal
import unittest

from codex_token_monitor.pricing import price_call, combine_costs


class PricingTests(unittest.TestCase):
    def test_cached_input_and_reasoning_are_subsets_not_extra_charges(self):
        cost = price_call('gpt-6.1-sol', {
            'input_tokens': 100000, 'cached_input_tokens': 80000,
            'output_tokens': 2000, 'reasoning_output_tokens': 1500,
            'total_tokens': 102000})
        # 20k * $2 + 80k * $.10 + 2k * $10, all per million.
        self.assertEqual(Decimal(cost['usd']), Decimal('.068'))
        self.assertEqual(Decimal(cost['components_usd']['output']), Decimal('.02'))

    def test_price_tier_is_per_call_not_per_user_turn(self):
        small = price_call('gpt-6.1-sol', {'input_tokens': 200000,
            'cached_input_tokens': 100000, 'output_tokens': 1000})
        long = price_call('gpt-6.1-sol', {'input_tokens': 300000,
            'cached_input_tokens': 200000, 'output_tokens': 1000})
        self.assertEqual(Decimal(combine_costs([small, small])['usd']), Decimal('.44'))
        self.assertEqual(Decimal(long['usd']), Decimal('.455'))
        self.assertEqual(combine_costs([small, long])['long_context_calls'], 1)
        boundary = price_call('gpt-6.1-sol', {'input_tokens': 272000,
            'cached_input_tokens': 0, 'output_tokens': 0})
        self.assertEqual(Decimal(boundary['usd']), Decimal('.544'))

    def test_both_cache_write_lifetimes_replace_ordinary_input(self):
        cost = price_call('gpt-6.1-sol', {'input_tokens': 12000,
            'input_tokens_details': {'cached_tokens': 4000, 'cache_write_tokens': 7000,
                'cache_write_tokens_details': {'ttl_30m_tokens': 3000, 'ttl_12h_tokens': 4000}},
            'output_tokens': 100})
        # 1k*$2 + 4k*$.10 + 3k*$2.50 + 4k*$5 + 100*$10.
        self.assertEqual(Decimal(cost['usd']), Decimal('.0309'))
        self.assertEqual(cost['notes'], [])

    def test_unreported_lifetime_is_an_explicit_default_assumption(self):
        cost = price_call('gpt-6-luna', {'input_tokens': 100000,
            'cached_input_tokens': 50000, 'cache_write_input_tokens': 20000,
            'output_tokens': 0})
        self.assertEqual(Decimal(cost['usd']), Decimal('.006'))
        self.assertIn('30-minute', cost['notes'][0])

    def test_mixed_models_sum_at_their_own_prices(self):
        value = {'input_tokens': 100000, 'cached_input_tokens': 0, 'output_tokens': 10000}
        sol = price_call('gpt-6.1-sol', value)
        luna = price_call('gpt-6-luna', value)
        self.assertEqual(Decimal(combine_costs([sol, luna])['usd']), Decimal('.315'))

    def test_unknown_model_is_unpriced_not_silently_sol_or_zero(self):
        unknown = price_call('future-sol', {'input_tokens': 100, 'output_tokens': 10})
        known = price_call('gpt-6.1-sol', {'input_tokens': 100, 'output_tokens': 10})
        combined = combine_costs([known, unknown])
        self.assertIsNone(combined['usd'])
        self.assertEqual(Decimal(combined['known_usd']), Decimal('.0003'))
        self.assertFalse(combined['complete'])

    def test_inconsistent_counters_are_not_billed(self):
        for value in [
            {'input_tokens': 100, 'cached_input_tokens': 110, 'output_tokens': 10},
            {'input_tokens': 100, 'cached_input_tokens': 60, 'cache_write_input_tokens': 50, 'output_tokens': 10},
            {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 999},
        ]:
            with self.subTest(value=value):
                self.assertIsNone(price_call('gpt-6.1-sol', value)['usd'])


if __name__ == '__main__':
    unittest.main()
