"""Contract guards for agent identity and same-office messaging routes."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from routers import town
from main import _agent_may_write


class TownContracts(unittest.TestCase):
    def setUp(self):
        self.user={'id':7,'_agent_token_id':23}
        self.kind='agent'
        app=FastAPI()
        @app.middleware('http')
        async def identity(request:Request,next_call):
            request.state.current_user=self.user
            request.state.auth_kind=self.kind
            return await next_call(request)
        app.include_router(town.router)
        self.client=TestClient(app)
        self.payload={'recipient_id':12,'kind':'handoff','subject':'Review','body':'Check totals','client_id':'test-1'}

    def test_anonymous_cannot_read_map(self):
        self.user=None
        self.assertEqual(self.client.get('/api/auth/town').status_code,401)

    def test_browser_cannot_impersonate_an_agent_sender(self):
        self.kind='browser'
        with patch.object(town.town,'send') as send:
            self.assertEqual(self.client.post('/api/auth/agent-messages',json=self.payload).status_code,403)
            send.assert_not_called()

    def test_sender_identity_comes_only_from_credential(self):
        with patch.object(town.town,'send',return_value={'id':1}) as send:
            self.assertEqual(self.client.post('/api/auth/agent-messages',json=self.payload).status_code,201)
            self.assertEqual(send.call_args.args[0],self.user)
            self.assertEqual(self.client.post('/api/auth/agent-messages',json={**self.payload,'sender_id':99}).status_code,422)

    def test_messages_are_bounded_and_nonempty(self):
        for delta in [{'body':''},{'body':' '*10},{'body':'x'*4001},{'subject':'x'*161},{'client_id':'x'*81},{'recipient_id':0},{'kind':'execute'}]:
            with self.subTest(delta=list(delta)):
                self.assertEqual(self.client.post('/api/auth/agent-messages',json={**self.payload,**delta}).status_code,422)

    def test_authorization_failures_are_forbidden(self):
        with patch.object(town.town,'send',side_effect=PermissionError('Different office')):
            self.assertEqual(self.client.post('/api/auth/agent-messages',json=self.payload).status_code,403)

    def test_agent_profile_rejects_sibling_edit(self):
        with patch.object(town.town,'agent_self',return_value={'agent_id':1}),patch.object(town.town,'update_agent') as edit:
            self.assertEqual(self.client.patch('/api/auth/office-agents/2',json={'name':'Other','animal':'cat','cloth':'red'}).status_code,403)
            edit.assert_not_called()

    def test_pending_inbox_flag_reaches_store(self):
        with patch.object(town.town,'messages',return_value={'messages':[]}) as inbox:
            self.assertEqual(self.client.get('/api/auth/agent-inbox?pending_only=true').status_code,200)
            inbox.assert_called_once_with(self.user,pending_only=True)

    def test_write_exceptions_are_exact_and_leave_credentials_protected(self):
        for path in ['/api/auth/agent-heartbeat','/api/auth/agent-messages','/api/auth/agent-messages/12','/api/auth/office-agents/12']:
            self.assertTrue(_agent_may_write(path),path)
        for path in ['/api/auth/agent-tokens','/api/auth/agent-heartbeat/other','/api/auth/agent-messages/12/other','/api/auth/office-agents/12x']:
            self.assertFalse(_agent_may_write(path),path)

    def test_town_page_size_is_bounded(self):
        self.assertEqual(self.client.get('/api/auth/town?limit=201').status_code,422)
        self.assertEqual(self.client.get('/api/auth/town?offset=-1').status_code,422)

if __name__=='__main__':unittest.main()
