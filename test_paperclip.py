"""Check the Paperclip launcher and its scoped tool boundary without inference."""
import importlib.util
from pathlib import Path
import sys
import tomllib
import unittest

ROOT=Path(__file__).parent
sys.path.insert(0,str(ROOT))
import codex
import paperclip_codex
import paperclip_mcp

COMPANY='8744dbdb-cdd7-4fee-8b46-3bd6ae6705fe'

class PaperclipTests(unittest.TestCase):
    def test_adapter_permissions_cannot_override_guest_routing(self):
        args=paperclip_codex.arguments_for_managed_codex(['exec','--json','-c','sandbox_mode="workspace-write"','-c','model_reasoning_effort="high"','--model','gpt-6.1-sol','-'])
        self.assertNotIn('-c',args)
        self.assertIn('--skip-git-repo-check',args)
        for args in [['exec','-c','model_provider="other"','-'], ['exec','--cd','/home/agent','-'],['exec','--model','claude-anything','-'],['--search','exec','-']]:
            with self.subTest(args=args),self.assertRaises(RuntimeError):
                paperclip_codex.arguments_for_managed_codex(args)

    def test_scoped_paperclip_tool_uses_separate_credentials(self):
        context={'PAPERCLIP_API_URL':'https://org.h.mattre.id','PAPERCLIP_API_KEY':'scoped-test-credential','PAPERCLIP_COMPANY_ID':COMPANY}
        config=tomllib.loads(codex.harness_config(context))
        self.assertEqual(config['model_providers']['environment_inference']['env_key'],codex.PROVIDER_KEY_ENV)
        self.assertEqual(config['mcp_servers']['paperclip']['env'],context)
        self.assertIn('PAPERCLIP_API_KEY',config['shell_environment_policy']['exclude'])
        self.assertFalse(config['features']['apps'])

    def test_company_paths_and_sensitive_api_routes_are_restricted(self):
        for path in ['/agents/me',f'/companies/{COMPANY}/agents',f'/companies/{COMPANY}/agent-hires','/issues/'+COMPANY+'/checkout']:
            self.assertTrue(paperclip_mcp.allowed_path(path,COMPANY),path)
        for path in ['https://evil.test','/companies/other/agents','/agents/../../secrets','/companies/'+COMPANY+'/secrets','/agents/'+COMPANY+'/keys','/auth/sign-in/email']:
            self.assertFalse(paperclip_mcp.allowed_path(path,COMPANY),path)

    def test_api_results_do_not_return_credentials(self):
        result=paperclip_mcp.redact({'name':'Visible','token':'hidden','nested':[{'privateKey':'hidden','status':'idle'}]})
        self.assertEqual(result,{'name':'Visible','nested':[{'status':'idle'}]})

if __name__=='__main__':
    unittest.main()
