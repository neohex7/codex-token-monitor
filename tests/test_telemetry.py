import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from codex_token_monitor.telemetry import Monitor, Rollout, normalize_limit, usage


def event(kind, payload, at=100):
    return {'type':kind, 'timestamp':at, 'payload':payload}


def start(tid='t1', at=100):
    return event('event_msg', {'type':'task_started','turn_id':tid}, at)


def tokens(total, at=101, cached=0, out=10):
    return event('event_msg', {'type':'token_count','info':{'total_token_usage':{
        'input_tokens':total-out,'output_tokens':out,'total_tokens':total,
        'cached_input_tokens':cached}}},at)


def modern(total, tid='t1', response='r1', at=101, cumulative=None, thread='chat'):
    value = {'input_tokens':total-10, 'output_tokens':10, 'total_tokens':total}
    return event('token_usage_record',{'thread_id':thread,'turn_id':tid,'root_turn_id':tid,
        'response_id':response,'usage':value,'thread_token_usage':{'total_tokens':cumulative or total}},at)


def prompt(text, at=100):
    return event('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':text}]},at)


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'rollout.jsonl'
        self.path.touch()
        self.reader = Rollout(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *events):
        with self.path.open('a') as f:
            for item in events:
                f.write(json.dumps(item)+'\n')
        self.reader.refresh()

    def test_legacy_duplicate_snapshots_and_compaction(self):
        self.append(start(), prompt('first'), tokens(100), tokens(100),
            event('compacted', {'latest_token_usage_record':{}},102), tokens(180,103),
            event('event_msg',{'type':'task_complete','turn_id':'t1'},104),
            start('t2',110),prompt('second',110),tokens(250,111),tokens(250,112))
        self.assertEqual(self.reader.total['total_tokens'],250)
        self.assertEqual([t['tokens']['total_tokens'] for t in self.reader.public_turns(113)],[180,70])
        self.assertEqual(self.reader.public_turns(113)[0]['status'],'completed')
        self.assertEqual(self.reader.public_turns(113)[1]['prompt'],'second')
        self.reader.refresh()
        self.assertEqual(self.reader.total['total_tokens'],250)

    def test_mixed_versions_have_independent_counter_bases(self):
        self.append(start('old'),prompt('historic'),tokens(1000),
            event('event_msg',{'type':'task_complete','turn_id':'old'},102),
            start(),prompt('new'),modern(100,cumulative=100),tokens(1100,out=20),
            modern(150,response='r2',cumulative=250),tokens(1250,out=30))
        self.assertEqual(self.reader.total['total_tokens'],1250)
        self.assertEqual(self.reader.public_turns(120)[1]['tokens']['total_tokens'],250)
        self.assertFalse(any('reset' in w.lower() for w in self.reader.warnings))

    def test_modern_only_deduplicates_responses(self):
        self.append(start(),prompt('one'),modern(100),modern(100),modern(150,response='r2',cumulative=250))
        self.assertEqual(self.reader.total['total_tokens'],250)
        self.assertEqual(self.reader.public_turns(105)[0]['tokens']['total_tokens'],250)
        self.assertEqual(self.reader.own_records['total_tokens'],250)

    def test_modern_followed_by_same_legacy_not_counted_twice(self):
        self.append(start(),prompt('one'),modern(100),tokens(100),modern(150,response='r2',cumulative=250),tokens(250,out=20))
        self.assertEqual(self.reader.total['total_tokens'],250)
        self.assertEqual(self.reader.public_turns(105)[0]['tokens']['total_tokens'],250)

    def test_partial_line_waits_for_newline_then_processes_once(self):
        self.append(start())
        data=json.dumps(tokens(100)).encode()
        with self.path.open('ab') as f:f.write(data[:len(data)//2])
        self.reader.refresh()
        self.assertEqual(self.reader.total['total_tokens'],0)
        with self.path.open('ab') as f:f.write(data[len(data)//2:]+b'\n')
        self.reader.refresh()
        self.assertEqual(self.reader.total['total_tokens'],100)
        self.reader.refresh()
        self.assertEqual(self.reader.total['total_tokens'],100)

    def test_truncated_rollout_rebuilds(self):
        self.append(start(),prompt('original question'),tokens(999))
        self.path.write_text(json.dumps(tokens(80))+'\n')
        self.reader.refresh()
        self.assertEqual(self.reader.total['total_tokens'],80)
        self.assertTrue(any('truncated' in w for w in self.reader.warnings))

    def test_bad_json_is_skipped(self):
        self.path.write_text('not json\n'+json.dumps(start())+'\n'+json.dumps(tokens(100))+'\n')
        self.reader.refresh()
        self.assertEqual(self.reader.total['total_tokens'],100)
        self.assertEqual(self.reader.bad_lines,1)

    def test_bad_nested_shapes_do_not_abort_refresh(self):
        self.append(start(), modern(100),
                    event('token_usage_record', {'usage':[], 'response_id':'bad'}),
                    event('event_msg', {'type':'token_count','info':'invalid'}),
                    event('event_msg', {'type':'token_count','rate_limits':{'primary':'invalid'}}),
                    tokens(100))
        self.assertEqual(self.reader.total['total_tokens'],100)
        self.assertEqual(self.reader.bad_lines,3)

    def test_modern_pending_update_visible_before_legacy_snapshot(self):
        self.append(start(), modern(100), tokens(100))
        self.append(modern(150,response='r2',cumulative=250))
        self.assertEqual(self.reader.total['total_tokens'],250)
        self.append(tokens(250,out=20))
        self.assertEqual(self.reader.total['total_tokens'],250)

    def test_context_filtered_and_steering_same_turn(self):
        self.append(start(),prompt('# AGENTS.md instructions\nsecret rules'),
                    prompt('<external_codex_apps_open_page>{}</external_codex_apps_open_page>'),
                    prompt('Actual question'),prompt('Actual question'),prompt('Follow-up while working'),tokens(100))
        turn=self.reader.public_turns(105)[0]
        self.assertEqual(len(self.reader.turns),1)
        self.assertEqual(turn['prompt'],'Actual question\n\n---\n\nFollow-up while working')

    def test_pre_start_message_adopted_without_extra_turn(self):
        self.append(prompt('Question'),start('real'),event('turn_context',{'turn_id':'real','model':'model'}),tokens(100))
        self.assertEqual(len(self.reader.turns),1)
        self.assertEqual(self.reader.turns[0]['id'],'real')
        self.assertEqual(self.reader.turns[0]['prompt'],'Question')

    def test_interruption_has_end_time(self):
        self.append(start(),tokens(100),event('event_msg',{'type':'turn_aborted','turn_id':'t1'},110))
        turn=self.reader.public_turns(999)[0]
        self.assertEqual(turn['status'],'interrupted')
        self.assertEqual(turn['avg_tps'],1)

    def test_models_and_cost_survive_model_changes_and_response_dedup(self):
        self.append(start(), event('turn_context', {'turn_id':'t1','model':'gpt-6.1-sol'}),
            modern(100), modern(100),
            event('turn_context', {'turn_id':'t1','model':'gpt-6-luna'},102),
            modern(100,response='r2',at=103),
            event('event_msg', {'type':'task_complete','turn_id':'t1'},110),
            start('t2',120), event('turn_context', {'turn_id':'t2','model':'gpt-5.5'},120),
            modern(100,tid='t2',response='r3',at=121))
        first,second=self.reader.public_turns(130)
        self.assertEqual(first['models'],['gpt-6.1-sol','gpt-6-luna'])
        self.assertEqual(second['model'],'gpt-5.5')
        self.assertEqual(first['avg_tps'],2)
        self.assertEqual(first['api_cost']['calls'],2)
        self.assertEqual(Decimal(first['api_cost']['usd']),Decimal('.000294'))

    def test_no_historical_model_is_inferred_from_a_later_turn(self):
        self.append(start(),modern(100),start('t2',110),
            event('turn_context',{'turn_id':'t2','model':'gpt-6.1-sol'},110),
            modern(100,tid='t2',response='r2',at=111))
        first,second=self.reader.public_turns(120)
        self.assertEqual(first['model'],'unknown')
        self.assertIsNone(first['api_cost']['usd'])
        self.assertIsNotNone(second['api_cost']['usd'])

    def test_legacy_turn_cannot_be_priced_as_one_large_request(self):
        self.append(start(),event('turn_context',{'turn_id':'t1','model':'gpt-6.1-sol'}),
            tokens(500000))
        turn=self.reader.public_turns(110)[0]
        self.assertEqual(turn['model'],'gpt-6.1-sol')
        self.assertIsNone(turn['api_cost']['usd'])

    def test_empty_active_turn_has_zero_reported_cost_so_far(self):
        self.append(start(),event('turn_context',{'turn_id':'t1','model':'gpt-6.1-sol'}))
        cost=self.reader.public_turns(110)[0]['api_cost']
        self.assertTrue(cost['complete'])
        self.assertEqual(Decimal(cost['usd']),0)

    def test_modern_partial_coverage_keeps_known_cost_but_no_false_total(self):
        record=modern(100)
        record['payload']['turn_token_usage']={'input_tokens':180,'output_tokens':20,'total_tokens':200}
        self.append(start(),event('turn_context',{'turn_id':'t1','model':'gpt-6.1-sol'}),record)
        cost=self.reader.public_turns(110)[0]['api_cost']
        self.assertIsNone(cost['usd'])
        self.assertEqual(Decimal(cost['known_usd']),Decimal('.00028'))

    def test_quota_history_does_not_drift_after_turn_ends(self):
        def quota(percent,at):
            return event('event_msg',{'type':'token_count','info':None,'rate_limits':{
                'limit_id':'codex','primary':{'used_percent':percent,'resets_at':1000,'window_minutes':300}}},at)
        self.append(quota(10,98),start(),tokens(100),quota(11,102),
            event('event_msg',{'type':'task_complete','turn_id':'t1'},103),quota(50,104))
        deltas=self.reader.public_turns(105)[0]['limit_deltas']
        self.assertTrue(any('+1.00 pp' in v for v in deltas))
        self.assertFalse(any('+40.00 pp' in v for v in deltas))

    def test_quota_reset_is_not_attributed_to_turn(self):
        def quota(percent,reset):return event('event_msg',{'type':'token_count','rate_limits':{'primary':{'used_percent':percent,'resets_at':reset}}})
        self.append(quota(20,1000),start(),quota(1,2000))
        self.assertEqual(self.reader.public_turns(105)[0]['limit_deltas'],[])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.rollout = self.home/'one.jsonl'
        self.rollout.write_text('\n'.join(json.dumps(e) for e in (start(),prompt('Question'),tokens(100)))+'\n')
        self.db = self.home/'state_5.sqlite'
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE threads(id,title,name,source,thread_source,rollout_path,updated_at,tokens_used,model,cwd,archived)')
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('chat','first message','Real chat name','vscode','user',str(self.rollout),100,100,'model','/tmp',0))
        self.monitor=Monitor(self.home)

    def tearDown(self):
        self.monitor.close()
        self.temp.cleanup()

    def test_exact_title_and_no_db_changes(self):
        before=self.db.read_bytes()
        snapshot=self.monitor.snapshot('chat')
        self.assertEqual(snapshot['selected']['title'],'Real chat name')
        self.assertEqual(snapshot['selected']['tokens']['total_tokens'],100)
        self.assertEqual(self.db.read_bytes(),before)
        self.assertFalse(list(self.home.glob('*journal')))

    def test_missing_rollout_does_not_turn_a_database_balance_into_free_usage(self):
        self.rollout.unlink()
        cost=self.monitor.snapshot('chat')['selected']['api_cost']
        self.assertIsNone(cost['usd'])

    def test_tps_uses_deltas_and_never_initial_history(self):
        with patch('codex_token_monitor.telemetry.time.monotonic',side_effect=[100,105,110]):
            first=self.monitor.snapshot('chat')
            with self.rollout.open('a') as f:f.write(json.dumps(tokens(200,out=30))+'\n')
            second=self.monitor.snapshot('chat')
            third=self.monitor.snapshot('chat')
        self.assertIsNone(first['selected']['tps']['total'])
        self.assertEqual(second['selected']['tps']['total'],20)
        self.assertEqual(second['selected']['tps']['output'],4)
        self.assertEqual(third['selected']['tps']['total'],0)

    def test_search_empty_does_not_select_other_chat(self):
        self.assertIsNone(self.monitor.snapshot(query='no-match')['selected'])

    def test_agents_hidden_by_default_and_distinct(self):
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(child_thread_id,parent_thread_id,status)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)',('child','chat','completed'))
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('child','','Agent','{}','subagent',str(self.rollout),99,25,'model','/tmp',0))
        a=self.monitor.snapshot('chat')
        self.assertEqual(len(a['threads']),1)
        self.assertEqual(a['selected']['children_total'],25)
        self.assertEqual(a['selected']['tokens']['total_tokens'],100)
        self.assertEqual(len(self.monitor.snapshot('chat',include_agents=True)['threads']),2)

    def test_all_quota_buckets_and_windows_normalized(self):
        raw={'limitId':'model-x','primary':{'usedPercent':12.345,'windowDurationMins':300,'resetsAt':2000},'secondary':{'usedPercent':3,'windowDurationMins':10080,'resetsAt':9000}}
        value=normalize_limit(raw,100,'live')
        self.assertEqual(value['id'],'model-x')
        self.assertEqual(value['primary']['used_percent'],12.345)
        self.assertEqual(value['secondary']['window_minutes'],10080)

    def test_child_usage_is_linked_to_parent_turn_without_changing_root_counter(self):
        child_path=self.home/'child.jsonl'
        record=modern(40,tid='child-turn',thread='child')
        record['payload']['root_turn_id']='t1'
        child_path.write_text('\n'.join(json.dumps(e) for e in (start('child-turn'),record,record))+'\n')
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(child_thread_id,parent_thread_id,status)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)',('child','chat','completed'))
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('child','','Agent','{}','subagent',str(child_path),99,40,'model','/tmp',0))
        snapshot=self.monitor.snapshot('chat')['selected']
        self.assertEqual(snapshot['tokens']['total_tokens'],100)
        self.assertEqual(snapshot['children_recorded_tokens']['total_tokens'],40)
        self.assertEqual(snapshot['turns'][0]['children_tokens']['total_tokens'],40)

    def test_child_prices_use_child_model_and_are_added_once(self):
        self.rollout.write_text('\n'.join(json.dumps(e) for e in (
            start(),event('turn_context',{'turn_id':'t1','model':'gpt-6.1-sol'}),modern(100)))+'\n')
        child_path=self.home/'priced-child.jsonl'
        record=modern(100,tid='child-turn',thread='child')
        record['payload']['root_turn_id']='t1'
        child_path.write_text('\n'.join(json.dumps(e) for e in (
            start('child-turn'),event('turn_context',{'turn_id':'child-turn','model':'gpt-6-luna'}),
            record,record))+'\n')
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(child_thread_id,parent_thread_id,status)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)',('child','chat','completed'))
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('child','','Agent','{}','subagent',str(child_path),99,100,'gpt-6-luna','/tmp',0))
        s=self.monitor.snapshot('chat')['selected'];t=s['turns'][0]
        self.assertEqual(Decimal(t['api_cost']['usd']),Decimal('.00028'))
        self.assertEqual(Decimal(t['children_api_cost']['usd']),Decimal('.000014'))
        self.assertEqual(Decimal(t['api_cost_with_children']['usd']),Decimal('.000294'))
        self.assertEqual(t['children_api_cost']['models'],['gpt-6-luna'])
        self.assertEqual(s['api_cost_with_children'],t['api_cost_with_children'])


if __name__ == '__main__':unittest.main()
