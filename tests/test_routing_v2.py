import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import plugin_loader
import importlib
mod=plugin_loader.load_module('routing')
r=importlib.import_module(mod.__name__+'.routing_v2');rt=importlib.import_module(mod.__name__+'.runtime_v2')
IDS={'openai-codex':['gpt-6-luna','gpt-6.1-sol','gpt-6-astra'],'anthropic':['claude-haiku-4-5-20251001','claude-sonnet-5-5','claude-opus-5-5']}
def reply(complexity='simple',kind='general',need='none'):
 return {'answers':{k:{'type':'choice','choice':v,'confidence':1,'probabilities':{x:float(x==v) for x in r.QUESTIONS[k]['criteria']}} for k,v in zip(r.QUESTIONS,[complexity,kind,need])}}
class V2Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name);self.calls=[]
  self.cfg={'model':{'default':'gpt-6.1-sol','provider':'openai-codex'},'agent':{'reasoning_effort':'low'},'plugins':{'entries':{'jev-routing':{'settings':{'mode':'on'}}}}}
  self.answer=reply()
  def api(*args):self.calls.append(1);return self.answer
  self.e=r.Engine(self.home,lambda:self.cfg,lambda:IDS,api);self.addCleanup(self.e.pool.shutdown)
  self.baseline={'provider':'openai-codex','model':'gpt-6-astra','effort':'low'}
  self.policy=self.e.policy();self.session=self.e.store.session('s','owner',self.baseline)
 def body(self,**kw):
  return dict(agentId='friday',sessionId='s',idempotencyKey='t1',expectedPolicyRevision=1,expectedSelectionRevision=1,text='Summarize this paragraph.',inputTokens=10000,**kw)
 def plan(self,body=None):return self.e.plan('s','owner',body or self.body(),self.baseline)
 def test_preserves_selected_baseline_routes_small_and_idempotent(self):
  b=self.body();p=self.plan(b);self.assertEqual(p['slot'],'small');self.assertEqual(p['baseline'],self.baseline);self.assertEqual(p,self.plan(b));self.assertEqual(self.calls,[1]);self.assertIn('--once',p['rpc']['params']['value']);self.assertFalse(p['applied'])
 def test_off_makes_no_api_call_no_switch(self):
  self.e.store.save_session('s','owner',{'routingOverride':False,'selectedBaseline':self.baseline},1)
  b=self.body();b['expectedSelectionRevision']=2;p=self.plan(b);self.assertEqual(p['target'],self.baseline);self.assertIsNone(p['rpc']);self.assertEqual(self.calls,[])
 def test_ordinary_and_complex_code(self):
  self.answer=reply('ordinary','code');p=self.plan(self.body(taskKind='code'));self.assertEqual(p['slot'],'medium')
  self.e.transition('s','owner',p['routeId'],'cancel');self.answer=reply('complex','code');b=self.body();b['idempotencyKey']='next';p=self.plan(b);self.assertEqual(p['slot'],'big')
 def test_simple_code_floor(self):
  self.answer=reply('simple','code');p=self.plan();self.assertEqual(p['slot'],'medium')
 def test_error_code_goes_big_general_baseline(self):
  self.answer={};p=self.plan(self.body(taskKind='code'));self.assertEqual(p['slot'],'big')
  self.e.transition('s','owner',p['routeId'],'cancel');b=self.body();b['idempotencyKey']='next';p=self.plan(b);self.assertEqual(p['target'],self.baseline)
 def test_context_unknown_does_not_downshift(self):
  b=self.body();b.pop('inputTokens');p=self.plan(b);self.assertEqual(p['target'],self.baseline);self.assertEqual(p['reason'],'context_unverified_or_too_large')
 def test_secret_never_sent_or_persisted(self):
  b=self.body();b['text']='my api_key = sk-or-v1-'+('a'*60);p=self.plan(b);self.assertEqual(self.calls,[])
  self.assertNotIn(b['text'],self.e.store.path.read_bytes().decode(errors='ignore'));self.assertEqual(p['target'],self.baseline)
 def test_conflicts_and_ownership(self):
  p=self.plan();b=self.body();b['text']='changed'
  with self.assertRaises(r.RoutingError) as ex:self.plan(b)
  self.assertEqual(ex.exception.code,'idempotency_conflict')
  with self.assertRaises(r.RoutingError):self.e.get_plan('s','other',p['routeId'])
  with self.assertRaises(r.RoutingError):self.e.store.session('s','other',self.baseline)
  b=self.body();b['idempotencyKey']='different'
  with self.assertRaises(r.RoutingError) as ex:self.plan(b)
  self.assertEqual(ex.exception.code,'session_reserved')
 def test_policy_cas_and_invalid_slot(self):
  body={'expectedRevision':1,'enabledDefault':True,'slots':copy.deepcopy(self.policy['slots'])};body['slots']['big']['model']='gpt-6-luna'
  with self.assertRaises(r.RoutingError):self.e.put_policy(body)
  body['slots']['big']['model']='gpt-6-astra';self.assertEqual(self.e.put_policy(body)['revision'],2)
  with self.assertRaises(r.RoutingError):self.e.put_policy(body)
 def test_claude_routes_and_provider_limits(self):
  slots=copy.deepcopy(self.policy['slots'])
  slots['small']={'provider':'anthropic','model':'claude-haiku-4-5-20251001','effort':'low'}
  slots['big']={'provider':'anthropic','model':'claude-opus-5-5','effort':'high'}
  body={'expectedRevision':1,'autoModelDefault':True,'autoEffortDefault':False,'slots':slots}
  for slot,value,code in [('medium',{'provider':'anthropic','model':'claude-haiku-4-5-20251001','effort':'medium'},'coding_slot_requires_sol_or_stronger'),
                          ('big',{'provider':'openrouter','model':'claude-opus-5-5','effort':'high'},'model_unavailable'),
                          ('big',{'provider':'anthropic','model':'gpt-6.1-sol','effort':'high'},'model_unavailable')]:
   bad=copy.deepcopy(body);bad['slots'][slot]=value
   with self.assertRaises(r.RoutingError) as ex:self.e.put_policy(bad)
   self.assertEqual(ex.exception.code,code)
  mixed=copy.deepcopy(body)
  with self.assertRaises(r.RoutingError) as ex:self.e.put_policy(mixed)
  self.assertEqual(ex.exception.code,'routes_share_one_provider')
  body['slots']['medium']={'provider':'anthropic','model':'claude-sonnet-5-5','effort':'medium'}
  self.assertEqual(self.e.put_policy(body)['revision'],2)
  b=self.body();b['expectedPolicyRevision']=2;p=self.plan(b)
  self.assertEqual(p['target']['provider'],'anthropic');self.assertIn('claude-haiku-4-5-20251001 --provider anthropic --once',p['rpc']['params']['value'])
  self.e.transition('s','owner',p['routeId'],'arm');self.e.store.observe('s','turn',b['text'],'claude-haiku-4-5-20251001','anthropic')
  self.assertEqual(rt.guard(self.home,session_id='s',turn_id='turn',tool_name='terminal')['action'],'block')
 def test_arm_stale_revision_rejected(self):
  p=self.plan();self.e.store.save_session('s','owner',{'routingOverride':False,'selectedBaseline':self.baseline},1)
  with self.assertRaises(r.RoutingError):self.e.transition('s','owner',p['routeId'],'arm')
 def test_observe_request_and_coding_guard(self):
  p=self.plan();self.e.transition('s','owner',p['routeId'],'arm');self.e.store.observe('s','turn','Summarize this paragraph.','gpt-6-luna','openai-codex')
  self.assertFalse(self.e.get_plan('s','owner',p['routeId'])['applied'])
  self.e.store.record_request('s','turn','gpt-6-luna','openai-codex','low');self.assertTrue(self.e.get_plan('s','owner',p['routeId'])['applied'])
  block=rt.guard(self.home,session_id='s',turn_id='turn',tool_name='terminal');self.assertEqual(block['action'],'block');self.assertTrue(block['message'])
  self.assertIsNone(rt.guard(self.home,session_id='s',turn_id='turn',tool_name='web_search'))
  self.assertIsNone(rt.guard(self.home,session_id='other',turn_id='turn',tool_name='terminal'))
 def test_deadline_and_one_worker(self):
  done=threading.Event();self.e.api=lambda *args:(done.wait(2),reply())[1]
  try:
   start=time.monotonic();p=self.plan();self.assertLess(time.monotonic()-start,.8);self.assertEqual(p['reason'],'deadline');self.e.transition('s','owner',p['routeId'],'cancel')
   b=self.body();b['idempotencyKey']='next';p=self.plan(b);self.assertEqual(p['reason'],'router_busy')
  finally:done.set()
 def test_kill_and_strict_fields(self):
  (self.home/'jev.disabled').touch();p=self.plan();self.assertEqual(p['target'],self.baseline);self.assertEqual(self.calls,[])
  for b in [self.body(extra='x'),{**self.body(),'expectedPolicyRevision':True}]:
   with self.assertRaises(r.RoutingError):self.plan(b)
 def test_profile_isolation(self):
  other=r.Engine(self.home/'other',lambda:self.cfg,lambda:IDS,lambda *a:reply('complex'))
  self.addCleanup(other.pool.shutdown);other.policy();other.store.session('s','owner',self.baseline)
  self.assertEqual(self.plan()['slot'],'small');self.assertEqual(other.plan('s','owner',self.body(),self.baseline)['slot'],'big')
if __name__=='__main__':unittest.main()
