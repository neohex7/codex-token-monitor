import unittest
from codex_token_monitor.ui import _Dashboard, _cost_breakdown
from codex_token_monitor.pricing import price_call, combine_costs, unavailable


class Screen:
    def __init__(self): self.writes=[]
    def getmaxyx(self): return 44,156
    def addstr(self,y,x,text,attr=0): self.writes.append(text)


class UITests(unittest.TestCase):
    def dashboard(self):
        screen=Screen()
        dashboard=_Dashboard(screen,None,'chat',5,2)
        dashboard.data={'selected':{'title':'Task title','tokens':{'total_tokens':100},
            'turns':[{'id':'one','prompt':'Question','status':'completed',
                      'tokens':{'total_tokens':100},'limit_deltas':['codex/primary: +1.00 pp (account-wide observed delta)']}]},
            'limits':[{'id':'codex','source':'rollout','primary':{'used_percent':30,'window_minutes':10080}}]}
        return screen,dashboard

    def test_task_view_does_not_present_account_quota_as_task_consumption(self):
        screen,dashboard=self.dashboard()
        dashboard.draw_main(3,41,0,156)
        text='\n'.join(screen.writes)
        self.assertNotIn('TASK LIMIT SHARE',text)
        self.assertNotIn('does not report quota % per task',text)
        self.assertNotIn('ACCOUNT CHANGE',text)
        self.assertNotIn('+1.00 pp',text)
        self.assertIn('TURN HISTORY',text)
        self.assertNotIn('30.00% used',text)
        self.assertNotIn('10080m',text)

    def test_optional_account_view_omits_period(self):
        screen,dashboard=self.dashboard()
        dashboard.draw_limits(12,41,0,156)
        text='\n'.join(screen.writes)
        self.assertIn('30.00% used; reset',text)
        self.assertNotIn('7 days',text)
        self.assertNotIn('10080',text)

    def test_turn_displays_model_api_price_and_output_average(self):
        screen,dashboard=self.dashboard()
        turn=dashboard.data['selected']['turns'][0]
        turn.update(model='gpt-6.1-sol',avg_tps=2.5,
            api_cost={'complete':True,'usd':'.068'},
            children_api_cost={'complete':True,'usd':'0'},
            api_cost_with_children={'complete':True,'usd':'.068'})
        dashboard.draw_main(3,41,0,156)
        text='\n'.join(screen.writes)
        self.assertIn('latest avg output 2.50/s',text)
        self.assertIn('gpt-6.1-sol',text)
        self.assertIn('$0.068000',text)
        self.assertNotIn('TOK/S total',text)
        screen.writes=[]
        dashboard.draw_detail(3,41,0,156)
        text='\n'.join(screen.writes)
        self.assertIn('average OUTPUT 2.50 tok/s',text)
        self.assertIn('Model: gpt-6.1-sol',text)

    def test_chat_and_turn_breakdown_include_agents_at_cached_rates(self):
        screen,dashboard=self.dashboard()
        root=price_call('gpt-6.1-sol', {'input_tokens':100000,
            'cached_input_tokens':80000,'output_tokens':2000})
        child=price_call('gpt-6-luna', {'input_tokens':100000,
            'cached_input_tokens':80000,'output_tokens':2000})
        combined=combine_costs([root,child])
        costs=dict(api_cost=root,children_api_cost=child,api_cost_with_children=combined)
        dashboard.data['selected'].update(costs)
        dashboard.data['selected']['turns'][0].update(costs)
        for draw in (dashboard.draw_main,dashboard.draw_detail):
            screen.writes=[]
            draw(3,41,0,156)
            text='\n'.join(screen.writes)
            self.assertIn('$0.071800',text)
            self.assertIn('uncached $0.042000 · cache read $0.008800',text)
            self.assertIn('cache write $0.000000 · output $0.021000',text)

    def test_incomplete_breakdown_does_not_claim_exact_components(self):
        cost=combine_costs([price_call('gpt-6.1-sol', {
            'input_tokens':100000,'cached_input_tokens':80000,'output_tokens':2000}),
            unavailable('Missing response')])
        text='\n'.join(_cost_breakdown(cost))
        self.assertIn('uncached >=$0.040000 · cache read >=$0.008000',text)
        self.assertIn('cache write N/A · output >=$0.020000',text)
        self.assertNotIn('$0.000000', '\n'.join(_cost_breakdown(None)))


if __name__=='__main__':unittest.main()
