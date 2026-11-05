import unittest
from codex_token_monitor.ui import _Dashboard


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


if __name__=='__main__':unittest.main()
