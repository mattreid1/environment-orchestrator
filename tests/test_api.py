"""Black-box API checks for a packaged orchestrator; no Firecracker guest boots."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import tempfile
import time
import unittest

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, WSServerHandshakeError, web


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class PackagedAPI(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = os.environ.get('ENVIRONMENT_ORCHESTRATOR_BIN')
        if not cls.binary or not Path(cls.binary).is_file():
            raise unittest.SkipTest('Set ENVIRONMENT_ORCHESTRATOR_BIN to the packaged Rust service binary')

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='orchestrator-api-')
        self.folder = Path(self.temporary.name)
        self.state = self.folder/'state'
        self.attempts = self.folder/'unexpected-vm-starts'
        self.port = free_port()
        self.web_port = free_port()
        self.web_url = f'http://127.0.0.1:{self.web_port}'
        self.web_headers = {'Origin':self.web_url, 'X-Environment-UI':'1'}
        slots = []
        for slot in range(2):
            runner = self.folder/f'runner-{slot}'
            (runner/'bin').mkdir(parents=True)
            script = runner/'bin/microvm-run'
            script.write_text('#!/bin/sh\nprintf attempted >> "'+str(self.attempts)+'"\nexit 94\n')
            script.chmod(0o700)
            slots.append({'runner':str(runner), 'firecracker':str(script), 'guest_host':'127.0.0.1',
                'memory_mb':3072, 'profiles':{name:{'runner':str(runner), 'memory_mb':3072} for name in ('swe','frontend','marketing','sales','research')}})
        self.environment = {**os.environ, 'ENVIRONMENT_STATE':str(self.state),
            'ENVIRONMENT_SLOTS':json.dumps(slots), 'ENVIRONMENT_PORT':str(self.port),
            'ENVIRONMENT_WEB_ADDR':f'127.0.0.1:{self.web_port}',
            'ENVIRONMENT_IDLE_SECONDS':'0', 'ENVIRONMENT_HOST_RESERVE_MB':'1000000000'}
        self.logfile = (self.folder/'service.log').open('wb')
        self.process = None
        self.admin = None
        self.client = ClientSession(timeout=ClientTimeout(total=5))
        self.sockets = []
        try:
            await self.start_service()
        except BaseException:
            await self.client.close()
            try:
                await self.stop_service()
            finally:
                self.logfile.close()
                self.temporary.cleanup()
            raise

    async def asyncTearDown(self):
        for websocket in self.sockets:
            await websocket.close()
        await self.client.close()
        await self.stop_service()
        self.logfile.close()
        self.temporary.cleanup()

    async def start_service(self):
        self.process = await asyncio.create_subprocess_exec(self.binary, env=self.environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=self.logfile, stderr=self.logfile)
        self.admin = ClientSession(connector=UnixConnector(path=str(self.state/'control.sock')),
            timeout=ClientTimeout(total=5))
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                self.fail('Service exited during startup: '+(self.folder/'service.log').read_text())
            try:
                async with self.admin.get('http://localhost/workspaces') as response:
                    if response.status == 200:
                        async with self.client.get(self.web_url+'/api/dashboard') as web_response:
                            if web_response.status == 200:
                                return
            except (ClientError, OSError):
                pass
            await asyncio.sleep(.025)
        self.fail('Service did not publish its private API: '+(self.folder/'service.log').read_text())

    async def stop_service(self):
        if self.admin:
            await self.admin.close()
            self.admin = None
        if self.process and self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
                self.fail('Service failed to stop without a running guest')

    async def api(self, path='/workspaces', method='GET', payload=None, expected=200):
        async with self.admin.request(method, 'http://localhost'+path, json=payload) as response:
            body = await response.text()
            self.assertEqual(response.status, expected, body)
            if expected >= 400:
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {'error':body}
            return json.loads(body)

    async def test_paperclip_creation_discovery_lazy_binding_and_auth_expiry(self):
        company='8744dbdb-cdd7-4fee-8b46-3bd6ae6705fe'
        agent='f3ccf705-bd33-4082-a266-3d06da1433b9'
        row={'id':agent,'companyId':company,'name':'Hiring','status':'idle','adapterType':'codex_local','role':'cmo','adapterConfig':{'secret':'not-for-dashboard'}}
        fixture={'rows':[row], 'logins':0, 'session':1}
        async def login(request):
            self.assertEqual(await request.json(), {'email':'test@example.test','password':'fixture-password'})
            fixture['logins']+=1
            response=web.json_response({'token':'not-for-dashboard'})
            response.set_cookie('paperclip-test.session_token',str(fixture['session']))
            return response
        async def agents(request):
            if request.cookies.get('paperclip-test.session_token') != str(fixture['session']):
                return web.json_response({'error':'fixture-secret'},status=401)
            return web.json_response(fixture['rows'])
        app=web.Application()
        app.router.add_post('/api/auth/sign-in/email',login)
        app.router.add_get(f'/api/companies/{company}/agents',agents)
        runner=web.AppRunner(app)
        await runner.setup()
        port=free_port()
        await web.TCPSite(runner,'127.0.0.1',port).start()
        try:
            login_file=self.folder/'login.json'
            login_file.write_text(json.dumps({'email':'test@example.test','password':'fixture-password'}))
            login_file.chmod(0o600)
            config=self.state/'paperclip.json'
            config.write_text(json.dumps({'base_url':f'http://127.0.0.1:{port}','login_file':str(login_file),'companies':[{'id':company,'name':'Test','prefix':'TEST'}]}))
            config.chmod(0o600)
            await self.stop_service()
            await self.start_service()
            async def catalog_until(predicate):
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    async with self.client.get(self.web_url+'/api/dashboard') as response:
                        data=await response.json()
                    if predicate(data['paperclip']): return data['paperclip']
                    await asyncio.sleep(.2)
                self.fail('Paperclip catalog did not reach its expected state')
            catalog=await catalog_until(lambda c:c['connected'] and len(c['agents'])==1)
            self.assertIsNone(catalog['agents'][0]['workspace_id'])
            self.assertEqual((await self.api())['workspaces'],[])
            self.assertNotIn('fixture-password',json.dumps(catalog))
            self.assertNotIn('not-for-dashboard',json.dumps(catalog))
            path=f'/paperclip/companies/{company}/agents/{agent}/workspace'
            binding=await self.api(path,'POST',{})
            self.assertEqual((await self.api(path,'POST',{}))['id'],binding['id'])
            self.assertTrue(binding['id'].startswith('pc-'))
            self.assertEqual(binding['profile'],'marketing')
            self.assertEqual(catalog['agents'][0]['profile'],'marketing')
            self.assertFalse(self.attempts.exists(),'Discovery and binding must not boot guests')
            fresh='25dc15d4-4f72-443b-ae5a-d4213a54c917'
            fixture['rows'].append({**row,'id':fresh,'name':'New hire'})
            immediate=await self.api(f'/paperclip/companies/{company}/agents/{fresh}/workspace','POST',{})
            self.assertNotEqual(immediate['id'],binding['id'],'A new hire must bind before the next catalog poll')
            before=fixture['logins']
            fixture['session']=2
            await catalog_until(lambda c:not c['connected'])
            await catalog_until(lambda c:c['connected'])
            self.assertEqual(fixture['logins'],before+1)
            fixture['rows']=[]
            catalog=await catalog_until(lambda c:c['agents'] and not c['agents'][0]['present'])
            self.assertEqual(catalog['agents'][0]['workspace_id'],binding['id'])
            await self.api(path,'POST',{},expected=409)
            self.assertEqual(len((await self.api())['workspaces']),2,'Removed agents must retain workspace files')
        finally:
            await runner.cleanup()

    async def allocate(self, name='alpha'):
        return await self.api(method='POST', payload={'id':name})

    async def connect(self, binding, token=None):
        websocket = await self.client.ws_connect(
            f'http://127.0.0.1:{self.port}/workspaces/{binding["id"]}/exec',
            headers={'Authorization':'Bearer '+(token if token is not None else binding['auth_bearer_token'])})
        self.sockets.append(websocket)
        return websocket

    async def wait_status(self, workspace, predicate):
        async with asyncio.timeout(5):
            while True:
                status = await self.api('/workspaces/'+workspace)
                if predicate(status):
                    return status
                await asyncio.sleep(.025)

    async def test_control_socket_and_state_are_private(self):
        self.assertTrue(stat.S_ISSOCK((self.state/'control.sock').stat().st_mode))
        for path in (self.state, self.state/'control.sock', self.state/'state.sqlite'):
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
        self.assertEqual((await self.api())['slots'], 2)

    async def test_malformed_allocation_inputs_return_client_errors(self):
        for body in ('{invalid', '[]', 'null', '"alpha"', '{"id":3}', '{}'):
            with self.subTest(body=body):
                async with self.admin.post('http://localhost/workspaces', data=body,
                    headers={'Content-Type':'application/json'}) as response:
                    self.assertEqual(response.status, 400, await response.text())
        self.assertEqual((await self.api())['workspaces'], [])

    async def test_workspace_ids_cannot_escape_the_state_directory(self):
        for name in ('../outside', 'a/b', '', 'a'*49, 'with space'):
            with self.subTest(name=name):
                await self.api(method='POST', payload={'id':name}, expected=400)
        self.assertEqual((await self.api())['workspaces'], [])

    async def test_allocation_is_idempotent_and_slot_exhaustion_preserves_bindings(self):
        first = await self.allocate()
        self.assertEqual(await self.allocate(), first)
        second = await self.allocate('beta')
        self.assertNotEqual(first['slot'], second['slot'])
        self.assertNotEqual(first['auth_bearer_token'], second['auth_bearer_token'])
        await self.api(method='POST', payload={'id':'gamma'}, expected=409)
        self.assertEqual(await self.allocate(), first)
        self.assertEqual(await self.allocate('beta'), second)
        status = await self.api('/workspaces/alpha')
        self.assertNotIn('auth_bearer_token', status)
        self.assertIsNone(status['pid'])
        self.assertFalse(self.attempts.exists())

    async def test_workspace_binding_and_capability_survive_service_restart(self):
        binding = await self.allocate()
        await self.stop_service()
        await self.start_service()
        self.assertEqual(await self.allocate(), binding)
        self.assertFalse(self.attempts.exists())

    async def test_profile_selection_persists_and_conflicts_never_replace_workspace(self):
        await self.api(method='POST', payload={'id':'alpha','profile':'missing'}, expected=409)
        await self.api(method='POST', payload={'id':'alpha','profile':3}, expected=400)
        self.assertEqual((await self.api())['workspaces'], [])
        binding=await self.api(method='POST',payload={'id':'alpha','profile':'frontend'})
        self.assertEqual(binding['profile'],'frontend')
        self.assertEqual(binding['configured_memory_mb'],3072)
        await self.stop_service()
        await self.start_service()
        self.assertEqual(await self.allocate(),binding)
        await self.api(method='POST',payload={'id':'alpha','profile':'swe'},expected=409)
        visible=await self.web('/api/workspaces',method='POST',payload={'id':'beta','profile':'research'})
        self.assertEqual(visible['profile'],'research')
        self.assertNotIn('auth_bearer_token',visible)
        self.assertEqual((await self.api())['profiles'],['frontend','marketing','research','sales','swe'])
        self.assertFalse(self.attempts.exists())

    async def test_legacy_checkpoint_keeps_original_memory_bound(self):
        await self.allocate()
        await self.stop_service()
        metadata=self.state/'workspaces/alpha/state.json'
        metadata.write_text(json.dumps({'state':'suspended','runner':'old-image','snapshot':'snapshot-fixture'}))
        await self.start_service()
        status=await self.api('/workspaces/alpha')
        self.assertEqual(status['memory_bound_bytes'],1024**3)
        self.assertEqual(status['configured_memory_mb'],3072)
        self.assertEqual(status['runner'],'old-image')
        dashboard=await self.web()
        self.assertEqual(dashboard['workspaces'][0]['memory_bound_bytes'],1024**3)
        self.assertFalse(self.attempts.exists())

    async def test_pending_operation_is_unknown_after_restart_without_replay(self):
        await self.allocate()
        await self.stop_service()
        with sqlite3.connect(self.state/'state.sqlite') as database:
            database.execute('INSERT INTO operations(id,workspace,method,state,created) VALUES(?,?,?,?,?)',
                ('interrupted', 'alpha', 'fs/writeFile', 'pending', time.time()))
        await self.start_service()
        with sqlite3.connect(self.state/'state.sqlite') as database:
            rows = database.execute('SELECT method,state,finished FROM operations WHERE id=?', ('interrupted',)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], ('fs/writeFile', 'unknown'))
        self.assertIsNotNone(rows[0][2])
        self.assertIsNone((await self.api('/workspaces/alpha'))['pid'])
        self.assertFalse(self.attempts.exists())

    async def test_second_service_cannot_replace_the_first_services_socket(self):
        socket_inode = (self.state/'control.sock').stat().st_ino
        second = await asyncio.create_subprocess_exec(self.binary,
            env={**self.environment, 'ENVIRONMENT_PORT':str(free_port())},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(second.communicate(), 3)
            self.assertNotEqual(second.returncode, 0, error.decode())
            self.assertEqual((self.state/'control.sock').stat().st_ino, socket_inode)
            self.assertEqual((await self.api())['slots'], 2)
        finally:
            if second.returncode is None:
                second.kill()
                await second.wait()

    async def test_capabilities_are_required_and_bound_to_one_workspace(self):
        first, second = await self.allocate(), await self.allocate('beta')
        for binding, token in ((first, 'incorrect'), (second, first['auth_bearer_token']), (first, 'wrong-é')):
            with self.subTest(workspace=binding['id'], token_kind='invalid'):
                with self.assertRaises(WSServerHandshakeError) as rejected:
                    await self.connect(binding, token)
                self.assertEqual(rejected.exception.status, 401)
        for binding in (first, second):
            status = await self.api('/workspaces/'+binding['id'])
            self.assertFalse(status['writer_connected'])
            self.assertIsNone(status['pid'])
        self.assertFalse(self.attempts.exists())

    async def test_one_writer_and_disconnect_cancel_admission_without_starting(self):
        binding = await self.allocate()
        websocket = await self.connect(binding)
        status = await self.wait_status('alpha', lambda value: value.get('queued_operations', 0) == 1)
        self.assertTrue(status['writer_connected'])
        self.assertIsNone(status['pid'])
        with self.assertRaises(WSServerHandshakeError) as rejected:
            await self.connect(binding)
        self.assertEqual(rejected.exception.status, 409)
        await websocket.close()
        status = await self.wait_status('alpha', lambda value: not value['writer_connected'])
        self.assertEqual(status.get('queued_operations', 0), 0)
        self.assertEqual(status['active_operations'], 0)
        self.assertIsNone(status['pid'])
        self.assertFalse(self.attempts.exists())
        self.assertFalse((self.state/'workspaces/alpha/state.ext4').exists())
        # The writer claim must be reusable after the canceled connection.
        await self.connect(binding)

    async def web(self, path='/api/dashboard', method='GET', payload=None, headers=None, expected=200):
        async with self.client.request(method, self.web_url+path, json=payload,
            headers=self.web_headers if headers is None else headers) as response:
            content = await response.text()
            self.assertEqual(response.status, expected, content)
            try:
                return json.loads(content) if content else None
            except json.JSONDecodeError:
                return {'error':content}

    async def test_dashboard_and_live_stream_do_not_wake_or_lease_a_workspace(self):
        binding = await self.allocate()
        async with self.client.get(self.web_url+'/events') as response:
            self.assertEqual(response.status, 200)
            self.assertIn('text/event-stream', response.headers['Content-Type'])
            snapshots = []
            while len(snapshots) < 2:
                line = (await response.content.readline()).decode()
                if line.startswith('data: '):
                    snapshots.append(json.loads(line[6:]))
            row = snapshots[-1]['workspaces'][0]
            self.assertEqual(row['id'], binding['id'])
            self.assertIsNone(row['pid'])
            self.assertFalse(row['writer_connected'])
            self.assertEqual(row['active_operations'], 0)
            self.assertEqual(row['queued_operations'], 0)
            self.assertNotIn(binding['auth_bearer_token'], json.dumps(snapshots))
        status = await self.api('/workspaces/alpha')
        self.assertIsNone(status['pid'])
        self.assertEqual(status['active_operations'], 0)
        self.assertFalse(self.attempts.exists())
        self.assertFalse((self.state/'workspaces/alpha/state.ext4').exists())

    async def test_dashboard_creation_redacts_capability_and_preserves_private_binding(self):
        visible = await self.web('/api/workspaces', method='POST', payload={'id':'alpha'})
        self.assertNotIn('auth_bearer_token', visible)
        binding = await self.allocate()
        self.assertEqual(visible['id'], binding['id'])
        dashboard = await self.web()
        self.assertEqual(dashboard['workspaces'][0]['id'], 'alpha')
        self.assertNotIn(binding['auth_bearer_token'], json.dumps(dashboard))
        self.assertFalse(self.attempts.exists())
        await self.web('/api/workspaces', method='POST', payload={'id':'../outside'}, expected=400)
        await self.web('/api/workspaces', method='POST', payload={'id':'beta'})
        await self.web('/api/workspaces', method='POST', payload={'id':'gamma'}, expected=409)

    async def test_dashboard_rejects_cross_origin_controls_and_unconfigured_hosts(self):
        for headers in ({}, {'Origin':'https://untrusted.example', 'X-Environment-UI':'1'},
            {'Origin':self.web_url}, {**self.web_headers, 'Sec-Fetch-Site':'cross-site'}):
            await self.web('/api/workspaces', method='POST', payload={'id':'alpha'}, headers=headers, expected=403)
        await self.web(headers={'Host':'untrusted.example'}, expected=403)
        self.assertEqual((await self.api())['workspaces'], [])

    async def test_dashboard_does_not_publish_execution_or_private_admin_routes(self):
        binding = await self.allocate()
        for path in ('/workspaces', '/workspaces/alpha', '/workspaces/alpha/exec'):
            await self.web(path, expected=404)
        await self.web('/api/workspaces/alpha/exec', method='POST', expected=404)
        async with self.client.get(self.web_url+'/') as response:
            self.assertEqual(response.status, 200)
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertNotIn(binding['auth_bearer_token'], await response.text())

    async def test_dashboard_actions_are_journaled_and_preserve_saved_files(self):
        await self.allocate()
        await self.web('/api/workspaces/alpha/suspend', method='POST')
        dashboard = await self.web()
        operation = dashboard['recent_operations'][0]
        self.assertEqual((operation['workspace'], operation['method'], operation['state']),
            ('alpha', 'workspace/suspend', 'completed'))
        self.assertIsNotNone(operation['finished'])
        await self.web('/api/workspaces/missing/suspend', method='POST', expected=404)
        await self.web('/api/workspaces/alpha/invalid', method='POST', expected=404)
        self.assertFalse(self.attempts.exists())

    async def test_dashboard_observation_preserves_the_attached_writer_and_its_queue(self):
        binding = await self.allocate()
        await self.connect(binding)
        await self.wait_status('alpha', lambda value: value.get('queued_operations') == 1)
        async with asyncio.timeout(5):
            while True:
                rows = (await self.web())['workspaces']
                if rows and rows[0]['queued_operations'] == 1:
                    row = rows[0]
                    break
                await asyncio.sleep(.025)
        self.assertTrue(row['writer_connected'])
        self.assertEqual(row['active_operations'], 0)
        self.assertIsNone(row['pid'])
        self.assertFalse(self.attempts.exists())

    async def test_open_dashboard_stream_does_not_block_service_shutdown(self):
        response = await self.client.get(self.web_url+'/events')
        self.assertEqual(response.status, 200)
        await response.content.readline()
        await self.stop_service()
        response.close()
        self.assertEqual(self.process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
