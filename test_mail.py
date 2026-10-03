"""Check shared mail boundaries and launcher wiring without touching real mail."""
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.request

import claude
import codex
import mail_mcp

MESSAGE = b'From: Sender <sender@example.com>\r\nTo: receiver@example.com\r\nSubject: Fixture\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nRead me without marking seen.\r\n'


class IMAPFixture:
    instances = []
    validity = b'42'
    size = len(MESSAGE)
    failure = None

    def __init__(self, host, port, **options):
        self.host, self.port, self.options = host, port, options
        self.calls = []
        self.literal = None
        self.logged_out = False
        self.__class__.instances.append(self)

    def login(self, account, password):
        if self.failure:
            raise RuntimeError(self.failure)

    def select(self, mailbox, readonly=False):
        self.selection = (mailbox, readonly)
        return 'OK', [b'2']

    def response(self, key):
        return key, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, args, self.literal))
        if command == 'SEARCH':
            return 'OK', [b'7 9 11']
        if args[1] == '(RFC822.SIZE)':
            return 'OK', [f'1 (RFC822.SIZE {self.size})'.encode()]
        return 'OK', [(b'1 (BODY[])', MESSAGE), b')']

    def logout(self):
        self.logged_out = True


class MailTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'mail.json'
        self.path.write_text(json.dumps({'account': 'fixture@example.com', 'app_password': 'fixture-secret', 'mailbox': 'INBOX'}))
        self.path.chmod(0o600)
        IMAPFixture.instances = []
        IMAPFixture.validity = b'42'
        IMAPFixture.size = len(MESSAGE)
        IMAPFixture.failure = None

    def invoke(self, name, args):
        return mail_mcp.invoke(name, args, self.path, IMAPFixture)

    def test_search_uses_literal_readonly_and_peek(self):
        result = self.invoke('search_mail', {'query': 'subject:"résumé"', 'limit': 2})
        self.assertEqual([row['message_id'] for row in result['messages']], ['42:11', '42:9'])
        self.assertTrue(result['has_more'])
        client = IMAPFixture.instances[0]
        self.assertEqual((client.host, client.port), ('imap.gmail.com', 993))
        self.assertIsNotNone(client.options['ssl_context'])
        self.assertEqual(client.selection, ('"INBOX"', True))
        self.assertEqual(client.calls[0], ('SEARCH', ('CHARSET', 'UTF-8', 'X-GM-RAW'), 'subject:"résumé"'.encode()))
        self.assertTrue(all('BODY.PEEK[' in row[1][1] for row in client.calls[1:]))
        self.assertTrue(client.logged_out)
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_read_checks_identity_size_and_does_not_mark_seen(self):
        result = self.invoke('read_mail', {'message_id': '42:9'})
        self.assertIn('Read me', result['body'])
        client = IMAPFixture.instances[0]
        self.assertEqual(client.selection[1], True)
        self.assertEqual(client.calls[0][1][1], '(RFC822.SIZE)')
        self.assertTrue(client.calls[1][1][1].startswith('(BODY.PEEK[]<'))
        with self.assertRaises(mail_mcp.MailError):
            self.invoke('read_mail', {'message_id': '43:9'})
        self.assertEqual(IMAPFixture.instances[-1].calls, [])
        IMAPFixture.size = mail_mcp.MAX_MESSAGE_BYTES + 1
        with self.assertRaises(mail_mcp.MailError):
            self.invoke('read_mail', {'message_id': '42:9'})
        self.assertEqual(len(IMAPFixture.instances[-1].calls), 1)

    def test_input_cannot_supply_commands_paths_or_write_actions(self):
        for name, args in [('search_mail', {'query': 'ALL\r\nSTORE 1 +FLAGS \\Seen'}),
                           ('search_mail', {'query': 'a', 'limit': True}),
                           ('search_mail', {'query': 'a', 'path': '/etc/passwd'}),
                           ('read_mail', {'message_id': '42:9 STORE 1'}),
                           ('send_mail', {})]:
            with self.subTest(name=name, args=args), self.assertRaises(mail_mcp.MailError):
                self.invoke(name, args)
        self.assertEqual(IMAPFixture.instances, [])

    def test_config_permissions_symlinks_and_provider_errors(self):
        self.path.chmod(0o644)
        with self.assertRaises(mail_mcp.MailError):
            mail_mcp.load_config(self.path)
        self.path.chmod(0o600)
        link = self.path.with_name('link')
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            mail_mcp.load_config(link)
        IMAPFixture.failure = 'provider exposed fixture-secret'
        response = mail_mcp.handle({'method': 'tools/call', 'params': {'name': 'search_mail', 'arguments': {'query': 'a'}}}, self.path, IMAPFixture)
        self.assertTrue(response['isError'])
        self.assertNotIn('fixture-secret', json.dumps(response))
        self.assertTrue(IMAPFixture.instances[-1].logged_out)

    def test_html_and_attachments_are_not_executed_or_exposed(self):
        raw = b'Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/html\r\n\r\n<p>Hello</p><script>secret_script</script>World\r\n--x\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename="proof.bin"\r\n\r\nsecret_attachment\r\n--x--\r\n'
        message = mail_mcp.BytesParser(policy=mail_mcp.email.policy.default).parsebytes(raw)
        result = mail_mcp.message_text(message)
        self.assertIn('Hello', result['body'])
        self.assertNotIn('secret_script', result['body'])
        self.assertNotIn('secret_attachment', json.dumps(result))
        self.assertEqual(result['attachments'][0]['filename'], 'proof.bin')

    def test_bridge_requires_capability_and_discovery_does_not_authenticate(self):
        with mail_mcp.http_bridge(self.path, IMAPFixture) as bridge:
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode()
            request = urllib.request.Request(bridge['url'], data=body, headers={'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            request.add_header('Authorization', 'Bearer ' + bridge['capability'])
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertEqual({tool['name'] for tool in result['result']['tools']}, {'search_mail', 'read_mail'})
            self.assertEqual(IMAPFixture.instances, [])
            call = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                    'params': {'name': 'search_mail', 'arguments': {'query': 'subject:Fixture', 'limit': 1}}}
            request = urllib.request.Request(bridge['url'], data=json.dumps(call).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + bridge['capability']})
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertFalse(result['result'].get('isError', False))
            mail = json.loads(result['result']['content'][0]['text'])
            self.assertEqual(mail['messages'][0]['message_id'], '42:11')

    def test_both_launchers_expose_only_bridge_capability(self):
        bridge = {'url': 'http://127.0.0.1:9999/mcp', 'capability': 'temporary-mail-capability'}
        config = tomllib.loads(codex.harness_config(mail_context=bridge))
        self.assertEqual(config['mcp_servers']['mail']['http_headers']['Authorization'], 'Bearer temporary-mail-capability')
        state = Path(self.folder.name)
        claude.harness_arguments(state, bridge, ['--print'], mail_context=bridge)
        config = json.loads((state / 'mcp.json').read_text())
        self.assertEqual(set(config['mcpServers']), {'mail', 'environment'})
        self.assertNotIn('fixture-secret', json.dumps(config))


if __name__ == '__main__':
    unittest.main()
