"""2.4 server-side routing: the plugin picks and applies the model per turn through llm_request middleware."""
import copy
import json
import sys
import types
import unittest
import test_routing_v2 as f
import test_routing_native_fallback as native
reply, r, rt = f.reply, f.r, f.rt
serve = sys.modules[f.mod.__name__ + '.serve']

class ServerRoutingTests(unittest.TestCase):
 body = f.V2Tests.body
 plan = f.V2Tests.plan
 prepare = native.NativeFallbackTests.prepare
 request = native.NativeFallbackTests.request
 def setUp(self):
  native.NativeFallbackTests.setUp(self)
  self.wire['include']=['reasoning.encrypted_content']
  self.wire['input']=[{'type':'reasoning','encrypted_content':'sealed-to-astra','summary':[]},{'role':'user','content':'body'}]
 def put(self,model=True,effort=True,slots=None):
  return self.e.put_policy({'expectedRevision':self.e.policy()['revision'],'autoModelDefault':model,'autoEffortDefault':effort,'slots':slots or self.e.policy()['slots']})

 def test_routes_model_and_effort_once_per_turn_without_touching_the_chat(self):
  self.answer=reply('complex');self.prepare();before=copy.deepcopy(self.wire)
  out=self.request()['request']
  self.assertEqual((out['model'],out['reasoning']['effort']),('gpt-6.1-sol','high'))
  self.assertEqual(self.wire,before)
  # The routed model never gets the chat model's sealed reasoning, nor mints its own.
  self.assertEqual(out['include'],[]);self.assertFalse(any(i.get('type')=='reasoning' for i in out['input']))
  self.assertEqual(self.request()['request'],out);self.assertEqual(self.calls,[1])
  record=self.e.store.turns('s')[0]
  self.assertEqual((record['slot'],record['served']['model'],record['baseline']['model']),('big','gpt-6.1-sol','gpt-6-astra'))
  self.assertNotIn('body',json.dumps(record))

 def test_same_model_effort_only_keeps_replay(self):
  self.put(False,True);self.answer=reply('ordinary');self.prepare();out=self.request()['request']
  self.assertEqual(out['model'],'gpt-6-astra');self.assertEqual(out['include'],['reasoning.encrypted_content']);self.assertEqual(out['reasoning']['effort'],'medium')

 def test_fast_compaction_and_large_requests_keep_the_chat_model(self):
  cases=[('fast_mode',lambda w:w.update(service_tier='priority')),
         ('native_compaction',lambda w:w['input'].insert(0,{'type':'compaction','encrypted_content':'x'})),
         ('context_unverified_or_too_large',lambda w:w['input'].append({'role':'user','content':'x'*600000}))]
  base=copy.deepcopy(self.wire)
  for n,(reason,change) in enumerate(cases):
   with self.subTest(reason=reason):
    self.wire=copy.deepcopy(base);change(self.wire);self.answer=reply('complex');self.prepare(str(n))
    out=self.request(str(n))['request'];self.assertEqual(out['model'],'gpt-6-astra');self.assertEqual(out['reasoning']['effort'],'high')
    self.assertEqual(self.e.store.turns('s')[0]['reason'],reason)

 def test_never_crosses_provider(self):
  slots={'small':{'provider':'anthropic','model':'claude-haiku-4-5-20251001','effort':'low'},'medium':{'provider':'anthropic','model':'claude-sonnet-5-5','effort':'medium'},'big':{'provider':'anthropic','model':'claude-opus-5-5','effort':'high'}}
  self.put(True,False,slots);self.prepare();self.assertIsNone(self.request())
  self.assertEqual(self.e.store.turns('s')[0]['reason'],'cross_provider_unavailable')

 def test_small_turn_blocks_code_tools_then_continues_on_medium(self):
  self.prepare();self.assertEqual(self.request()['request']['model'],'gpt-6-luna')
  self.assertIsNone(rt.guard(self.home,session_id='s',turn_id='turn',tool_name='web_search'))
  block=rt.guard(self.home,session_id='s',turn_id='turn',tool_name='terminal')
  self.assertEqual(block['action'],'block');self.assertIn('Medium',block['message'])
  out=self.request()['request'];self.assertEqual((out['model'],out['reasoning']['effort']),('gpt-6.1-sol','medium'))
  self.assertIsNone(rt.guard(self.home,session_id='s',turn_id='turn',tool_name='terminal'))

 def test_refused_route_falls_back_and_cools_down(self):
  self.prepare();self.assertEqual(self.request()['request']['model'],'gpt-6-luna')
  self.e.request_failed(session_id='s',turn_id='turn',status_code=429);self.assertEqual(self.request()['request']['model'],'gpt-6-luna')
  self.e.request_failed(session_id='s',turn_id='turn',status_code=400);self.assertIsNone(self.request())
  self.assertIsNone(rt.guard(self.home,session_id='s',turn_id='turn',tool_name='terminal'))
  self.prepare('next');self.assertIsNone(self.request('next'))
  self.assertEqual(self.e.store.turns('s')[0]['reason'],'target_failed_recently')

 def test_app_plan_turn_is_left_to_the_app(self):
  p=self.plan();self.e.transition('s','owner',p['routeId'],'arm');self.wire['model']='gpt-6-luna'
  self.prepare(text='Summarize this paragraph.');self.assertIsNone(self.request());self.assertEqual(self.calls,[1])

 def test_pins_surfaces_and_explicit_requests(self):
  self.e.pinned=lambda *a:True;self.prepare();self.assertIsNone(self.request());self.assertEqual(self.calls,[])
  # The app keeps its chats' switches current, so a managed chat routes despite its own pick.
  self.e.store.save_session('s','owner',{'autoModelOverride':None,'autoEffortOverride':None,'selectedBaseline':self.baseline},1)
  self.prepare('managed');self.assertEqual(self.request('managed')['request']['model'],'gpt-6-luna')
  self.e.prepare_native(session_id='s',turn_id='cron',user_message='Explain a token.',platform='cron');self.assertIsNone(self.request('cron'))
  self.prepare('explicit','/model gpt-6-astra');self.assertIsNone(self.request('explicit'))

 def test_unclassified_code_goes_big_and_unclassified_text_stays(self):
  self.answer={};self.prepare(text='Debug this script for me.');out=self.request()['request']
  self.assertEqual((out['model'],out['reasoning']['effort']),('gpt-6.1-sol','high'))
  self.prepare('plain','Explain a token.');self.assertIsNone(self.request('plain'))

 def test_hermes_provider_fallback_mid_turn_is_left_alone(self):
  self.prepare();self.assertEqual(self.request()['request']['model'],'gpt-6-luna')
  other=dict(self.wire,model='gpt-6.1-sol');self.assertIsNone(self.request(request=other,model='gpt-6.1-sol'))

 def test_off_switch_mid_turn_restores_the_chat_request(self):
  self.answer=reply('complex');self.prepare();self.assertIsNotNone(self.request())
  self.put(False,False);self.assertIsNone(self.request())

class AnthropicRewriteTests(unittest.TestCase):
 def setUp(self):
  fake=types.ModuleType('agent.anthropic_adapter')
  fake.normalize_model_name=lambda m,preserve_dots=False:m
  fake._get_anthropic_max_output=lambda m:64000 if 'haiku' in m or 'sonnet' in m else 128000
  fake._forbids_sampling_params=lambda m:'opus' in m
  fake._supports_adaptive_thinking=lambda m:'haiku' not in m
  fake._supports_fast_mode=lambda m:'opus' in m
  fake._thinking_kwargs=lambda cfg,m,mt:{} if 'haiku' in m else {'thinking':{'type':'adaptive','display':'summarized'},'output_config':{'effort':cfg['effort']}}
  pkg=sys.modules.get('agent') or types.ModuleType('agent');pkg.anthropic_adapter=fake
  self.saved={k:sys.modules.get(k) for k in ('agent','agent.anthropic_adapter')}
  sys.modules['agent']=pkg;sys.modules['agent.anthropic_adapter']=fake
  self.addCleanup(lambda:[sys.modules.pop(k,None) if v is None else sys.modules.__setitem__(k,v) for k,v in self.saved.items()])
  self.wire={'model':'claude-opus-5-5','max_tokens':128000,'thinking':{'type':'adaptive','display':'summarized'},'output_config':{'effort':'high'},
             'messages':[{'role':'user','content':'one'},{'role':'assistant','content':[{'type':'thinking','thinking':'t','signature':'opus'},{'type':'text','text':'a'}]},{'role':'user','content':'two'}]}

 def test_haiku_route_drops_thinking_and_clamps_output(self):
  A=serve.Anthropic;self.assertIsNone(A.check(self.wire,'claude-haiku-4-5-20251001'))
  out=A.rewrite(self.wire,model='claude-haiku-4-5-20251001',strip=True)
  self.assertEqual((out['model'],out['max_tokens']),('claude-haiku-4-5-20251001',64000))
  self.assertNotIn('thinking',out);self.assertNotIn('output_config',out)
  self.assertEqual(out['messages'][1]['content'],[{'type':'text','text':'a'}])

 def test_effort_on_own_model_and_fast_mode(self):
  out=serve.Anthropic.rewrite(self.wire,effort='low');self.assertEqual(out['output_config'],{'effort':'low'});self.assertEqual(out['messages'],self.wire['messages'])
  fast=dict(self.wire,extra_body={'speed':'fast'});self.assertEqual(serve.Anthropic.check(fast,'claude-sonnet-5-5'),'fast_mode')

if __name__=='__main__':unittest.main()
