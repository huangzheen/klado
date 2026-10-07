#!/usr/bin/env python3
"""Real PG + S3-compatible storage + browser probe. Run only on disposable fixtures.
KLADO_TEST_DISPOSABLE=1 KLADO_BASE=http://127.0.0.1:18010 python ...
Never run against a deployed service: this script creates and deletes test content.
"""
import os, sys, json
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
import requests
from services import auth_store
from playwright.sync_api import sync_playwright
BASE = os.environ.get('KLADO_BASE', 'http://127.0.0.1:18010')
assert os.environ.get('KLADO_TEST_DISPOSABLE') == '1', 'disposable fixtures required'
PASS = 0

def check(value, label):
    global PASS
    assert value, label
    PASS += 1
    print('ok', label, flush=True)

users = ['dc-owner@example.test', 'dc-mate@example.test', 'dc-admin@example.test']
password = 'dc-test-password'
auth_store._ensure_schema()
with auth_store._db() as conn, conn.cursor() as cur:
    for email in users:
        cur.execute('INSERT INTO app_users(email,password_hash,display_name,role) VALUES(%s,%s,%s,%s) ON CONFLICT(email) DO UPDATE SET password_hash=EXCLUDED.password_hash,role=EXCLUDED.role',
                    (email, auth_store._hash(password), email, 'admin' if 'admin' in email else 'user'))
    conn.commit()
from services import data_center_db as dcdb, dataset_shares, file_shares
from core.config import settings
assert settings.POSTGRES_HOST == '127.0.0.1' and settings.POSTGRES_PORT == 15439, 'dedicated test database required'
dcdb.init_registry(); dcdb.init_file_library(); dataset_shares.ensure_schema(); file_shares.ensure_schema()
with dcdb.get_pg_conn() as conn, conn.cursor() as cur:
    cur.execute('DROP TABLE IF EXISTS public.dc_test_dataset')
    cur.execute("DELETE FROM public.dataset_shares WHERE table_name='dc_test_dataset'")
    cur.execute("DELETE FROM public._import_registry WHERE table_name='dc_test_dataset'")
    cur.execute('DELETE FROM public._file_library WHERE owner_email=ANY(%s)', (users,))
    conn.commit()
import boto3
s3=boto3.client('s3',endpoint_url=os.environ['OSS_ENDPOINT'],aws_access_key_id=os.environ['OSS_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['OSS_ACCESS_KEY_SECRET'])
try:
    s3.create_bucket(Bucket=os.environ['OSS_BUCKET_NAME'])
except s3.exceptions.BucketAlreadyOwnedByYou:
    pass
sessions=[]
for email in users:
    session=requests.Session()
    session.trust_env=False
    r=session.post(BASE+'/api/auth/login',json={'email':email,'password':password})
    check(r.status_code==200, 'login '+email)
    sessions.append(session)
a,b,admin=sessions
api='/api/data-center'
def post(session,path,**kwargs): return session.post(BASE+api+path,timeout=30,**kwargs)
def get(session,path): return session.get(BASE+api+path,timeout=30)
def delete(session,path): return session.delete(BASE+api+path,timeout=30)
files=[]
for session in (a,b):
    r=post(session,'/files/stage', files={'file':('same.csv',b'customer_id,amount,percentage,date,flag\n001,12.50,20%,2026-10-03,yes\n002,8.75,30%,2026-10-04,no\n','text/csv')},data={'folder':'mixed'})
    check(r.status_code==200, 'upload owned file: '+r.text[:100])
    files.append(r.json())
f,g=files
check(f['object_name']!=g['object_name'],'same filename does not collide')
for session, own in ((a,f),(b,g)):
    rows=get(session,'/files').json()
    check(any(r['id']==own['id'] for r in rows) and len(rows)==1,'own file list excludes other user')
check(get(admin,'/files').json()==[], 'administrator file list stays private')
check(get(b,'/files/download?path='+f['object_name']).status_code==404,'unshared file download refused')
r=post(a,'/files/import-with-rules',json={'file_id':f['id'],'table_name':'dc_test_dataset','display_name':'<b>我的数据集</b>','mode':'replace','rules':{}})
check(r.status_code==200, 'create dataset: '+r.text[:100])
patch = a.patch(BASE+api+'/datasets/dc_test_dataset',json={'display_name':'<b>我的数据集</b>'})
check(patch.status_code==200, 'owner renames dataset')
rows=get(a,'/datasets').json(); ds=next(d for d in rows if d['table_name']=='dc_test_dataset')
check(ds['can_manage'] is True,'ordinary owner can manage')
check(get(b,'/datasets').json()==[], 'datasets isolated from colleague')
check(get(admin,'/datasets').json()==[], 'datasets isolated from administrator')
check(post(b,'/files/import-with-rules',json={'file_id':g['id'],'table_name':'dc_test_dataset','mode':'replace'}).status_code==409,'cross-account overwrite refused')
check(post(a,'/files/import-with-rules',json={'file_id':f['id'],'table_name':'app_users','mode':'replace'}).status_code==409,'system table cannot be claimed as a dataset')
check(post(a,'/files/import-with-rules',json={'file_id':f['id'],'table_name':'dc_test_dataset','mode':'replace','cleaning_rules':{'columns':[{'original':'amount','dtype':'boolean'}]}}).status_code==422,'invalid explicit conversion rejected')
check(get(a,'/datasets/dc_test_dataset/preview').json()['count']==2,'rejected update preserves original rows')
r=post(a,'/datasets/dc_test_dataset/shares',json={'email':users[1]})
check(r.status_code==200,'owner shares dataset '+r.text[:100])
check(get(b,'/datasets/dc_test_dataset/preview').status_code==200,'recipient reads dataset')
check(get(b,'/datasets/dc_test_dataset/source-file').status_code==404,'dataset share does not grant source file')
for path in ('/datasets/dc_test_dataset','/datasets/dc_test_dataset/shares/'+users[1]):
    check(delete(b,path).status_code==404,'recipient cannot delete or delegate '+path)
check(post(b,'/datasets/dc_test_dataset/shares',json={'email':users[2]}).status_code==404,'recipient cannot re-share')
check(post(admin,'/query',json={'sql':'select * from dc_test_dataset'}).status_code==403,'admin SQL cannot bypass ownership')
check(post(b,'/query',json={'sql':'select sum(amount) as total from dc_test_dataset'}).status_code==200,'authorized aggregate query')
check(post(b,'/query',json={'sql':"select query_to_xml('select * from app_users',false,false,'')"}).status_code==403,'SQL function cannot bypass scope')
r=post(a,'/files/'+str(f['id'])+'/shares',json={'email':users[1]})
check(r.status_code==200,'owner shares source file '+r.text[:100])
check(get(b,'/files/shared-with-me').json()[0]['id']==f['id'],'shared file list explicit')
check(get(b,'/files/download?path='+f['object_name']).status_code==200,'file grant permits download')
check(delete(b,'/files/'+str(f['id'])).status_code==404,'file recipient cannot delete')
check(post(b,'/files/'+str(f['id'])+'/publish').status_code==404,'file recipient cannot create public link')
copied_file=post(b,'/files/'+str(f['id'])+'/pull')
check(copied_file.status_code==200,'recipient pulls independently owned file copy')
copy_path=copied_file.json()['object_name']
copied_dataset=post(b,'/datasets/dc_test_dataset/pull')
check(copied_dataset.status_code==200,'recipient pulls independently owned dataset copy')
copy_table=copied_dataset.json()['table_name']
check(not copied_dataset.json().get('source_file_path'),'dataset copy does not copy source spreadsheet')
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1000})
    context.add_cookies([{'name':c.name,'value':c.value,'url':BASE} for c in a.cookies])
    page=context.new_page(); errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE,wait_until='networkidle')
    page.evaluate("switchToDataCenter()")
    page.locator('[data-dc-section="datasets"]').click()
    page.wait_for_selector('#dc-datasets-grid .dc-dataset-card')
    check(page.locator('#dc-section-upload').is_hidden(),'datasets is separate from files')
    check(page.locator('#dc-datasets-grid .dc-dataset-name').inner_text()=='<b>我的数据集</b>','dataset names escaped as text')
    page.locator('#dc-datasets-grid button[onclick^="dc.previewDataset"]').click()
    page.wait_for_selector('#dc-preview-modal.open')
    check('001' in page.locator('#dc-preview-body').inner_text(),'preview retains identifier values')
    page.locator('#dc-preview-modal button[onclick^="dc.closePreview"]').click()
    page.locator('#dc-datasets-grid button[onclick^="dc.openShare"]').click()
    check('dataset' in page.locator('#dc-share-title').inner_text().lower(),'dataset sharing dialog describes dataset')
    check(page.locator('#dc-share-email').get_attribute('aria-autocomplete')=='list','share dialog uses colleague picker')
    page.locator('#dc-share-modal button[onclick="dc.closeShare()"]').first.click()
    page.evaluate('(id)=>dc.openShare(String(id),"files")', f['id'])
    page.wait_for_selector('#dc-share-modal',state='visible')
    check('file' in page.locator('#dc-share-title').inner_text().lower(),'file sharing dialog describes file')
    page.locator('#dc-share-modal button[onclick="dc.closeShare()"]').first.click()
    for theme in ('light','dark'):
        page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
        check(page.locator('.nav').is_visible(),'navigation visible '+theme)
        hit=page.evaluate("""()=>{let n=document.querySelector('.nav'),r=n.getBoundingClientRect();return n.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}""")
        check(hit,'navigation not covered '+theme)
        page.screenshot(path='/tmp/dc-unify-'+theme+'.png',full_page=True)
    page.locator('[data-dc-section="upload"]').click()
    check(page.locator('#dc-section-datasets').is_hidden(),'files hides dataset list')
    # ⚠️ The scope control is a pair of `.rpt-tab` buttons, the same shape every
    # other module uses — it was a `<select>`, and `select_option` on a missing
    # element fails as "element not found" rather than as "this control is gone".
    page.locator('[data-dc-scope="shared"]').click()
    page.wait_for_selector('#dc-shared-files .dc-dataset-card',state='hidden') # owner has no borrowed files
    check(page.locator('#dc-section-upload').is_hidden(),'shared scope cannot upload into owner folders')
    check(not errors,'no browser errors '+str(errors))
    browser.close()
check(delete(a,'/files/'+str(f['id'])+'/shares/'+users[1]).status_code==200,'owner revokes file access')
check(get(b,'/files/download?path='+f['object_name']).status_code==404,'revoked file immediately unavailable')
check(delete(a,'/datasets/dc_test_dataset/shares/'+users[1]).status_code==200,'owner revokes dataset access')
check(get(b,'/datasets/dc_test_dataset/preview').status_code==404,'revoked dataset immediately unavailable')
check(get(b,'/files/download?path='+copy_path).status_code==200,'file copy survives revoked grant')
check(get(b,'/datasets/'+copy_table+'/preview').status_code==200,'dataset copy survives revoked grant')
check(delete(a,'/files/by-path?path=mixed&is_folder=true').status_code==200,'owner deletes only owned portion of shared folder')
check(get(b,'/files/download?path='+g['object_name']).status_code==200,'other user file survives folder delete')
check(delete(a,'/datasets/dc_test_dataset').status_code==200,'ordinary owner deletes dataset')
check(delete(b,'/datasets/'+copy_table).status_code==200,'recipient manages own dataset copy')
print(json.dumps({'passed':PASS,'failed':0}))
