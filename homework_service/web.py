"""Small HTTP wrapper around codex exec, mounted in the dashboard process."""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml
from aiohttp import web
from .config import Settings
from .runner import Runner
from .store import Store
from .models import list_models



def message(event):
    body = event.get('payload', {})
    if event.get('type') == 'response_item' and body.get('type') == 'message' and body.get('role') in ('user', 'assistant'):
        parts = [c.get('text', '') for c in body.get('content', []) if c.get('type') in ('input_text', 'output_text')]
        parts = [text for text in parts if not text.startswith(('# AGENTS.md instructions', '<environment_context>', '<permissions instructions>', '<turn_aborted>'))]
        text = '\n'.join(parts).strip()
        return {'role': body['role'], 'text': text} if text else None
    if event.get('type') == 'event_msg' and body.get('type') in ('user_message', 'agent_message'):
        return {'role': 'user' if body['type'] == 'user_message' else 'assistant', 'text': body.get('message', '')}
    return None


def register(app, config_path):
    config_path = Path(config_path)
    settings = Settings.load(config_path)
    # Keep existing history in place even when the user changes the execution directory.
    state_path = settings.state
    store = Store(state_path / 'state.sqlite3')
    runner = Runner(settings, store, None, standalone=True)
    model_cache = {}
    model_lock = asyncio.Lock()
    native_paths = {}
    last_scan = 0
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()

    def session(sid):
        row = store.get('sessions', sid)
        if not row or row['provider'] != 'codex':
            raise web.HTTPNotFound(text='会话不存在')
        return row

    def running(sid=None):
        return [r for r in store.runs(sid) if r['id'] in runner.tasks]

    def discover():
        nonlocal last_scan
        if time.monotonic() - last_scan < 15:
            return
        last_scan = time.monotonic()
        titles = {}
        try:
            for line in (codex_home / 'session_index.jsonl').read_text().splitlines():
                entry = json.loads(line); titles[entry['id']] = entry.get('thread_name', '')
        except (OSError, ValueError, KeyError):pass
        known = {s.get('native_id'): s for s in store.list('sessions') if s['provider'] == 'codex'}
        for path in (codex_home / 'sessions').glob('**/rollout-*.jsonl'):
            try:
                with path.open() as handle:
                    meta = json.loads(handle.readline()).get('payload', {})
                native = str(uuid.UUID(meta['id']))
                cwd = Path(meta['cwd']).expanduser().resolve()
                if cwd != settings.workspace and native not in known:
                    continue
                native_paths[native] = path
                old = known.get(native)
                if old and not old['name'].startswith('Codex · '):
                    continue
                name = titles.get(native) or 'Codex · ' + native[:8]
                with path.open() as handle:
                    for line in handle:
                        row = json.loads(line)
                        item = message(row)
                        if item and item['role'] == 'user':
                            name = titles.get(native) or item['text'][:70]
                            break
                if old:
                    if name != old['name']:
                        old['name'] = name; store.put('sessions', old['id'], old)
                    continue
                sid = uuid.uuid4().hex
                store.put('sessions', sid, dict(id=sid, name=name, provider='codex',
                    native_id=native, started=True, imported=True, paused=False,
                    directory='.', model='', created=path.stat().st_mtime))
                known[native] = store.get('sessions', sid)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def public(row):
        active = running(row['id'])
        return {**row, 'directory': str(settings.workspace),
                'status': 'running' if active else 'idle', 'run_id': active[0]['id'] if active else None}

    async def configuration(request):
        nonlocal last_scan
        if request.method == 'PATCH':
            body = await request.json()
            if running():
                raise ValueError('请先停止正在执行的会话，再修改目录')
            value = body.get('workspace', '')
            if not isinstance(value, str) or not value.strip():
                raise ValueError('请输入会话工作目录')
            target = Path(value).expanduser()
            if not target.is_absolute() or not target.is_dir():
                raise ValueError('请选择已存在的绝对目录')
            values = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
            values = values or {}
            values['workspace'] = str(target.resolve())
            values['state_directory'] = str(state_path)
            config_path.write_text(yaml.safe_dump(values, allow_unicode=True, sort_keys=False))
            settings.workspace = target.resolve()
            last_scan = 0
            model_cache.clear()
        return web.json_response({'workspace': str(settings.workspace), 'default_model': settings.codex_model})

    async def models(request):
        async with model_lock:
            if request.method == 'POST' or time.time()-model_cache.get('updated_at',0)>300:
                try:
                    names=await list_models(settings.codex, settings.workspace)
                except (OSError, ValueError, KeyError) as exc:
                    # Do not silently serve an obsolete list after a failed refresh.
                    model_cache.clear()
                    reason=str(exc) if isinstance(exc,ValueError) else '无法查询 Codex，请检查 CLI 路径和配置'
                    return web.json_response({'error': reason},status=502)
                model_cache.update(models=names,updated_at=time.time(),source='codex model/list')
            return web.json_response(model_cache)

    async def sessions(request):
        if request.method == 'GET':
            await asyncio.to_thread(discover)
            rows = [public(s) for s in store.list('sessions') if s['provider'] == 'codex']
            return web.json_response(sorted(rows, key=lambda s: s['created'], reverse=True))
        body = await request.json()
        if not settings.workspace.is_dir():
            raise ValueError('请先设置已存在的会话工作目录')
        sid = uuid.uuid4().hex
        row = dict(id=sid, provider='codex', name=str(body.get('name') or '新会话')[:120],
                   model=str(body.get('model') or settings.codex_model), directory='.',
                   native_id=None, started=False, imported=False, paused=False, created=time.time())
        return web.json_response(public(store.put('sessions', sid, row)), status=201)

    async def update(request):
        sid = request.match_info['id']; row = session(sid)
        if running(sid):
            raise ValueError('本轮仍在执行，请结束后切换模型')
        body = await request.json()
        model = body.get('model', '')
        if not isinstance(model, str) or len(model) > 200:
            raise ValueError('模型名称无效')
        row['model'] = model.strip()
        return web.json_response(public(store.put('sessions', sid, row)))

    async def turns(request):
        sid = request.match_info['id']; row = session(sid)
        if request.method == 'GET':
            return web.json_response(store.runs(sid))
        body = await request.json()
        existing = next((r for r in store.runs(sid) if r['request_id'] == body.get('request_id')), None)
        if existing:
            return web.json_response(store.enqueue(sid, body.get('request_id'), body.get('prompt')), status=202)
        if running(sid):
            raise ValueError('请等待当前回复完成，或停止后再发送')
        row.update(directory='.', paused=False)
        if row['name'] == '新会话':row['name'] = str(body.get('prompt', '')).strip()[:70] or '新会话'
        store.put('sessions', sid, row)
        run = store.enqueue(sid, str(body.get('request_id', '')), body.get('prompt'))
        store.update_run(run['id'], status='running')
        task = asyncio.create_task(runner.execute(run))
        runner.tasks[run['id']] = task
        task.add_done_callback(lambda t: runner.tasks.pop(run['id'], None))
        return web.json_response(store.run(run['id']), status=202)

    async def events(request):
        sid = request.match_info['id']; session(sid)
        return web.json_response(store.events(sid, max(0, int(request.query.get('after', '0')))))

    async def history(request):
        row = session(request.match_info['id'])
        await asyncio.to_thread(discover)
        path = native_paths.get(row.get('native_id'))
        managed = store.runs(row['id'])
        cutoff = min((r['created'] for r in managed), default=float('inf'))
        def read():
            messages, fallback = [], []
            if path:
                with path.open() as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                            if event.get('timestamp') and datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp() >= cutoff:continue
                            item = message(event)
                            if item:
                                (messages if event['type'] == 'response_item' else fallback).append(item)
                        except (ValueError, TypeError):continue
            return messages or fallback
        return web.json_response(await asyncio.to_thread(read))

    async def stop(request):
        row = session(request.match_info['id'])
        for run in running(row['id']):await runner.stop(run['id'])
        return web.json_response({'stopped': True})

    def endpoint(handler):
        async def wrapped(request):
            try:return await handler(request)
            except (ValueError, TypeError) as exc:return web.json_response({'error': str(exc)}, status=400)
            except web.HTTPException as exc:return web.json_response({'error': exc.text}, status=exc.status)
        return wrapped

    for method, path, handler in [
        ('GET','/config',configuration), ('PATCH','/config',configuration),
        ('GET','/models',models), ('POST','/models/refresh',models),
        ('GET','/sessions',sessions), ('POST','/sessions',sessions),
        ('PATCH','/sessions/{id}',update), ('GET','/sessions/{id}/turns',turns),
        ('POST','/sessions/{id}/turns',turns), ('GET','/sessions/{id}/events',events),
        ('GET','/sessions/{id}/history',history), ('POST','/sessions/{id}/stop',stop),
    ]:app.router.add_route(method, '/api/codex'+path, endpoint(handler))

    async def lifetime(app):
        # Old queued homework tasks must never start implicitly in this UI.
        for run in store.runs():
            if run['status'] in ('running','queued'):
                store.update_run(run['id'], status='interrupted', error='上次执行已中断，请重新发送消息')
        yield
        await runner.close()
    app.cleanup_ctx.append(lifetime)
    return runner
