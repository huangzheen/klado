"""Smoke test inside app-test only; never run fixtures against production.

docker exec klado-app-test-1 python /app/api/tests/verify_town_deployed.py
"""
import sys
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from klado_shared import accounts
from klado_shared.db import pg_connection_kwargs
import requests


def main():
    assert pg_connection_kwargs()['dbname']=='klado_test','Only the isolated test database is allowed'
    with accounts._db() as c,c.cursor() as q:
        q.execute('SELECT current_database()')
        assert q.fetchone()[0]=='klado_test','Refusing production fixtures'
    prefix='town-smoke-'+uuid.uuid4().hex
    u=accounts.create_user(prefix+'@example.test',uuid.uuid4().hex,'Town smoke')
    def request(method,path,code,body=None):
        r=requests.request(method,'http://127.0.0.1:8000/api/auth/'+path,
            headers={'Authorization':'Bearer '+code},json=body,timeout=20)
        assert r.status_code in (200,201),f'{method} {path}: HTTP {r.status_code}'
        return r.json()
    try:
        a=accounts.create_agent_token(u['id'],'Smoke sender')[0]
        b=accounts.create_agent_token(u['id'],'Smoke recipient')[0]
        own=request('GET','town',a)['office']
        assert own['kind']=='personal'
        first=request('GET','agent-self',a)['agent_id']
        second=request('GET','agent-self',b)['agent_id']
        assert first!=second
        request('POST','agent-heartbeat',a)
        request('POST','office-work',a,{'state':'working','task':'Deployment smoke'})
        seats=request('GET','office-agents',a)['seats']
        assert len(seats)==2 and sum(bool(s['updates']) for s in seats)==1
        print('PASS deployed town, independent identities, heartbeat and work history')
        message=request('POST','agent-messages',a,{'recipient_id':second,'kind':'handoff',
            'subject':'Smoke handoff','body':'Check isolated deployment','client_id':prefix})
        assert request('GET','agent-inbox?pending_only=true',b)['messages'][0]['id']==message['id']
        for state in ('accepted','completed'):
            assert request('PATCH','agent-messages/'+str(message['id']),b,{'state':state})['state']==state
        print('PASS deployed same-office handoff: pending -> accepted -> completed')
    finally:
        # Only the random fixture just created above is removed.
        with accounts._db() as c,c.cursor() as q:
            q.execute('DELETE FROM access_log WHERE user_id=%s',(u['id'],))
            q.execute('DELETE FROM office_agents WHERE user_id=%s',(u['id'],))
            q.execute('DELETE FROM agent_tokens WHERE user_id=%s',(u['id'],))
            q.execute('DELETE FROM org_members WHERE user_id=%s',(u['id'],))
            q.execute('DELETE FROM app_users WHERE id=%s',(u['id'],))
            q.execute('DELETE FROM town_offices WHERE owner_key=%s',('user:'+str(u['id']),))
        print('PASS isolated smoke fixtures cleaned')


if __name__=='__main__':main()
