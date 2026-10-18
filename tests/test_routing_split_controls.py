import copy
import json
import unittest
import test_routing_v2 as f
import test_routing_native_fallback as native

class SplitControlsTests(unittest.TestCase):
 body=f.V2Tests.body
 plan=f.V2Tests.plan
 prepare=native.NativeFallbackTests.prepare
 request=native.NativeFallbackTests.request
 def setUp(self):
  native.NativeFallbackTests.setUp(self);self.policy_snapshot=self.policy;del self.policy
 def policy(self,model,effort):
  return self.e.put_policy({'expectedRevision':self.e.policy()['revision'],'autoModelDefault':model,'autoEffortDefault':effort,'slots':self.e.policy()['slots']})
 def selection(self,model,effort):
  return self.e.put_session('s','owner',{'expectedRevision':self.e.store.session('s','owner',self.baseline)['revision'],'autoModelOverride':model,'autoEffortOverride':effort,'selectedBaseline':self.baseline})
 def test_all_four_plan_combinations(self):
  self.baseline['effort']='high';self.selection(None,None)
  for model,effort in [(False,False),(True,False),(False,True),(True,True)]:
   with self.subTest(model=model,effort=effort):
    p=self.policy(model,effort);b=self.body();b.update(expectedPolicyRevision=p['revision'],expectedSelectionRevision=2,idempotencyKey=f'{model}-{effort}')
    before=len(self.calls);route=self.plan(b)
    self.assertEqual(route['target']['model'],'gpt-6-luna' if model else self.baseline['model'])
    self.assertEqual(route['target']['effort'],'low' if effort else 'high')
    self.assertEqual(len(self.calls)-before,int(model or effort))
    self.assertEqual(route['autoModel'],model);self.assertEqual(route['autoEffort'],effort)
    self.e.transition('s','owner',route['routeId'],'cancel')
 def test_native_model_only_routes_model_and_keeps_effort(self):
  self.wire['reasoning']['effort']='medium';self.policy(True,False);self.prepare();out=self.request()['request']
  self.assertEqual((out['model'],out['reasoning']['effort']),('gpt-6-luna','medium'));self.assertEqual(self.calls,[1])
 def test_effort_override_changes_pinned_model_effort_only(self):
  self.policy(False,False);self.selection(False,True);self.e.pinned=lambda *a:True
  self.answer=f.reply('complex');self.prepare();out=self.request()['request']
  self.assertEqual(out['model'],self.wire['model']);self.assertEqual(out['reasoning']['effort'],'high')
  self.assertFalse(self.e.store.model_enabled('s'))
 def test_independent_inheritance_and_off_override(self):
  self.policy(True,True);self.selection(None,False)
  self.assertEqual(f.r.effective(self.e.policy(),self.e.store.session('s','owner',self.baseline)),(True,False))
  self.prepare();out=self.request()['request'];self.assertEqual((out['model'],out['reasoning']['effort']),('gpt-6-luna','low'))
  self.policy(False,False);self.selection(True,None)
  self.assertEqual(f.r.effective(self.e.policy(),self.e.store.session('s','owner',self.baseline)),(True,False))
 def test_effort_only_uncertain_code_preserves_baseline_no_guard(self):
  p=self.policy(False,True);self.answer={};b=self.body(taskKind='code');b['expectedPolicyRevision']=p['revision'];route=self.plan(b)
  self.assertEqual(route['target'],self.baseline);self.assertIsNone(route['slot'])
  self.e.transition('s','owner',route['routeId'],'arm');self.e.store.observe('s','t',b['text'],'gpt-6-luna','openai-codex')
  self.assertIsNone(f.rt.guard(self.home,session_id='s',turn_id='t',tool_name='terminal'))
  self.assertIsNone(f.rt.guard(self.home,session_id='s',turn_id='t',tool_name='delegate',args={'model':'gpt-6-luna'}))
 def test_legacy_read_and_write_and_reject_mixed(self):
  with self.e.store.db() as db:
   raw={'revision':8,'enabledDefault':False,'slots':self.policy_slots()}
   db.execute('UPDATE policy SET value=? WHERE id=1',(json.dumps(raw),))
  self.assertEqual(f.r.toggles(self.e.policy()),(False,False))
  p=self.e.put_policy({'expectedRevision':8,'enabledDefault':True,'slots':self.policy_slots()});self.assertEqual(f.r.toggles(p),(True,True))
  with self.assertRaises(f.r.RoutingError):self.e.put_policy({'expectedRevision':9,'enabledDefault':True,'autoEffortDefault':False,'slots':p['slots']})
  with self.assertRaises(f.r.RoutingError):self.selection(False,'true')
 def policy_slots(self):return self.policy_snapshot['slots']
 def test_late_effort_off_discards_result(self):
  self.policy(False,True);self.selection(False,True);self.prepare()
  def api(*args):self.selection(False,False);return f.reply('complex')
  self.e.api=api;self.assertIsNone(self.request())
