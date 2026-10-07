"""Town addresses, multiple real credentials, private offices and real handoffs.
Uses only disposable local fixtures. AUTH_ENABLED=true is required.
"""
import os
os.environ['OFFICE_PORT']='18919'
os.environ['KNOWLEDGE_DOCS_AUTOAPPLY']='0'
import sys,uuid,json,re
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verify_office_ui as base
from klado_shared import accounts,orgs,town
from playwright.sync_api import sync_playwright
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import requests

PREFIX='town-'+uuid.uuid4().hex[:8]
USERS=[];ORG=None

def check(ok,msg):
    assert ok,msg
    print('PASS '+msg,flush=True)

def user(i):
    u=accounts.create_user(PREFIX+str(i)+'@example.test',base.PASSWORD,'Town user '+str(i))
    USERS.append(u['id']);return u

def req(method,path,code=None,body=None):
    return requests.request(method,base.BASE+'/api/auth/'+path,headers={'Authorization':'Bearer '+code} if code else {},json=body,timeout=20)

def main():
    global ORG
    base._serve()
    try:
        personal=user(0);mate=user(1);outsider=user(2)
        codes=[]
        for u,name in [(personal,'Research'),(personal,'Writer'),(mate,'Reviewer'),(outsider,'Private')]:
            codes.append(accounts.create_agent_token(u['id'],name)[0])
        a,b,c,d=codes
        check(req('GET','town').status_code==401,'anonymous town is protected (401)')
        initial=req('GET','town',a).json()
        check(initial['office']['kind']=='personal','individual occupies a high-rise suite')
        address=initial['office'].copy()
        ai=req('GET','agent-self',a).json()['agent_id'];bi=req('GET','agent-self',b).json()['agent_id'];ci=req('GET','agent-self',c).json()['agent_id'];di=req('GET','agent-self',d).json()['agent_id']
        check(ai!=bi,'two codes under one account have distinct stable agent IDs')
        seats=req('GET','office-agents',a).json()['seats']
        check({s['agent_id'] for s in seats}=={ai,bi},'personal office contains only its own agents')
        check(req('GET','agent-tokens',a).status_code==403,'agent cannot read sibling credentials')
        check(req('POST','agent-tokens',a,{'label':'Escalate'}).status_code==403,'agent cannot mint credentials')
        for code,state,task in [(a,'working','Research report'),(b,'done','Draft delivered')]:
            check(req('POST','office-work',code,{'state':state,'task':task}).status_code==201,'independent work report HTTP 201')
        seats=req('GET','office-agents',a).json()['seats'];byid={s['agent_id']:s for s in seats}
        check(byid[ai]['updates'][0]['task']=='Research report' and byid[bi]['updates'][0]['task']=='Draft delivered','tasks and histories never merge between sibling agents')
        check(req('PATCH','office-agents/'+str(bi),a,{'name':'Hijacked','animal':'cat','cloth':'red'}).status_code==403,'agent cannot edit a sibling')
        check(req('PATCH','office-agents/'+str(ai),a,{'name':'Research','animal':'tiger','cloth':'blue'}).status_code==200,'agent can customize its own identity')
        foreign=req('GET','town',d).json()['office']['id']
        check(req('GET','office-agents?office_id='+str(foreign),a).status_code==403,'other office remains private (403)')
        msg={'recipient_id':bi,'kind':'handoff','subject':'Review draft','body':'Please verify the totals.','client_id':'handoff-1'}
        sent=req('POST','agent-messages',a,msg);check(sent.status_code==201,'same-office task handoff stored (201)');mid=sent.json()['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda _:req('POST','agent-messages',a,msg).json()['id'],range(4)))
        check(set(ids)=={mid},'concurrent retries create one handoff')
        check(req('POST','agent-messages',a,{**msg,'body':'changed'}).status_code==400,'idempotency key cannot silently change payload')
        check(req('POST','agent-messages',a,{**msg,'client_id':'cross','recipient_id':di}).status_code==403,'cross-office message blocked (403)')
        check(req('PATCH','agent-messages/'+str(mid),a,{'state':'accepted'}).status_code==403,'sender cannot accept on recipient behalf')
        check(req('PATCH','agent-messages/'+str(mid),b,{'state':'completed'}).status_code==400,'task cannot jump straight from pending to completed')
        check(req('PATCH','agent-messages/'+str(mid),b,{'state':'accepted'}).status_code==200,'recipient accepts handoff')
        check(req('PATCH','agent-messages/'+str(mid),b,{'state':'completed'}).status_code==200,'recipient completes handoff')
        check(req('PATCH','agent-messages/'+str(mid),b,{'state':'accepted'}).status_code==400,'completed tasks cannot regress to accepted')
        check(req('GET','agent-inbox',b).json()['messages'][0]['state']=='completed','sender and receiver see durable task state')
        # More than one page of incoming messages cannot be lost behind newer ones.
        with accounts._db() as conn,conn.cursor() as q:
            for i in range(105):
                q.execute("INSERT INTO agent_messages(office_id,sender_id,recipient_id,kind,subject,body,client_id) VALUES(%s,%s,%s,'message','Queue','Check',%s)",(address['id'],ai,bi,'queue-'+str(i)))
        inbox=req('GET','agent-inbox?pending_only=true',b).json()['messages']
        check(len(inbox)==100 and inbox[0]['client_id']=='queue-0','pending inbox drains oldest first beyond 100 messages')
        check(req('PATCH','agent-messages/'+str(inbox[0]['id']),b,{'state':'read'}).status_code==200,'ordinary message acknowledged as read')
        check(req('GET','agent-inbox?pending_only=true',b).json()['messages'][0]['client_id']=='queue-1','acknowledgment advances the backlog')
        # Growth is stable across registrations and requests running concurrently.
        free=sum(x['capacity']-x['occupied'] for x in initial['buildings'] if x['kind']=='personal')
        for i in range(3,3+free+1):user(i)
        with ThreadPoolExecutor(max_workers=4) as pool:
            places=list(pool.map(lambda _:req('GET','town',a).json()['office'],range(4)))
        check(all(x==address for x in places),'registration growth and concurrent allocation preserve existing address')
        expanded=req('GET','town',a).json()
        check(expanded['total_buildings']>initial['total_buildings'],'new registrations expand town with another building')
        check('example.test' not in json.dumps(expanded) and 'Research report' not in json.dumps(expanded),'map contains no other users emails or work content')
        # Team formation moves both members to one low-rise office, with isolation.
        ORG=orgs.create_org(PREFIX+' studio',slug=PREFIX)['id']
        with accounts._db() as conn,conn.cursor() as q:
            for u in [personal,mate]:
                q.execute("INSERT INTO org_members(org_id,user_id,email,status) VALUES(%s,%s,%s,'active')",(ORG,u['id'],u['email']))
        team=req('GET','town',a).json();check(team['office']['kind']=='team','team occupies a multi-storey campus')
        check(req('GET','town',c).json()['office']['id']==team['office']['id'],'teammates share the same office address')
        check({s['agent_id'] for s in req('GET','office-agents',c).json()['seats']}=={ai,bi,ci},'team sees all and only its member agents')
        check(req('GET','agent-inbox',b).json()['messages']==[],'old private-office conversations stay private after transfer')
        msg2={**msg,'recipient_id':ci,'client_id':'team-review'}
        check(req('POST','agent-messages',a,msg2).status_code==201,'team agents can communicate across member accounts')
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1500,'height':1000});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base.WEB)
            page.evaluate('''async c=>{await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)})}''',{'email':personal['email'],'password':base.PASSWORD})
            page.reload();page.wait_for_selector('.town-building');page.evaluate("kladoI18n.setLang('en')")
            check(page.locator('#town-view').is_visible() and not page.locator('#town-interior').is_visible(),'opening view shows town map')
            check(page.locator('.town-building').count()>=2,'map draws both occupied building types')
            page.locator('.town-building.is-mine').click()
            check(page.locator('#town-info h2').inner_text()==PREFIX+' studio','clicking the map selects the correct building')
            page.screenshot(path='/private/tmp/town-map-en.png')
            page.locator('#town-info').get_by_role('button',name='Enter office').click()
            page.wait_for_selector('.off-row');check(page.locator('.off-row').count()==3,'enter office renders one desk per agent')
            page.wait_for_selector('.town-message')
            check(page.locator('.town-message').count()==1,'office UI shows only actual current-office messages')
            page.locator('.town-agent-card').filter(has_text='Writer').get_by_role('button',name='Edit').click()
            page.locator('#town-agent-name').fill('Studio Writer');page.locator('[data-animal="rabbit"]').click();page.locator('[data-cloth="green"]').click()
            page.screenshot(path='/private/tmp/town-agent-editor.png')
            page.locator('#town-agent-dialog').get_by_role('button',name='Save',exact=True).click()
            page.wait_for_function("document.querySelector('#town-office-tools').textContent.includes('Studio Writer')")
            check(next(s for s in req('GET','office-agents',a).json()['seats'] if s['agent_id']==bi)['avatar']=={'animal':'rabbit','cloth':'green'},'owner can save independent agent appearance with real UI')
            page.screenshot(path='/private/tmp/town-office-en.png')
            # Create through the real browser and HTTP endpoint, suppress fixture email only.
            page.locator('#town-office-bar').get_by_role('button',name='Connect an agent').click()
            page.locator('#agent-label').fill('Planner')
            with patch('routers.auth.mailer.send_agent_code',return_value='unconfigured'):
                with page.expect_response(lambda r:r.url.endswith('/api/auth/agent-tokens') and r.request.method=='POST') as created:
                    page.locator('#agent-modal-bg').get_by_role('button',name=re.compile('Create agent')).click()
                check(created.value.status==200,'browser creates a separate agent access code')
                planner_code=created.value.json()['code']
            page.wait_for_function("document.querySelector('#agent-list').textContent.includes('Planner')")
            check(not re.search(r'[\u4e00-\u9fff]',page.locator('#agent-modal-bg').inner_text()),'agent connection dialog uses English consistently')
            page.evaluate('authGate.closeAgentCodes()')
            page.wait_for_function("document.querySelectorAll('.town-agent-card').length===4")
            check(req('POST','agent-heartbeat',planner_code).status_code==200,'new browser-created code connects its own agent')
            check(req('GET','agent-self',planner_code).json()['agent_id'] not in {ai,bi,ci},'new agent gets a distinct identity immediately')
            page.locator('#town-office-bar').get_by_role('button',name='Back to town').click();page.wait_for_selector('#town-map')
            page.evaluate("kladoI18n.setLang('zh')");check('进入办公室' in page.locator('#town-view').inner_text(),'town follows Chinese language')
            page.screenshot(path='/private/tmp/town-map-zh.png')
            page.evaluate("document.documentElement.dataset.theme='dark'");page.screenshot(path='/private/tmp/town-map-dark.png')
            page.set_viewport_size({'width':390,'height':844});page.locator('.town-map-controls').get_by_role('button',name='定位',exact=True).click();page.screenshot(path='/private/tmp/town-map-mobile.png')
            check(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'town has no mobile horizontal overflow')
            check(not errors,'no uncaught JavaScript errors: '+str(errors));browser.close()
        # Membership changes and revoked credentials take effect immediately.
        with accounts._db() as conn,conn.cursor() as q:q.execute("UPDATE org_members SET status='disabled' WHERE user_id=%s",(mate['id'],))
        check(req('GET','office-agents',c).status_code==403,'disabled teammate loses office access')
        check(req('POST','agent-messages',a,{**msg2,'client_id':'disabled'}).status_code==403,'disabled teammate no longer receives messages')
        tokenid=next(t['id'] for t in accounts.list_agent_tokens(personal['id']) if t['label']=='Research');accounts.revoke_agent_token(tokenid,user_id=personal['id'])
        check(any(req('POST','agent-heartbeat',code).status_code==401 for code in [a,b]),'revoked agent credential no longer authenticates')
        print('TOWN + MULTI-AGENT HTTP/UI: ALL PASSED')
    finally:
        with accounts._db() as conn,conn.cursor() as q:
            q.execute('DELETE FROM access_log WHERE user_id=ANY(%s)',(USERS,))
            q.execute('DELETE FROM office_agents WHERE user_id=ANY(%s)',(USERS,))
            q.execute('DELETE FROM agent_tokens WHERE user_id=ANY(%s)',(USERS,))
            q.execute('DELETE FROM org_members WHERE user_id=ANY(%s)',(USERS,))
            q.execute('DELETE FROM app_users WHERE id=ANY(%s)',(USERS,))
            q.execute('DELETE FROM town_offices WHERE owner_key=ANY(%s)',(['user:'+str(i) for i in USERS]+(['team:'+str(ORG)] if ORG else []),))
            if ORG:q.execute('DELETE FROM orgs WHERE id=%s',(ORG,))

if __name__=='__main__':main()
