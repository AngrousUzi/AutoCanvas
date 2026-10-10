import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from autocanvas.attendance import LatestAttendance, parse_code
from autocanvas.storage import Store


def url(token='OLD', course='10001', history='history'):
    return f'https://mlearning.sjtu.edu.cn/lms/mobile2/forscan/?courseCode={course}&rollCallToken={token}&signHistoryId={history}'


class Client:
    def __init__(self):
        self.sent=[];self.preparing=asyncio.Event();self.ready=asyncio.Event();self.ready.set()
        self.result={'status':'succeeded','message':'ok'}
    async def prepare(self):
        self.preparing.set();await self.ready.wait()
    async def submit(self, code):
        self.sent.append(code)
        if isinstance(self.result,Exception):raise self.result
        return self.result


class AttendanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'state.sqlite3')
        self.client=Client();self.enabled=True
        self.worker=LatestAttendance(self.client,self.store,lambda c:self.enabled,sign_delay=0)
    async def asyncTearDown(self):self.tmp.cleanup()
    def offer(self,token='OLD',age=0):
        self.worker.offer('view',[{'content':url(token)}],'10001','lecture',time.monotonic()-age)

    async def test_default_delay_is_three_seconds(self):
        worker=LatestAttendance(self.client,self.store,lambda c:True)
        self.assertEqual(worker.sign_delay,3.0)

    async def test_delay_reselects_rotating_code_without_resetting_deadline(self):
        self.worker.sign_delay=.08
        self.offer()
        first=self.worker.first_seen['10001:history']
        task=asyncio.create_task(self.worker.dispatch())
        await self.client.preparing.wait()
        await asyncio.sleep(.02)
        self.assertFalse(self.client.sent)
        self.offer('NEW')
        self.assertEqual(self.worker.first_seen['10001:history'],first)
        await asyncio.wait_for(task,1)
        self.assertGreaterEqual(time.monotonic()-first,.08)
        self.assertEqual([c['token'] for c in self.client.sent],['NEW'])
        self.offer('AGAIN');await self.worker.dispatch()
        self.assertEqual(len(self.client.sent),1)

    async def test_delay_discards_missing_stale_or_disabled_code(self):
        for reason in ('missing','stale','disabled'):
            with self.subTest(reason=reason):
                self.enabled=True
                self.worker.first_seen.clear()
                self.worker.sign_delay=.04
                self.offer()
                task=asyncio.create_task(self.worker.dispatch())
                await asyncio.sleep(.01)
                if reason=='missing':self.worker.offer('view',[],'10001','lecture',time.monotonic())
                elif reason=='stale':self.worker.latest['view'][0]['received']-=3
                else:self.enabled=False
                await asyncio.wait_for(task,1)
                self.assertFalse(self.client.sent)

    async def test_new_rollcall_during_delay_gets_its_own_delay(self):
        self.worker.sign_delay=.06
        self.offer()
        task=asyncio.create_task(self.worker.dispatch())
        await asyncio.sleep(.02)
        self.worker.offer('view',[{'content':url('NEW',history='next')}],'10001','lecture',time.monotonic())
        started=self.worker.first_seen['10001:next']
        await asyncio.wait_for(task,1)
        self.assertGreaterEqual(time.monotonic()-started,.06)
        self.assertEqual([c['history'] for c in self.client.sent],['next'])

    async def test_reselects_latest_after_login_wait(self):
        self.client.ready.clear();self.offer()
        task=asyncio.create_task(self.worker.dispatch());await self.client.preparing.wait()
        self.offer('NEW');self.client.ready.set();await task
        self.assertEqual([c['token'] for c in self.client.sent],['NEW'])

    async def test_new_frame_without_qr_invalidates_pending_code(self):
        self.client.ready.clear();self.offer()
        task=asyncio.create_task(self.worker.dispatch());await self.client.preparing.wait()
        self.worker.offer('view',[],'10001','lecture',time.monotonic())
        self.client.ready.set();await task
        self.assertFalse(self.client.sent)

    async def test_stale_and_disabled_never_send(self):
        self.offer(age=3);await self.worker.dispatch();self.assertFalse(self.client.sent)
        self.offer();self.enabled=False;await self.worker.dispatch();self.assertFalse(self.client.sent)

    async def test_ages_out_during_authentication(self):
        self.client.ready.clear();self.offer()
        task=asyncio.create_task(self.worker.dispatch());await self.client.preparing.wait()
        self.worker.latest['view'][0]['received']-=3
        self.client.ready.set();await task;self.assertFalse(self.client.sent)

    async def test_disable_during_authentication(self):
        self.client.ready.clear();self.offer()
        task=asyncio.create_task(self.worker.dispatch());await self.client.preparing.wait()
        self.enabled=False;self.client.ready.set();await task;self.assertFalse(self.client.sent)

    async def test_success_deduplicates_rotating_code_for_same_rollcall(self):
        self.offer();await asyncio.gather(self.worker.dispatch(),self.worker.dispatch())
        self.offer('ROTATED');await self.worker.dispatch()
        self.assertEqual(len(self.client.sent),1)
        self.assertNotIn('token',self.store.list('attendance')[0])

    async def test_expired_waits_for_different_fresh_token(self):
        self.client.result={'status':'expired'};self.offer();await self.worker.dispatch()
        self.offer();await self.worker.dispatch();self.assertEqual(len(self.client.sent),1)
        self.offer('NEW');await self.worker.dispatch();self.assertEqual(len(self.client.sent),2)

    async def test_unknown_network_result_not_retried_even_with_rotated_code(self):
        self.client.result=TimeoutError();self.offer();await self.worker.dispatch()
        self.offer('NEW');await self.worker.dispatch()
        self.assertEqual(len(self.client.sent),1)
        self.assertEqual(self.store.list('attendance')[0]['status'],'unknown')

    async def test_restart_marks_pending_unknown(self):
        self.store.put('attendance','10001:history',{'id':'10001:history','status':'submitting'})
        task=asyncio.create_task(self.worker.run());await asyncio.sleep(.02)
        self.offer('NEW');await asyncio.sleep(.02);task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(self.store.get('attendance','10001:history')['status'],'unknown')
        self.assertFalse(self.client.sent)

    def test_strict_target_and_course_validation(self):
        self.assertIsNotNone(parse_code(url(),'10001'))
        for value in [url().replace('mlearning.sjtu.edu.cn','evil.example'),url(course='1'),url(token='../bad'),url()+'&courseCode=10001',url().replace('https:','http:')]:
            self.assertIsNone(parse_code(value,'10001'))

    async def test_observer_discards_decode_when_newer_frame_arrives(self):
        from autocanvas.live_attendance import LiveAttendance
        from autocanvas.types import MediaSource
        from threading import Event
        started=Event();release=Event()
        class Detector:
            def detect_bytes(self,data):
                if data==b'old':started.set();release.wait(2)
                return [{'content':url(data.decode())}]
        new_frame=asyncio.Event()
        async def reader(source):
            yield b'old'
            while not started.is_set():await asyncio.sleep(.001)
            yield b'new'
            new_frame.set()
            await asyncio.Event().wait()
        observer=LiveAttendance(self.store,Path(self.tmp.name),None,self.client,reader=reader,detector_factory=Detector)
        observer.worker=self.worker
        task=asyncio.create_task(observer.observe({'course_id':'10001','id':'lecture'},MediaSource('fixture'),0))
        await new_frame.wait();release.set()
        for _ in range(100):
            if self.worker.fresh():break
            await asyncio.sleep(.01)
        self.assertEqual(self.worker.fresh()['token'],'new')
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(self.worker.latest)

    async def test_http_response_validation_and_redaction(self):
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from unittest.mock import patch
        from autocanvas.attendance import MobileLogin
        replies=[{'resultCode':50000,'resultMessage':'rollcall_token:SECRET不存在！'},
                 {'resultCode':200,'body':{'status':'NORMAL'}},
                 {'resultCode':10001}, {'resultCode':200,'body':{'status':'EXPIRED'}},
                 {'resultCode':50000,'resultMessage':'请开启定位'},
                 {'resultCode':200,'body':{'status':'NORMAL'},'resultMessage':'签到成功，请开启定位 SECRET'},
                 {'resultCode':200,'body':{'status':'EXISTED'},'resultMessage':'已签到，location permission denied'},
                 {'resultCode':200,'body':{'status':'EXPIRED'},'resultMessage':'请开启定位'},
                 {'resultCode':200,'body':{'status':'LOCATION_REQUIRED'},'resultMessage':'请开启定位'}]
        async def reply(request):
            self.assertEqual(request.headers['Authorization'],'fixture-token')
            return web.json_response(replies.pop(0))
        app=web.Application();app.router.add_get('/lms-lti-rollcall-sjtu/sign/scan/{token}/{history}',reply)
        server=TestServer(app);await server.start_server()
        try:
            with patch('autocanvas.attendance.HOST',str(server.make_url('')).rstrip('/')):
                client=MobileLogin(None)
                for index, expected in enumerate(['expired','succeeded','needs_login','expired','needs_action','succeeded','succeeded','expired','needs_action']):
                    client.token='fixture-token'
                    result=await client.submit(parse_code(url('SECRET'),'10001'))
                    self.assertEqual(result['status'],expected)
                    self.assertNotIn('SECRET',str(result))
                    if index in (4,5,6):self.assertTrue(result['location_reminder'])
                    if index in (5,6):self.assertIn('已确认成功', result['message'])
        finally:await server.close()

    async def test_real_video_frames_decode_and_stop_without_pipe_deadlock(self):
        import cv2
        import subprocess
        from autocanvas.media import live_frames
        from autocanvas.types import MediaSource
        from autocanvas.qr import QRDetector
        folder=Path(self.tmp.name)
        qr=cv2.QRCodeEncoder_create().encode(url())
        qr=cv2.copyMakeBorder(qr,4,4,4,4,cv2.BORDER_CONSTANT,value=255)
        qr=cv2.resize(qr,(600,600),interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(str(folder/'qr.png'),qr)
        subprocess.run(['ffmpeg','-nostdin','-y','-hide_banner','-loglevel','error','-loop','1','-i',str(folder/'qr.png'),
                        '-t','5','-r','10','-c:v','libx264','-preset','ultrafast','-g','10','-f','mpegts',str(folder/'live.ts')],check=True)
        reader=live_frames(MediaSource(str(folder/'live.ts')))
        try:
            frame=await asyncio.wait_for(anext(reader),5)
            self.assertEqual(QRDetector().detect_bytes(frame)[0]['content'],url())
        finally:await asyncio.wait_for(reader.aclose(),8)
