import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from aiohttp.test_utils import TestServer, TestClient
from autocanvas.config import Settings
from autocanvas.storage import Store
from autocanvas.service import Service
from autocanvas.http import create_app
from autocanvas.types import AuthenticationRequired


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=Settings(root=Path(self.tmp.name),schedule_interval=1)
        self.store=Store(self.settings.root/'state.sqlite3')
        self.store.put('courses','1',{'id':'1','active':True})
        self.calls=[]
        async def replay(lecture,kind,options):
            self.calls.append(kind)
            if options.get('auth_error'):raise AuthenticationRequired()
            if options.get('wait'):await asyncio.sleep(60)
            return Path('artifact')
        self.service=Service(self.settings,self.store,None,None,SimpleNamespace(run=replay),None)
        self.lecture={'id':'2','course_id':'1','kind':'vod'}
        self.store.put('lectures','1:vod:2',self.lecture)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def wait_status(self,run,status):
        async with asyncio.timeout(3):
            while self.store.execution(run)['status'] != status:
                await asyncio.sleep(.01)

    async def test_cancel_does_not_kill_worker(self):
        run=self.service.enqueue_processing('1','2','vod_asr',{'wait':True})
        await self.service.start(automation=False)
        await self.wait_status(run,'running')
        self.service.cancel(run)
        await asyncio.sleep(.05)
        second=self.store.enqueue('vod_asr','1','3')
        self.store.put('lectures','1:vod:3',{**self.lecture,'id':'3'})
        await self.wait_status(second,'succeeded')
        self.assertEqual(self.store.execution(run)['status'],'cancelled')

    async def test_auth_failure_does_not_block_other_processing(self):
        bad=self.service.enqueue_processing('1','2','vod_asr',{'auth_error':True})
        good=self.service.enqueue_processing('1','2','vod_slides')
        await self.service.start(automation=False)
        await self.wait_status(bad,'needs_login')
        await self.wait_status(good,'succeeded')

    async def test_video_transport_failure_retries_without_login_block(self):
        from autocanvas.types import RemoteError
        async def replay(*args):
            raise RemoteError('Video launch transport failed', stage='video_launch', code='Timeout')
        self.service.replay = SimpleNamespace(run=replay)
        run = self.service.enqueue_processing('1', '2', 'vod_asr')
        await self.service._execute(self.store.claim('vod_asr'))
        row = self.store.execution(run)
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['error'], 'RemoteError')
        self.assertGreater(row['due'], time.time())

    async def test_auth_recovery_preserves_pause_cancel_and_course_scope(self):
        blocked = self.store.enqueue('vod_asr', '1', '2', options={'automatic': True})
        self.store.finish(blocked, 'needs_login')
        cancelled = self.store.enqueue('vod_slides', '1', '2')
        self.store.finish(cancelled, 'needs_login')
        self.store.cancel(cancelled)
        other = self.store.enqueue('vod_asr', '3', '4')
        self.store.finish(other, 'needs_login')
        client = Mock()
        def factory(course):
            if course == '3':
                raise AuthenticationRequired()
            return client
        self.service.catalog = SimpleNamespace(video_factory=factory)
        self.store.put('control', 'automation', {'paused': True})
        self.assertEqual(await self.service.recover_authentication(authenticated=True), 1)
        self.assertEqual(await self.service.recover_authentication(authenticated=True), 0)
        self.assertEqual(self.store.execution(blocked)['status'], 'pending')
        self.assertTrue(self.store.execution(blocked)['options']['automatic'])
        self.assertIsNone(self.store.claim('vod_asr', allow_automatic=False))
        self.assertEqual(self.store.execution(cancelled)['status'], 'cancelled')
        self.assertEqual(self.store.execution(other)['status'], 'needs_login')
        client.close.assert_called_once()

    async def test_auth_recovery_expires_finished_live_and_requests_replay(self):
        now = datetime.now(timezone.utc)
        self.store.put('lectures', '1:live:5', {'id':'5', 'course_id':'1', 'kind':'live', 'end':(now-timedelta(minutes=1)).isoformat()})
        run = self.store.enqueue('live', '1', '5', options={'automatic':True})
        self.store.finish(run, 'needs_login')
        await self.service.recover_authentication(authenticated=True)
        self.assertEqual(self.store.execution(run)['status'], 'expired')
        sync = [r for r in self.store.executions() if r['kind'] == 'sync']
        self.assertEqual(len(sync), 1)
        self.assertTrue(sync[0]['options']['automatic'])

    async def test_recovery_cannot_resurrect_cancelled_row(self):
        run = self.store.enqueue('vod_asr', '1', '2')
        self.store.finish(run, 'needs_login')
        self.store.cancel(run)
        self.assertFalse(self.store.recover_auth(run))
        self.assertFalse(self.store.recover_auth(run, expired=True))

    async def test_auth_maintenance_recovers_persisted_blocked_work(self):
        run = self.store.enqueue('vod_asr', '1', '2', options={'automatic': True})
        self.store.finish(run, 'needs_login')
        self.store.put('control', 'automation', {'paused': True})
        # A new service sees only persisted work, as after a restart.
        self.service.browser_auth = SimpleNamespace(
            automatic_status=lambda: {'enabled': True}, status=lambda: {'authenticated': True})
        self.service.catalog = SimpleNamespace(video_factory=lambda course: Mock())
        await self.service.start(automation=False)
        await self.wait_status(run, 'pending')
        self.assertEqual(self.calls, [])

    async def test_shutdown_recovers_interrupted(self):
        run=self.service.enqueue_processing('1','2','vod_asr',{'wait':True})
        await self.service.start(automation=False)
        await self.wait_status(run,'running')
        await self.service.close()
        self.assertEqual(self.store.execution(run)['status'],'pending')

    async def test_tick_idempotent_and_pause(self):
        self.store.put('sync','scheduled',{'at':time.time()})
        now=datetime.now(timezone.utc)
        self.store.put('lectures','1:live:4',{'id':'4','course_id':'1','kind':'live','begin':(now+timedelta(minutes=5)).isoformat(),'end':(now+timedelta(hours=1)).isoformat()})
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(len(self.store.executions()),4)
        self.store.put('control','automation',{'paused':True})
        await self.service.start(automation=False)
        await asyncio.sleep(.1)
        self.assertEqual(self.calls,[])
        manual=self.service.enqueue_processing('1','2','vod_asr',force=True)
        await self.wait_status(manual,'succeeded')

    async def test_http_control_and_validation(self):
        client=TestClient(TestServer(create_app(self.service)))
        await client.start_server()
        try:
            r=await client.get('/health');self.assertEqual(r.status,200)
            r=await client.post('/api/process/vod_asr',json={'course_id':'1','lecture_id':'2'})
            self.assertEqual(r.status,202)
            run=(await r.json())['execution_id']
            r=await client.post(f'/api/executions/{run}/cancel')
            self.assertTrue((await r.json())['changed'])
            r=await client.patch('/api/courses/1/rules',json={'asr':False})
            self.assertEqual((await r.json())['asr'],False)
            r=await client.post('/api/automation',json={'paused':'yes'})
            self.assertEqual(r.status,400)
            r=await client.post('/api/process/vod_asr',json={'course_id':'1','lecture_id':'unknown'})
            self.assertEqual(r.status,400)
            r=await client.get('/plugins');self.assertEqual(r.status,404)
        finally:
            await client.close()


    async def test_assignments_have_independent_half_hour_schedule(self):
        from unittest.mock import patch
        self.settings.auto_asr = self.settings.auto_slides = self.settings.auto_live = False
        self.store.put('sync', 'scheduled', {'at': 10000})
        self.store.put('courses','inactive',{'id':'inactive','active':False})
        with patch('autocanvas.service.time.time', return_value=10000):
            await self.service.tick()
        rows = self.store.executions()
        self.assertEqual([(r['kind'],r['course_id']) for r in rows],[('assignments','1')])
        run=rows[0]['id']; self.store.finish(run,'succeeded')
        with patch('autocanvas.service.time.time', return_value=11799):
            await self.service.tick()
        self.assertEqual(self.store.execution(run)['status'],'succeeded')
        # The persisted timestamp also survives a new Service instance.
        restarted=Service(self.settings,self.store,None,None,None,None)
        with patch('autocanvas.service.time.time', return_value=11800):
            await restarted.tick()
        self.assertEqual(self.store.execution(run)['status'],'pending')
        self.assertEqual(len(self.store.executions()),1)

    async def test_manual_assignment_sync_all_or_one_and_while_paused(self):
        self.store.put('courses','3',{'id':'3','active':True})
        self.store.put('courses','4',{'id':'4','active':False})
        self.store.put('control','automation',{'paused':True})
        await self.service.tick()
        self.assertEqual(self.store.executions(),[])
        client=TestClient(TestServer(create_app(self.service)));await client.start_server()
        try:
            response=await client.post('/api/assignments/sync')
            self.assertEqual(response.status,202)
            ids=(await response.json())['execution_ids'];self.assertEqual(len(ids),2)
            self.assertEqual({self.store.execution(i)['course_id'] for i in ids},{'1','3'})
            response=await client.post('/api/assignments/sync',json={'course_id':'1'})
            result=await response.json();self.assertEqual(result['execution_ids'],[result['execution_id']])
            self.assertEqual(self.store.claim('assignments',allow_automatic=False)['course_id'],'1')
            response=await client.post('/api/assignments/sync',json={'course_id':'missing'})
            self.assertEqual(response.status,404)
        finally:await client.close()
