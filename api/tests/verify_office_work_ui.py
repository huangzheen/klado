"""Real HTTP + PostgreSQL + browser acceptance for office reports and redesigned scene.
Creates only uniquely named fixtures; removes them in finally. Run with AUTH_ENABLED=true.
"""
import os
os.environ["OFFICE_PORT"] = "18915"
os.environ["KNOWLEDGE_DOCS_AUTOAPPLY"] = "0"
import sys
import json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verify_office_ui import _serve, BASE, PASSWORD
from services import auth_store
import requests
from playwright.sync_api import sync_playwright

PREFIX = 'office-work-accept-'
IDS = []

def check(condition, message):
    assert condition, message
    print('PASS '+message, flush=True)


def check_character_clearance(page, label):
    result=page.evaluate("""() => {
      const people=[...document.querySelectorAll('.off-agent')].filter(e=>e.getAttribute('opacity')!=='0');
      return {opaque:people.every(e=>getComputedStyle(e).opacity==='1'),
        away:people.filter(e=>e.classList.contains('st-idle')).length,
        muted:people.filter(e=>e.classList.contains('st-idle')).every(e=>getComputedStyle(e.querySelector('.off-person')).filter==='saturate(0.4)')};
    }""")
    check(result['away']>0 and result['opaque'] and result['muted'],label+' has opaque active and muted away characters')
    check(page.locator('.off-nameplate').count()==0,label+' has no duplicate desk nameplates')


def main():
    auth_store._ensure_schema()
    try:
        names = ['Lark','Atlas','Milo','Luna','Clover','Mochi']
        animals = ['tiger','ox','horse','cat','rabbit','hippo']
        with auth_store._db() as conn, conn.cursor() as cur:
            for i,name in enumerate(names):
                cur.execute('INSERT INTO app_users (email,password_hash,display_name,avatar_animal,avatar_cloth) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                            (f'{PREFIX}{i}@example.test',auth_store._hash(PASSWORD),name,animals[i],['blue','green','black','red','yellow','blue'][i]))
                IDS.append(cur.fetchone()[0])
            conn.commit()
        _serve()
        payloads = [
            dict(state='working',task='Research market signals',summary='Comparing three public sources.'),
            dict(state='done',task='Weekly product brief',summary='Published the weekly summary.'),
            dict(state='waiting',task='Review campaign direction',summary='Waiting for feedback on the proposed direction.'),
            dict(state='working',task='Prepare launch materials',summary='Drafting the product overview.'),
            dict(state='idle',task='Ready for the next task'),
            dict(state='error',task='Refresh analytics',summary='Data source unavailable; needs attention.')]
        check(requests.post(BASE+'/api/auth/office-work',json=payloads[0]).status_code==401,'anonymous writes rejected (HTTP 401)')
        for i,payload in enumerate(payloads):
            response=requests.post(BASE+'/api/auth/office-work',auth=(f'{PREFIX}{i}@example.test',PASSWORD),json=payload)
            check(response.status_code==201, f'persisted {payload["state"]} (HTTP {response.status_code})')
        creds=(f'{PREFIX}0@example.test',PASSWORD)
        check(requests.post(BASE+'/api/auth/office-work',auth=creds,json={**payloads[0],'user_id':IDS[1]}).status_code==422,'cannot write another account')
        token, _ = auth_store.create_agent_token(IDS[0], label="office acceptance")
        for i in range(14):
            response=requests.post(BASE+'/api/auth/office-work',headers={'Authorization':'Bearer '+token},json={**payloads[0], 'summary':f'Progress {i}'})
            check(response.status_code==201, f'agent bearer report {i+1} accepted')
        data=requests.get(BASE+'/api/auth/office-seats',auth=creds).json()
        own=[h for h in data['seats'] if h['user_id'] in IDS]
        check(len(own[0]['updates'])==12 and own[0]['updates'][0]['summary']=='Progress 13','history is newest first and capped at twelve')
        check(len(own)==6 and all(h['presence']=='active' and h['updates'] for h in own),'real seat response includes six active reporting accounts')
        check(all('user_id' not in e and 'email' not in e for h in own for e in h['updates']),'updates expose only public work fields')
        # Exercise away figures explicitly; live writes above intentionally made all six active.
        own[0]['presence']='idle'
        own[5]['presence']='idle'
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1600,'height':1000},device_scale_factor=1)
            page.emulate_media(reduced_motion='reduce')
            errors=[]
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE+'/?welcome=1')
            page.evaluate('''async c => {await fetch('api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)})}''',dict(email=creds[0],password=creds[1]))
            # Scene fixture isolates the six REAL HTTP-created seats from unrelated local accounts.
            page.route('**/api/auth/office-seats',lambda route:route.fulfill(json={**data,'seats':own}))
            page.reload()
            page.wait_for_function('window.AgentOffice && document.querySelectorAll(".off-row").length === 6')
            page.evaluate("kladoI18n.setLang('en')")
            page.wait_for_timeout(400)
            close_width=page.locator('#off-svg').get_attribute('viewBox').split()[2]
            page.screenshot(path='/private/tmp/office-default-close.png',full_page=True)
            page.locator('#off-overview').click()
            wide_width=page.locator('#off-svg').get_attribute('viewBox').split()[2]
            check(float(close_width)<float(wide_width)*.7,'opening camera is closer than the overview')
            geometry=page.locator('.off-desk').evaluate_all("""es=>es.every(e=>{
              const center=q=>{const r=e.querySelector(q).getBBox();return r.y+r.height/2;};
              const top=e.querySelector('.off-desktop').getBBox();
              const legs=[...e.querySelectorAll('.off-desk-leg')];
              return legs.length===4 && center('.off-chair-seat')>center('.off-keyboard') && center('.off-keyboard')>center('.screen') && top.height>0;
            })""")
            check(geometry,'every row has four desk legs and chair / keyboard / screen in the correct order')
            check(page.locator('.workspace-bar .off-stat').count()==4,'four counters are in the title bar')
            check(page.locator('#page-home .off-stats').count()==0,'counters no longer consume room height')
            check(page.locator('.off-label .who').all_text_contents()[:6]==names,'single floating cards retain agent names')
            check(page.locator('.off-label .m').all_text_contents()[:6]==[f'0{i+1}' for i in range(6)],'floating cards retain desk numbers')
            check(page.locator('#off-stage').bounding_box()['height']>700,'3D viewport gains the former counter row height')
            page.locator('.off-row').nth(1).click()
            check('Weekly product brief' in page.locator('#off-detail').inner_text(),'real mouse selection opens completed work and summary')
            check('Published the weekly summary.' in page.locator('#off-detail').inner_text(),'completion outcome is visible')
            check_character_clearance(page,'Light room')
            page.screenshot(path='/private/tmp/office-upgrade-light.png',full_page=True)
            page.evaluate("document.documentElement.dataset.theme='dark';document.documentElement.dataset.bsTheme='dark'")
            page.wait_for_timeout(400)
            check_character_clearance(page,'Dark room')
            page.screenshot(path='/private/tmp/office-upgrade-dark.png',full_page=True)
            check(page.locator('.off-profile svg').is_visible(),'selected agent has a large portrait')
            page.evaluate("kladoI18n.setLang('zh')")
            check('最近记录' in page.locator('#off-detail').inner_text(),'detail follows language changes')
            page.locator('#off-zoom-in').click()
            check_character_clearance(page,'Zoomed room')
            page.locator('#off-overview').click()
            page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
            check(page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'mobile has no horizontal overflow')
            page.screenshot(path='/private/tmp/office-upgrade-mobile.png',full_page=True)
            page.set_viewport_size({'width':1600,'height':1000})
            # Real animation, sampled over time; reported state remains unchanged.
            page.emulate_media(reduced_motion='no-preference')
            page.wait_for_function("document.querySelector('.off-agent[data-seat=\"4\"]').dataset.motion === 'working'")
            worker=page.locator('.off-agent[data-seat="4"]')
            check(worker.evaluate("e => getComputedStyle(e.querySelector('.sit-legs')).display !== 'none' && getComputedStyle(e.querySelector('.stand-legs')).display === 'none'"),'working agent visibly sits with bent legs')
            hand0=worker.locator('.typing-hands').evaluate("e=>getComputedStyle(e).transform")
            page.wait_for_timeout(170)
            check(hand0!=worker.locator('.typing-hands').evaluate("e=>getComputedStyle(e).transform"),'seated working hands animate')
            idle=page.locator('.off-agent[data-seat="1"]')
            p0=idle.get_attribute('transform');page.wait_for_timeout(700)
            check(p0!=idle.get_attribute('transform'),'idle agent walks through the office')
            page.wait_for_function("document.querySelectorAll('.off-agent.is-chatting').length >= 2",timeout=16000)
            check(page.locator('.off-agent.is-chatting .chat-bubble').first.is_visible(),'neighboring idle agents show chat gestures')
            page.screenshot(path='/private/tmp/office-motion-chat.png',full_page=True)
            page.evaluate("document.documentElement.dataset.theme='light';document.documentElement.dataset.bsTheme='light'")
            page.screenshot(path='/private/tmp/office-motion-light.png',full_page=True)
            own[0]['presence']='active';own[0]['work_stale']=False
            page.evaluate('AgentOffice.refresh()')
            page.wait_for_function("document.querySelector('.off-agent[data-seat=\"1\"]').dataset.motion === 'working'",timeout=15000)
            check(idle.evaluate("e=>e.classList.contains('is-seated') && !e.classList.contains('is-chatting')"),'a new task brings the agent back to its seat')
            page.locator('#off-motion').click();page.wait_for_timeout(100)
            poses=page.locator('.off-agent[opacity="1"]').evaluate_all("es=>es.map(e=>e.getAttribute('transform'))")
            page.wait_for_timeout(500)
            check(poses==page.locator('.off-agent[opacity="1"]').evaluate_all("es=>es.map(e=>e.getAttribute('transform'))"),'motion toggle stops movement')
            check(page.locator('#off-motion').get_attribute('aria-pressed')=='false','motion toggle reports paused state')
            page.locator('#off-motion').click()
            page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(100)
            poses=page.locator('.off-agent[opacity="1"]').evaluate_all("es=>es.map(e=>e.getAttribute('transform'))")
            page.wait_for_timeout(500)
            check(poses==page.locator('.off-agent[opacity="1"]').evaluate_all("es=>es.map(e=>e.getAttribute('transform'))"),'reduced motion stops position animation')
            page.evaluate("navTo(null,'system-settings','Settings')")
            check(not page.locator('.off-stats').is_visible(),'office counters hide on another page')
            page.evaluate("navTo(null,'home','Home')")
            check(page.locator('.off-stats').is_visible(),'office counters return with the office')
            own[0]['presence']='idle'
            # Refresh must remove departed accounts, without resetting room size.
            page.unroute('**/api/auth/office-seats')
            page.route('**/api/auth/office-seats',lambda route:route.fulfill(json={**data,'seats':own[:1]}))
            page.evaluate('AgentOffice.refresh()')
            check(page.locator('.off-row').count()==1 and page.locator('.off-agent[opacity="1"]').count()==1,'refresh removes stale occupants')
            page.unroute('**/api/auth/office-seats')
            page.route('**/api/auth/office-seats',lambda route:route.fulfill(status=503,json={'detail':'fixture unavailable'}))
            page.evaluate('AgentOffice.refresh()')
            check(page.locator('.off-row').count()==1 and '同步失败' in page.locator('#off-load').inner_text(),'failed refresh preserves data and shows a retry message')
            page.unroute('**/api/auth/office-seats')
            dense = [{**own[i%6], 'user_id':100000+i, 'name':f'Agent {i+1}'} for i in range(39)]
            page.route('**/api/auth/office-seats',lambda route:route.fulfill(json={**data,'seats':dense}))
            page.evaluate('AgentOffice.refresh()')
            page.wait_for_timeout(300)
            page.locator('#off-overview').click()
            overlaps=page.evaluate("""() => {const rs=[...document.querySelectorAll('.off-label:not(.is-off)')].map(e=>e.getBoundingClientRect()); let n=0; for(let i=0;i<rs.length;i++) for(let j=i+1;j<rs.length;j++) {const a=rs[i],b=rs[j]; if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1) n++;} return n;}""")
            check_character_clearance(page,'Full room of 39 agents')
            check(overlaps==0,'full room of 39 agents has no overlapping labels: '+str(overlaps))
            # Sample all moving feet against the actual projected floor polygon.
            page.emulate_media(reduced_motion='no-preference')
            for sample in range(12):
                page.wait_for_timeout(700)
                outside=page.evaluate("""() => {
                  const poly=[...document.querySelector('#off-floor-clip polygon').points];
                  function inside(x,y){let v=false;for(let i=0,j=poly.length-1;i<poly.length;j=i++){
                    const a=poly[i],b=poly[j];if((a.y>y)!==(b.y>y)&&x<(b.x-a.x)*(y-a.y)/(b.y-a.y)+a.x)v=!v;}return v;}
                  return [...document.querySelectorAll('.off-agent[opacity="1"]')].filter(e=>{
                    const m=e.transform.baseVal.consolidate().matrix;return !inside(m.e,m.f);
                  }).length;
                }""")
                check(outside==0,f'39-agent animation stays on the floor, sample {sample+1}')
            leg_bounds=page.locator('.off-agent.is-walking').evaluate_all("""es=>es.every(e=>{
              const p=e.querySelector('.off-shadow').getBoundingClientRect();
              return [...e.querySelectorAll('.leg')].every(l=>{const r=l.getBoundingClientRect();return Math.abs(r.x-p.x)<30&&Math.abs(r.y-p.y)<30;});
            })""")
            check(leg_bounds,'walking legs remain attached to the character, not orbiting the SVG')
            page.wait_for_timeout(150)
            collisions=page.evaluate("""() => {
              const cards=[...document.querySelectorAll('.off-label:not(.is-off):not(.is-crowded)')].map(e=>e.getBoundingClientRect());
              let n=0;for(let i=0;i<cards.length;i++)for(let j=i+1;j<cards.length;j++){
                const a=cards[i],b=cards[j];if(a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top)n++;
              }return n;
            }""")
            check(collisions==0,'moving name cards avoid each other in a full room')
            page.screenshot(path='/private/tmp/office-motion-dense.png',full_page=True)
            check(not errors,'no browser JavaScript errors: '+str(errors))
            browser.close()
        print('OFFICE WORK HTTP + UI: ALL PASSED')
    finally:
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM access_log WHERE user_id = ANY(%s)',(IDS,))
            cur.execute('DELETE FROM agent_tokens WHERE user_id = ANY(%s)',(IDS,))
            cur.execute('DELETE FROM app_users WHERE id = ANY(%s)',(IDS,))
            conn.commit()

if __name__=='__main__': main()
