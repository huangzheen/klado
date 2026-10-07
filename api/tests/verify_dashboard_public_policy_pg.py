#!/usr/bin/env python3
"""Public Dashboard writes: real disposable PostgreSQL, browser and agent identity.
Requires the same disposable fixtures as verify_datacenter_isolation_http.py.
"""
import os, sys, io, base64
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
os.environ['NO_PROXY'] = '127.0.0.1,localhost'
import requests
from PIL import Image
from core.config import settings
from services import auth_store
from klado_shared import orgs
assert os.environ.get('KLADO_TEST_DISPOSABLE') == '1'
assert settings.POSTGRES_HOST == '127.0.0.1' and settings.POSTGRES_PORT == 15439
BASE=os.environ.get('KLADO_BASE','http://127.0.0.1:18011')
OWNER='dc-owner@example.test'
PASS=0

def check(value,label):
    global PASS
    assert value, label
    PASS+=1; print('ok',label,flush=True)

orgs.ensure_schema()
org=orgs.create_org('Public policy test',share_scope=orgs.SCOPE_GLOBAL,public_scope=orgs.PUBLIC_FORBID)
with auth_store._db() as conn, conn.cursor() as cur:
    cur.execute('SELECT id FROM app_users WHERE email=%s',(OWNER,)); uid=cur.fetchone()[0]
    cur.execute('DELETE FROM org_members WHERE user_id=%s',(uid,))
    cur.execute("INSERT INTO org_members(org_id,user_id,email,org_role,status) VALUES(%s,%s,%s,'owner','active')",(org['id'],uid,OWNER))
    conn.commit()
a=requests.Session(); a.trust_env=False
check(a.post(BASE+'/api/auth/login',json={'email':OWNER,'password':'dc-test-password'}).status_code==200,'browser owner login')
slug='dc-public-policy-test'
image=Image.new('RGB',(320,180),'blue'); buffer=io.BytesIO(); image.save(buffer,format='PNG')
body={'title':'Public policy test','html':'<!doctype html><p>Public policy test</p>','visibility':'public','cover_base64':base64.b64encode(buffer.getvalue()).decode()}
url=BASE+'/api/dashboard/'+slug
r=a.put(url,json=body)
check(r.status_code==403,'organization forbids public Dashboard before write: '+r.text[:100])
check(a.get(url).status_code==404,'refusal creates no Dashboard')
with auth_store._db() as conn,conn.cursor() as cur:
    cur.execute("UPDATE orgs SET public_scope='approve' WHERE id=%s",(org['id'],)); conn.commit()
r=a.put(url,json=body)
check(r.status_code==403,'public Dashboard requires approval: '+r.text[:100])
request=orgs.request_public_approval(OWNER,orgs.TARGET_DASHBOARD,slug,orgs.CHANNEL_PUBLIC)
orgs.decide_public_approval(request['id'],'approve','test-admin@example.test',expected_org_id=org['id'])
r=a.put(url,json=body)
check(r.status_code==200,'approved browser owner publishes public Dashboard: '+r.text[:100])
code,_=auth_store.create_agent_token(uid,'public policy probe',created_by=OWNER)
agent=requests.Session(); agent.trust_env=False; agent.headers['Authorization']='Bearer '+code
r=agent.put(url,json={'title':'Agent public update'})
check(r.status_code==403,'agent cannot edit existing public Dashboard')
r=agent.put(BASE+'/api/dashboard/dc-agent-public-policy-test',json=body)
check(r.status_code==403,'agent cannot create public Dashboard even with authorization')
check(a.delete(url).status_code==200,'owner removes disposable public Dashboard')
print({'passed':PASS,'failed':0})
