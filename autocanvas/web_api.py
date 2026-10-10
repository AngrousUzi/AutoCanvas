"""Browser-facing adapters. Processing modules have no dependency on this module."""
import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from urllib.parse import unquote
from aiohttp import web
from .execution import blocking
from .flows import lecture_key
from .types import AuthenticationRequired
from .progress import execution_progress


def install(app, service):
    store, root = service.store, service.settings.root

    async def attendance(request):
        if request.method == 'PATCH':
            return web.json_response(service.attendance.configure(await request.json()))
        return web.json_response(service.attendance.status())

    async def classroom(request):
        from .classroom_events import classroom_events
        return web.json_response(await blocking(classroom_events, root))

    async def progress(request):
        row = store.execution(request.match_info['id'])
        if not row:
            raise web.HTTPNotFound()
        return web.json_response(await blocking(execution_progress, root, row))

    async def settings(request):
        config = service.configuration
        if request.method == 'PATCH':
            body = await request.json()
            return web.json_response(config.save(body['values'], body['revision']))
        return web.json_response(config.snapshot())

    async def auth(request):
        client = service.browser_auth
        action = request.match_info['action']
        body = await request.json() if request.can_read_body else {}
        try:
            if action == 'status' and request.method == 'GET':
                result = await blocking(client.status)
            elif action == 'automatic' and request.method == 'GET':
                result = await blocking(client.automatic_status)
            elif action == 'automatic' and request.method == 'POST':
                result = await blocking(client.configure_automatic, body['username'], body['password'])
            elif action == 'automatic-test' and request.method == 'POST':
                result = await blocking(client.test_automatic)
            elif action == 'automatic-disable' and request.method == 'POST':
                result = await blocking(client.disable_automatic)
            elif action == 'automatic' and request.method == 'DELETE':
                result = await blocking(client.disable_automatic, forget=True)
            elif action == 'challenge' and request.method == 'POST':
                result = await blocking(client.begin)
            elif action == 'login' and request.method == 'POST':
                result = await blocking(client.submit, body['challenge_id'], body['username'], body['password'], body.get('captcha', ''))
            elif action == 'import' and request.method == 'POST':
                result = await blocking(client.import_session, body['cookies'])
            elif action == 'logout' and request.method == 'POST':
                await blocking(client.logout)
                result = {'authenticated': False}
            else:
                raise web.HTTPNotFound()
            if action in ('login', 'import', 'automatic', 'automatic-test') and request.method == 'POST' and result.get('authenticated'):
                result['recovered_executions'] = await service.recover_authentication(authenticated=True)
            return web.json_response(result)
        except AuthenticationRequired:
            return web.json_response({'error': '登录未完成，请查看自动登录状态；若凭据被拒绝，请修改 credentials.yml'}, status=401)

    async def sources(request):
        lecture = store.get('lectures', lecture_key(request.query['course_id'], request.query.get('kind', 'vod'), request.query['lecture_id']))
        if not lecture:
            raise web.HTTPNotFound()
        rows = await blocking(service.catalog.sources, lecture)
        return web.json_response([{'view': row.view, **({'url': row.location} if request.query.get('show_urls') == '1' else {})} for row in rows])

    async def upload(request):
        if request.method == 'GET':
            return web.json_response(store.list('uploads'))
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != 'file' or not part.filename:
            raise ValueError('请选择媒体文件')
        name = Path(unquote(part.filename).replace("\\", "/")).name
        suffix = Path(name).suffix.lower()
        if suffix not in {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.mp3', '.wav', '.m4a', '.flac', '.ogg', '.aac'}:
            raise ValueError('不支持此媒体文件格式')
        key = uuid.uuid4().hex
        folder = root/'uploads'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(key+suffix)
        size = 0
        try:
            with path.open('xb') as handle:
                path.chmod(0o600)
                while chunk := await part.read_chunk(1024*1024):
                    size += len(chunk)
                    if size > 2*1024**3:
                        raise web.HTTPRequestEntityTooLarge(max_size=2*1024**3, actual_size=size)
                    await blocking(handle.write, chunk)
            if not size:
                raise ValueError('文件为空')
            row = {'id': key, 'name': name, 'file': path.name, 'size': size, 'created': time.time()}
            store.put('uploads', key, row)
            return web.json_response(row, status=201)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def sync_assignments(request):
        body = await request.json() if request.can_read_body else {}
        course = str(body.get('course_id', '*'))
        if course != '*' and not store.get('courses', course):
            raise web.HTTPNotFound()
        ids = service.request_assignments(course)
        result = {'execution_ids': ids}
        if course != '*' and ids:result['execution_id'] = ids[0]
        return web.json_response(result, status=202)

    async def local(request):
        body = await request.json()
        kind = request.match_info['kind']
        if kind not in ('asr', 'slides') or not store.get('uploads', body['upload_id']):
            raise ValueError('未知文件或处理类型')
        options = {**duration_options(body), 'upload_id': body['upload_id']}
        run_id = store.enqueue('local_'+kind, 'local', uuid.uuid4().hex, options=options)
        return web.json_response({'execution_id': run_id}, status=202)

    async def sample(request):
        body = await request.json()
        kind = request.match_info['kind']
        if kind not in ('asr', 'slides'):
            raise web.HTTPNotFound()
        options = duration_options(body, required=True)
        options['output_key'] = uuid.uuid4().hex
        if body.get('view'):
            options['view'] = str(body['view'])
        run_id = service.enqueue_processing(str(body['course_id']), str(body['lecture_id']), 'sample_'+kind, options, force=True)
        return web.json_response({'execution_id': run_id}, status=202)

    def safe_file(raw):
        path = (root/raw).resolve()
        allowed = any(path.is_relative_to((root/base).resolve()) for base in ('outputs', 'samples', 'assignments'))
        if not allowed or not path.is_file():
            raise web.HTTPNotFound()
        return path

    async def files(request):
        def listing():
            rows = []
            for base in ('outputs', 'samples', 'assignments'):
                for path in (root/base).rglob('*'):
                    if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to((root/base).resolve()):
                        stat = path.stat()
                        rows.append({'path': str(path.relative_to(root)), 'name': path.name, 'size': stat.st_size, 'modified': stat.st_mtime})
            return sorted(rows, key=lambda row: row['modified'], reverse=True)
        return web.json_response(await blocking(listing))

    async def file(request):
        path = safe_file(request.query['path'])
        response = web.FileResponse(path)
        if request.query.get('download') == '1' or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.txt', '.json', '.jsonl'):
            response.headers['Content-Disposition'] = 'attachment'
            response.content_type = 'application/octet-stream'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def log_tail():
        path = root/'logs'/'service.log'
        if not path.exists():
            return ''
        with path.open('rb') as handle:
            handle.seek(max(0, path.stat().st_size-64000))
            return handle.read().decode('utf-8', errors='replace')

    async def logs(request):
        return web.json_response({'text': await blocking(log_tail)})

    closing = asyncio.Event()

    async def shutdown(app):
        closing.set()

    async def events(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
        await response.prepare(request)
        try:
            while not closing.is_set():
                payload = {'executions': store.executions(), 'active': len(service.active), 'automation': store.get('control', 'automation', {'paused': False})}
                await response.write(('event: state\ndata: '+json.dumps(payload, ensure_ascii=False)+'\n\n').encode())
                try:
                    await asyncio.wait_for(closing.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        except (ConnectionError, asyncio.CancelledError):
            pass
        return response

    async def cleanup(app):
        if hasattr(service, 'browser_auth'):
            service.browser_auth.close()

    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    app.router.add_get('/api/executions/{id}/progress', progress)
    app.router.add_post('/api/assignments/sync', sync_assignments)
    app.router.add_get('/api/settings', settings)
    app.router.add_patch('/api/settings', settings)
    app.router.add_route('*', '/api/auth/{action}', auth)
    app.router.add_get('/api/sources', sources)
    app.router.add_get('/api/uploads', upload)
    app.router.add_post('/api/uploads', upload)
    app.router.add_post('/api/local/{kind}', local)
    app.router.add_post('/api/sample/{kind}', sample)
    app.router.add_get('/api/classroom-events', classroom)
    app.router.add_get('/api/attendance', attendance)
    app.router.add_patch('/api/attendance', attendance)
    app.router.add_get('/api/files', files)
    app.router.add_get('/api/file', file)
    app.router.add_get('/api/logs', logs)
    app.router.add_get('/api/events', events)


def duration_options(body, required=False):
    value = body.get('duration')
    if value is None and not required:
        return {}
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
        raise ValueError('时长必须在 0–86400 秒之间')
    return {'duration': value}
