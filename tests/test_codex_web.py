import asyncio
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from dashboard_gateway.app import create_app


class CodexWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        (self.workspace / 'AGENTS.md').write_text('Existing user instructions')
        self.home = self.root / 'codex'; self.home.mkdir()
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.home)})
        self.env.start()
        self.config = self.root / 'homework.yml'
        self.config.write_text(yaml.safe_dump({'workspace': str(self.workspace), 'codex': str(Path('tests/homework_fake_cli.py').resolve())}))
        dist = self.root / 'dist'; dist.mkdir(); (dist / 'index.html').write_text('test')
        self.app = create_app({'password': 'test-password-1234', 'dist': str(dist), 'codex_config': str(self.config)})
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.assertEqual((await self.client.get('/api/codex/sessions')).status, 401)
        await self.client.post('/_dashboard/login', json={'password':'test-password-1234'})

    async def asyncTearDown(self):
        await self.client.close(); self.env.stop(); self.tmp.cleanup()

    async def request(self, path, body=None, method='POST', expected=200):
        response = await self.client.request(method, '/api/codex'+path, json=body)
        value = await response.json(); self.assertEqual(response.status, expected, value)
        return value

    async def new(self):
        return await self.request('/sessions', {}, expected=201)

    async def send(self, sid, text):
        return await self.request('/sessions/'+sid+'/turns', {'prompt':text, 'request_id':uuid.uuid4().hex}, expected=202)

    async def wait(self, sid):
        for _ in range(160):
            rows = await self.request('/sessions/'+sid+'/turns', method='GET')
            if rows and rows[-1]['status'] not in ('queued','running'):return rows[-1]
            await asyncio.sleep(.05)
        self.fail('Execution timed out')

    async def test_exec_resume_model_and_exact_directory_without_assignment(self):
        session = await self.new(); sid = session['id']
        self.assertNotIn('assignment_id', session)
        await self.send(sid, 'FIRST')
        self.assertEqual((await self.wait(sid))['status'], 'succeeded')
        rows = await self.request('/sessions', method='GET'); native = rows[0]['native_id']
        await self.request('/sessions/'+sid, {'model':'fixture-model'}, method='PATCH')
        await self.send(sid, 'SECOND')
        self.assertEqual((await self.wait(sid))['status'], 'succeeded')
        rows = await self.request('/sessions', method='GET')
        self.assertEqual(rows[0]['native_id'], native)
        self.assertEqual(rows[0]['model'], 'fixture-model')
        self.assertEqual((self.workspace/'result.txt').read_text(), 'FIRST\nSECOND\n')
        self.assertEqual((self.workspace/'AGENTS.md').read_text(), 'Existing user instructions')
        calls=[json.loads(line) for line in (self.workspace/'fixture-invocations.jsonl').read_text().splitlines()]
        for call in calls:
            self.assertEqual(call['cwd'], str(self.workspace))
            self.assertEqual(call['agents'], 'Existing user instructions')
            self.assertEqual(call['argv'][call['argv'].index('--cd')+1],str(self.workspace))
        self.assertIn('fixture-model', calls[-1]['argv'])
        self.assertIn(native, calls[-1]['argv'])
        self.assertFalse(list(self.workspace.glob('**/.homework-context')))
        events = await self.request('/sessions/'+sid+'/events', method='GET')
        self.assertTrue(any(e['kind']=='message' for e in events))
        self.assertFalse(any(e['kind']=='files' for e in events))

    async def test_directory_change_and_stop(self):
        s = await self.new(); sid=s['id']
        await self.send(sid, 'WAIT')
        await asyncio.sleep(.3)
        target = self.root/'other';target.mkdir()
        await self.request('/config', {'workspace':str(target)}, method='PATCH', expected=400)
        await self.request('/sessions/'+sid, {'model':'other'}, method='PATCH', expected=400)
        await self.request('/sessions/'+sid+'/stop')
        self.assertEqual((await self.wait(sid))['status'],'cancelled')
        await self.request('/config', {'workspace':str(target)}, method='PATCH')
        await self.send(sid,'NEW DIRECTORY')
        self.assertEqual((await self.wait(sid))['status'],'succeeded')
        self.assertEqual((target/'result.txt').read_text(),'NEW DIRECTORY\n')
        self.assertEqual(yaml.safe_load(self.config.read_text())['workspace'],str(target))
        self.assertFalse((target/'.homework-manager').exists())
        from homework_service.config import Settings
        self.assertEqual(Settings.load(self.config).state, self.workspace/'.homework-manager')

    async def test_native_sessions_in_configured_directory_are_visible(self):
        folder=self.home/'sessions';folder.mkdir()
        native=str(uuid.uuid4())
        events=[{'type':'session_meta','payload':{'id':native,'cwd':str(self.workspace)}},
                {'timestamp':'2020-01-01T00:00:00Z','type':'event_msg','payload':{'type':'user_message','message':'Existing conversation'}},
                {'timestamp':'2020-01-01T00:00:01Z','type':'event_msg','payload':{'type':'agent_message','message':'Existing reply'}}]
        (folder/'rollout-example.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        rows=await self.request('/sessions',method='GET')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['native_id'],native)
        messages=await self.request('/sessions/'+rows[0]['id']+'/history',method='GET')
        self.assertEqual([m['text'] for m in messages],['Existing conversation','Existing reply'])

    async def test_native_response_messages_and_instruction_filter(self):
        from homework_service.web import message
        self.assertIsNone(message({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'# AGENTS.md instructions for /workspace'},{'type':'input_text','text':'<environment_context>metadata'}]}}))
        row = message({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'My actual request'}]}})
        self.assertEqual(row, {'role':'user','text':'My actual request'})

    async def test_model_catalog_queries_cli_and_refresh_does_not_reuse_old_list(self):
        from unittest.mock import AsyncMock
        with patch('homework_service.web.list_models',new_callable=AsyncMock) as query:
            query.return_value=['current-model']
            first=await self.request('/models',method='GET')
            self.assertEqual(first['models'],['current-model'])
            await self.request('/models',method='GET')
            self.assertEqual(query.await_count,1)
            query.return_value=['new-model']
            updated=await self.request('/models/refresh',{})
            self.assertEqual(updated['models'],['new-model'])
            self.assertEqual(query.await_count,2)
            query.side_effect=ValueError('模型查询超时')
            await self.request('/models/refresh',{},expected=502)
            await self.request('/models',method='GET',expected=502)
            self.assertEqual(query.await_count,4)
