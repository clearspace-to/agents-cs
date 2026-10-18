"""Per-request model/effort rewrites for server-side routing (2.4), through Hermes' llm_request middleware.

Hermes keeps one provider client per agent, so a route may only change the model and effort inside the
provider the turn already uses; the chat's own model, provider and session config are never written.
Every function returns a complete replacement payload (Hermes' middleware contract) or raises Skip.
"""
import copy
import json

TOKEN_BOUND = 180000


class Skip(Exception):
 """This request can't take the routed model; keep the chat's own model (effort may still apply)."""
 def __init__(self, reason): self.reason = reason


def tokens(request):
 """Conservative bound for the whole request, as the app computed it: UTF-8 bytes / 3, plus headroom."""
 try:raw=json.dumps(request,ensure_ascii=False,default=str).encode()
 except Exception:return None
 return len(raw)//3+8000


class Codex:
 """openai-codex over codex_responses."""
 provider, api_mode = 'openai-codex', 'codex_responses'

 @staticmethod
 def effort(request):
  r=request.get('reasoning')
  return r.get('effort') if isinstance(r,dict) else None

 @staticmethod
 def check(request,model=None):
  """Why the model can't change for this request, or None."""
  if request.get('service_tier'):return 'fast_mode'
  if any(isinstance(i,dict) and i.get('type')=='compaction' for i in request.get('input') or []):return 'native_compaction'
  return None

 @staticmethod
 def strip(request):
  """Encrypted reasoning is sealed to the model that minted it, and Hermes stamps replies with the chat's
  model: send the routed model none of the chat model's blobs and ask it for none of its own, so neither
  model is ever handed the other's (HTTP 400 invalid_encrypted_content)."""
  r=copy.deepcopy(request)
  r['input']=[i for i in r.get('input') or [] if not (isinstance(i,dict) and i.get('type')=='reasoning')]
  if 'include' in r:r['include']=[x for x in r.get('include') or [] if x!='reasoning.encrypted_content']
  return r

 @classmethod
 def rewrite(cls,request,model=None,effort=None,**kw):
  r=cls.strip(request) if model else copy.deepcopy(request)
  if model:r['model']=model
  if effort and isinstance(r.get('reasoning'),dict):r['reasoning']['effort']=effort
  return r

 @classmethod
 def bound(cls,request):return tokens(cls.strip(request))


class Anthropic:
 """anthropic over anthropic_messages (Claude subscription). Uses Hermes' own model tables, read-only."""
 provider, api_mode = 'anthropic', 'anthropic_messages'

 @staticmethod
 def helpers():
  from agent import anthropic_adapter as a
  return a

 @staticmethod
 def effort(request):
  oc=request.get('output_config')
  return oc.get('effort') if isinstance(oc,dict) else None

 @classmethod
 def check(cls,request,model=None):
  a=cls.helpers()
  t=request.get('thinking')
  if isinstance(t,dict) and t.get('type')=='enabled':return 'manual_thinking'
  if model and not ('haiku' in model.lower() or a._supports_adaptive_thinking(model)):return 'manual_thinking'
  if (request.get('extra_body') or {}).get('speed') and model and not a._supports_fast_mode(model):return 'fast_mode'
  return None

 @staticmethod
 def strip_prior_thinking(messages):
  """Drop thinking from replies before the current user turn: they were signed by another model."""
  def new_turn(m):
   c=m.get('content')
   return m.get('role')=='user' and (isinstance(c,str) or any(not (isinstance(b,dict) and b.get('type')=='tool_result') for b in c or []))
  last=max((i for i,m in enumerate(messages) if isinstance(m,dict) and new_turn(m)),default=-1)
  out=[]
  for i,m in enumerate(messages):
   if i<last and isinstance(m,dict) and m.get('role')=='assistant' and isinstance(m.get('content'),list):
    kept=[b for b in m['content'] if not (isinstance(b,dict) and b.get('type') in ('thinking','redacted_thinking'))]
    m={**m,'content':kept or [{'type':'text','text':'(thinking elided)'}]}
   out.append(m)
  return out

 @classmethod
 def rewrite(cls,request,model=None,effort=None,strip=False,**kw):
  a=cls.helpers();r=copy.deepcopy(request);target=model or r['model']
  if model:
   r['model']=a.normalize_model_name(model)
   r['max_tokens']=min(int(r.get('max_tokens') or a._get_anthropic_max_output(model)),a._get_anthropic_max_output(model))
   if a._forbids_sampling_params(model):
    for k in ('temperature','top_p','top_k'):r.pop(k,None)
  if strip:r['messages']=cls.strip_prior_thinking(r.get('messages') or [])
  disabled=isinstance(r.get('thinking'),dict) and r['thinking'].get('type')=='disabled'
  if 'haiku' in target.lower():
   r.pop('thinking',None);r.pop('output_config',None)
  elif not disabled and (effort or model):
   level=effort or cls.effort(request) or 'medium'
   r.pop('output_config',None);r.pop('thinking',None)
   r.update(a._thinking_kwargs({'effort':level},target,r['max_tokens']))
  return r

 @classmethod
 def bound(cls,request):return tokens(request)


ADAPTERS={(c.provider,c.api_mode):c for c in (Codex,Anthropic)}
