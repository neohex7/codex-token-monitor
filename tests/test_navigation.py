import threading
import time
import unittest
from unittest.mock import patch
from codex_token_monitor.ui import _Dashboard


class Screen:
    def keypad(self,value):pass
    def timeout(self,value):self.wait=value


class SlowMonitor:
    def __init__(self):self.started=threading.Event();self.release=threading.Event()
    def snapshot(self,**kwargs):
        self.started.set();self.release.wait(timeout=2)
        selected={'id':kwargs['selected_id'] or 'chat','turns':[]}
        return {'selected':selected,'threads':[selected]}


class NavigationTests(unittest.TestCase):
    def test_refresh_cannot_block_enter_and_escape(self):
        monitor=SlowMonitor();dashboard=_Dashboard(Screen(),monitor,'chat',5,2)
        dashboard.data={'selected':{'id':'chat','turns':[{'id':'turn'}]}}
        try:
            before=time.monotonic();dashboard.fetch()
            self.assertLess(time.monotonic()-before,.1)
            self.assertTrue(monitor.started.wait(timeout=.5))
            before=time.monotonic()
            dashboard.key('\n');self.assertEqual(dashboard.focus,'history')
            dashboard.key('\n');self.assertTrue(dashboard.detail)
            dashboard.key('\x1b');self.assertFalse(dashboard.detail)
            dashboard.key('\x1b');self.assertEqual(dashboard.focus,'chats')
            self.assertLess(time.monotonic()-before,.1)
            self.assertIsNotNone(dashboard._worker)
        finally:
            monitor.release.set();dashboard._worker.join(timeout=1)

    def test_stale_snapshot_does_not_switch_back_to_previous_chat(self):
        monitor=SlowMonitor();dashboard=_Dashboard(Screen(),monitor,'old',5,2)
        dashboard.threads=[{'id':'old'},{'id':'new'}]
        dashboard.fetch();self.assertTrue(monitor.started.wait(timeout=.5))
        dashboard.select_chat(1)
        worker=dashboard._worker
        monitor.release.set();worker.join(timeout=1);dashboard.poll_results()
        self.assertEqual(dashboard.selected,'new')
        self.assertEqual(dashboard.data['selected']['id'],'new')
        self.assertEqual(dashboard.next_refresh,0)

    def test_bare_escape_has_short_delay_and_no_one_second_wait(self):
        screen=Screen();dashboard=_Dashboard(screen,None,None,5,2)
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False),patch('curses.mousemask'),patch('curses.set_escdelay') as delay:
            dashboard.setup()
        delay.assert_called_once_with(20)
        self.assertEqual(screen.wait,25)


if __name__=='__main__':unittest.main()
