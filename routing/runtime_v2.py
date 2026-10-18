"""Read-only Hermes adapters and supported plugin hooks."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from .routing_v2 import Engine, RoutingError, Store, identifier, small_family
from . import settings

_ENGINES={};_LOCK=threading.RLock()

def root():
 """Hermes's root home (Hermes's own home API); profiles live under it."""
 from .. import paths
 return paths.hermes_root()

def home_for(agent):
 identifier(agent)
 base=root()
 home=base if agent in ('friday','default') else base/'profiles'/agent
 # Only installed, configured profiles; never interpret an arbitrary filesystem target.
 if home.parent!=base/'profiles' and home!=base:raise RoutingError('agent_not_found',404)
 if not (home/'plugins/hermes-bots/routing/routing_v2.py').is_file():raise RoutingError('agent_not_found',404)
 if not settings.configured(config(home),home):raise RoutingError('agent_not_found',404)
 return home.resolve()

def config(home):
 import yaml
 try:return yaml.safe_load((Path(home)/'config.yaml').read_text()) or {}
 except FileNotFoundError:return {}

ROUTE_FAMILIES={'anthropic':r'claude-(?:opus|sonnet|haiku|fable)-[0-9][0-9a-z-]*','openai-codex':r'gpt-[0-9]+(?:\.[0-9]+)?-(?:luna|sol|astra)(?:-pro)?'}

def catalog(home):
 """{provider: [model ids]} a route may use: Claude subscription models, then Codex Luna/Sol/Astra.
 One provider failing (a live fetch inside the server) falls back to Hermes' static list, never fails both."""
 import re,logging
 from hermes_constants import set_hermes_home_override,reset_hermes_home_override
 from hermes_cli.models import curated_models_for_provider
 token=set_hermes_home_override(home)
 # The server hosts every profile in one process: credential reads need that profile's secret scope,
 # installed the way Hermes' own profile endpoints do, or they raise UnscopedSecretError.
 scope=None
 try:
  from agent.secret_scope import build_profile_secret_scope,set_secret_scope
  scope=set_secret_scope(build_profile_secret_scope(Path(home)),profile_home=str(home))
 except Exception:pass
 try:
  out={}
  for provider,family in ROUTE_FAMILIES.items():
   try:ids=[m for m,_ in curated_models_for_provider(provider)]
   except Exception as exc:
    logging.getLogger('jev_routing').warning('jev-routing catalog %s failed: %s',provider,type(exc).__name__)
    try:
     from hermes_cli.models_catalog_static import _PROVIDER_MODELS
     ids=list(_PROVIDER_MODELS.get(provider,[]))
    except Exception:ids=[]
   # Supported catalog, not an entitlement claim; native switch still validates credentials/access.
   out[provider]=[m for m in ids if re.fullmatch(family,m)]
  return out
 finally:
  if scope is not None:
   from agent.secret_scope import reset_secret_scope
   reset_secret_scope(scope)
  reset_hermes_home_override(token)

def engine(home):
 home=Path(home).resolve()
 with _LOCK:
  if home not in _ENGINES:
   cached={'at':0,'ids':[]}
   def models():
    if time.monotonic()-cached['at']>300:
     cached['ids']=catalog(home);cached['at']=time.monotonic()
    return {k:list(v) for k,v in cached['ids'].items()}
   _ENGINES[home]=Engine(home,lambda:config(home),models)
  return _ENGINES[home]

def session_baseline(home,sid,auth_user):
 identifier(sid)
 try:
  with sqlite3.connect(f'file:{Path(home)/"state.db"}?mode=ro',uri=True,timeout=.1) as db:
   row=db.execute('SELECT model,model_config,user_id FROM sessions WHERE id=? AND ended_at IS NULL',(sid,)).fetchone()
 except sqlite3.Error:raise RoutingError('session_unavailable',503)
 if not row:raise RoutingError('session_not_found',404)
 # Match Hermes' persisted provider:user principal. Legacy ownerless sessions are dashboard-managed.
 if row[2] and row[2]!=auth_user:raise RoutingError('session_not_found',404)
 cfg=config(home);stored=json.loads(row[1] or '{}');model=stored.get('model') or row[0] or cfg.get('model',{}).get('default')
 provider=stored.get('provider') or cfg.get('model',{}).get('provider')
 effort=(stored.get('reasoning_config') or {}).get('effort') or cfg.get('agent',{}).get('reasoning_effort','low')
 return {'provider':provider,'model':model,'effort':effort}

def idle(home,sid):
 try:
  with sqlite3.connect(f'file:{Path(home)/"state.db"}?mode=ro',uri=True,timeout=.1) as db:
   cols={r[1] for r in db.execute('PRAGMA table_info(session_turn_leases)')}
   column='conversation_id' if 'conversation_id' in cols else 'session_id'
   if column not in cols:raise RoutingError('idle_check_unavailable',503)
   root=sid
   for _ in range(100):
    row=db.execute('SELECT parent_session_id FROM sessions WHERE id=?',(root,)).fetchone()
    if not row or not row[0]:break
    parent=db.execute('SELECT end_reason FROM sessions WHERE id=?',(row[0],)).fetchone()
    if not parent or parent[0]!='compression':break
    root=row[0]
   if db.execute(f'SELECT 1 FROM session_turn_leases WHERE {column}=? AND expires_at>?',(root,time.time())).fetchone():raise RoutingError('session_busy',409)
 except sqlite3.Error:raise RoutingError('idle_check_unavailable',503)

def guard(home,tool_name='',session_id='',turn_id='',args=None,**kw):
 if not Store(home).path.exists():return None
 try:
  store=Store(home)
  if store.active_small(session_id,turn_id) and tool_name not in ('web_search','web_extract','read_file'):
   from .routing_v2 import ENGINES
   e=ENGINES.get(Path(home).resolve())
   if e is not None and e.escalate(session_id,turn_id):
    return {'action':'block','message':'This step needs a larger model, so this turn now continues on the Medium model. Call the tool again. No tool was executed.'}
   return {'action':'block','message':'This Small-model turn cannot run this tool. Stop and tell the user this step needs a larger model; their next message starts on Medium. No tool was executed.'}
  if store.managed(session_id) and store.model_enabled(session_id) and any(word in tool_name.lower() for word in ('delegate','subagent','spawn')) and small_family(json.dumps(args or {})):
   return {'action':'block','message':'Automatic coding delegates must use Medium or Big, not Luna or Haiku. Choose a configured coding model.'}
 except Exception:
  return {'action':'block','message':'Routing safety state is unavailable. Retry after the routing service recovers.'}
 return None
