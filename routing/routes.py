"""The ``routing`` module's router, mounted by ``dashboard/plugin_api.py`` under ``/api/plugins/hermes-bots/``
(``/routing/<op>``). Authenticated Hermes plugin API: routing switches, routed-turn records and (deprecated) one-turn plans; never raw inference."""
import hashlib
import json
import time
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import runtime_v2 as rt
from . import routing_v2 as domain
from . import settings as routing_settings
router=APIRouter()

def identity(request):
 session=getattr(request.state,'session',None)
 if session is not None:
  from hermes_cli.dashboard_auth.base import Session
  if not isinstance(session,Session) or type(session.expires_at)!=int or session.expires_at<=time.time():raise domain.RoutingError('sign_in_required',401)
  parts=[session.provider,session.org_id,session.user_id]
  if any(not isinstance(x,str) or len(x)>512 or any(ord(c)<32 for c in x) for x in parts) or not session.provider or not session.user_id:raise domain.RoutingError('sign_in_required',401)
  return hashlib.sha256(json.dumps(parts).encode()).hexdigest(),f'{session.provider}:{session.user_id}'
 if (getattr(request.app.state,'auth_required',None) is not False or getattr(request.state,'token_authenticated',False) or not request.url.path.startswith('/api/plugins/hermes-bots/routing/')):raise domain.RoutingError('sign_in_required',401)
 token=request.headers.get('X-Hermes-Session-Token','')
 authorization=request.headers.get('Authorization','')
 if token and authorization:raise domain.RoutingError('sign_in_required',401)
 if not token and authorization.startswith('Bearer '):token=authorization[7:]
 if not token or len(token)>32768:raise domain.RoutingError('sign_in_required',401)
 # Hermes' outer mount validates both session-token headers, including the connector's Bearer.
 # API-token principals remain excluded above. Stable identity across local token rotation.
 return 'legacy-dashboard',None

def dispatch(operation,method,body,owner,auth_user):
 agent=body.get('agentId');home=rt.home_for(agent);e=rt.engine(home)
 if operation=='capabilities' and method=='GET':
  domain.exact(body,('agentId',));ids=e.catalog()
  # 2.4: the plugin routes every turn itself. `execution` stays app_one_turn_switch so 2.3 app builds keep
  # working (they still plan/arm/switch, and a matching plan suppresses server routing); new builds read
  # serverRouting and only write the switches.
  return {'schemaVersion':2,'pluginVersion':'2.4.0','independentRoutingControls':True,'crossProviderRoutes':False,'agentId':agent,'execution':'app_one_turn_switch','serverRouting':True,'serverRoutingSurfaces':list(domain.NATIVE_PLATFORMS),'appPlanDeprecated':True,'atomicDispatch':True,'nativeFallback':'selected_model_effort','fallbackBuiltIn':True,'supportedProviders':list(domain.PROVIDERS),'models':[{'provider':p,'model':m,'efforts':['low','medium','high']} for p in domain.PROVIDERS for m in ids.get(p,[])],'catalogSource':'hermes_provider_catalog','entitlementCheckedOnSwitch':False,'deadlineMs':500,'codingFloor':'medium','uncoveredSurfaces':['cron','kanban','third_party_senders','realtime_voice'],'requiresAppIntegration':False,'classifierAvailable':routing_settings.key_file() is not None}
 if operation=='policy':
  if method=='GET':domain.exact(body,('agentId',));return e.policy()
  if method=='PUT':
   return e.put_policy({k:v for k,v in body.items() if k!='agentId'})
 sid=body.get('sessionId');baseline=rt.session_baseline(home,sid,auth_user)
 s=e.store.session(sid,owner,baseline)
 if operation=='session':
  if method=='GET':domain.exact(body,('agentId','sessionId'));return s
  if method=='PUT':
   return e.put_session(sid,owner,{k:v for k,v in body.items() if k not in ('agentId','sessionId')})
 if operation=='turns' and method=='GET':domain.exact(body,('agentId','sessionId'));return {'turns':e.store.turns(sid)}
 # Deprecated (2.4): plan/arm/cancel serve 2.3 app builds only.
 if operation=='plan' and method=='POST':rt.idle(home,sid);return e.plan(sid,owner,body,baseline)
 if operation=='plan' and method=='GET':domain.exact(body,('agentId','sessionId','routeId'));return e.get_plan(sid,owner,body['routeId'])
 if operation=='plan/arm' and method=='POST':
  domain.exact(body,('agentId','sessionId','routeId'));rt.idle(home,sid);return e.transition(sid,owner,body['routeId'],'arm')
 if operation=='plan/cancel' and method=='POST':
  domain.exact(body,('agentId','sessionId','routeId'));return e.transition(sid,owner,body['routeId'],'cancel')
 raise domain.RoutingError('unknown_operation',404)

@router.api_route('/routing/{operation:path}',methods=['GET','PUT','POST'])
async def routing(operation:str,request:Request):
 headers={'Cache-Control':'no-store'}
 try:
  owner,auth_user=identity(request)
  if request.method=='GET':
   if len(request.url.query)>2000:raise domain.RoutingError('request_too_large',413)
   body=dict(request.query_params)
  else:
   raw=bytearray()
   async for chunk in request.stream():
    raw.extend(chunk)
    if len(raw)>40000:raise domain.RoutingError('request_too_large',413)
   try:body=json.loads(raw)
   except (ValueError,UnicodeError):raise domain.RoutingError('invalid_json')
   if not isinstance(body,dict):raise domain.RoutingError('invalid_fields')
  result=await run_in_threadpool(dispatch,operation,request.method,body,owner,auth_user)
  return JSONResponse(result,headers=headers)
 except domain.RoutingError as exc:
  if exc.status>=500 or exc.status==401:trace(operation,request.method,exc.status,exc.code)
  return JSONResponse({'code':exc.code},status_code=exc.status,headers=headers)
 except Exception as exc:
  trace(operation,request.method,503,type(exc).__name__)
  return JSONResponse({'code':'routing_unavailable'},status_code=503,headers=headers)

def trace(operation,method,status,code):
 """One line per refused or failed app call: operation, status and error class only, never bodies."""
 import logging
 try:
  logging.getLogger('jev_routing').warning('jev-routing %s %s -> %s %s',method,operation[:40],status,str(code)[:80])
  path=rt.root()/'logs'/'jev-routing-api.log'
  path.parent.mkdir(parents=True,exist_ok=True)
  with open(path,'a') as out:out.write(json.dumps({'at':int(time.time()),'op':operation[:40],'method':method,'status':status,'code':str(code)[:80]})+'\n')
 except Exception:pass
