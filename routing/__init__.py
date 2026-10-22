"""Model Routing (the retired ``jev-routing`` plugin, now an optional module of ``agents-cs``).

Server-side model and effort routing per turn, configurable slots, a coding floor and read-only Jev reviews.
Off unless configured (see ``settings.py``); the root ``__init__`` only calls ``register`` for a profile that
has it. Loaded separately in each profile; its heavy parts import lazily inside the process that uses them."""
import json
from pathlib import Path


def register(ctx):
    from . import jev
    from . import runtime_v2
    from .routing_v2 import Store
    from .. import paths
    home = paths.hermes_home()
    workspace = (paths.config() or {}).get('terminal', {}).get('cwd', str(home))
    def handle(args, **kwargs):
        if Path(paths.hermes_home()).resolve() != Path(home).resolve():
            return json.dumps({'status': 'fallback', 'reason': 'profile_scope'})
        return json.dumps(jev.run(args, home=home, workspace=workspace), ensure_ascii=False)
    engine = runtime_v2.engine(home)
    def scoped(callback):
        def run(**kwargs):
            if Path(paths.hermes_home()).resolve() != Path(home).resolve():
                return None
            try:
                return callback(**kwargs)
            except Exception:
                return None  # Fail open without logging private provider payloads.
        return run
    ctx.register_hook('pre_llm_call', scoped(engine.prepare_native))
    ctx.register_hook('pre_tool_call', scoped(lambda **kw: runtime_v2.guard(home, **kw)))
    ctx.register_hook('on_session_end', scoped(engine.end_native))
    ctx.register_hook('api_request_error', scoped(engine.request_failed))
    def request_v2(request=None, **kw):
        if Store(home).managed(kw.get('session_id','')):
            Store(home).record_request(kw.get('session_id',''),kw.get('turn_id',''),kw.get('model',''),kw.get('provider',''),(request or {}).get('reasoning',{}).get('effort'))
        return engine.native_request(request=request, **kw)
    ctx.register_middleware('llm_request', scoped(request_v2))
    ctx.register_tool(name='jev_review', toolset='jev', schema={
        'name': 'jev_review',
        'description': 'Cheap read-only Jev classification or passage selection BEFORE loading large data into context. Source must be a file in your workspace. Use for retrieval, inbox/alert triage and transaction categories. Read jev-routing skill. Uncertain/error results require normal review. Never authorizes actions.',
        'parameters': {'type': 'object', 'properties': {
            'source_path': {'type': 'string', 'description': 'Absolute .json [{id,text}], .txt, .md or .log path; originals preserved.'},
            'mode': {'type': 'string', 'enum': ['select', 'inbox', 'alert', 'transaction']},
            'query': {'type': 'string', 'description': 'One narrow relevance question or classification context.'}},
            'required': ['source_path', 'mode', 'query'], 'additionalProperties': False}},
        handler=handle, description='Jev selection and classification')
    ctx.register_skill('jev-routing', Path(__file__).parent / 'skills' / 'jev-routing' / 'SKILL.md',
                       description='Use Jev before reading large retrieved text, or for bounded inbox, alert and transaction classification.')
