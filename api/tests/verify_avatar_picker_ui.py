"""Real clicks and HTTP persistence, with browser and agent credentials.
Run AUTH_ENABLED=true python api/tests/verify_avatar_picker_ui.py.
Uses a disposable local account, never a production user's preferences.
"""
import os
os.environ['OFFICE_PORT']='18917'
os.environ['KNOWLEDGE_DOCS_AUTOAPPLY']='0'
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verify_office_ui as office
import requests
from playwright.sync_api import sync_playwright


def check(value,message):
    assert value,message
    print('PASS '+message,flush=True)


def main():
    office.ADMIN=('Avatar acceptance','avatar-picker-accept@example.test')
    office._serve()
    uid=office._prepare()
    try:
        # A normal user must be able to change only their own appearance.
        with office.auth_store._db() as c,c.cursor() as q:
            q.execute("UPDATE app_users SET role='user' WHERE id=%s",(uid,))
        token,_=office.auth_store.create_agent_token(uid,label='avatar acceptance')
        headers={'Authorization':'Bearer '+token}
        url=office.BASE+'/api/auth/me/avatar'
        check(requests.patch(url,json={'animal':'cat','cloth':'blue'}).status_code==401,'anonymous save rejected: HTTP 401')
        check(requests.patch(url,headers=headers,json={'animal':'cat','cloth':'blue','user_id':uid+1}).status_code==422,'targeting another account rejected: HTTP 422')
        check(requests.patch(url,headers=headers,json={'animal':'invalid','cloth':'blue'}).status_code==400,'invalid avatar rejected: HTTP 400')
        check(requests.patch(office.BASE+'/api/auth/admin/users/'+str(uid),headers=headers,json={'role':'admin'}).status_code==403,'agent cannot grant admin privileges: HTTP 403')
        check(requests.patch(url,auth=(office.ADMIN[1],office.PASSWORD),json={'animal':'tiger','cloth':'blue'}).status_code==200,'Basic credential saves its own appearance: HTTP 200')
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1440,'height':1000})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(office.WEB)
            page.evaluate('''async c=>{await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)})}''',{'email':office.ADMIN[1],'password':office.PASSWORD})
            page.reload()
            page.wait_for_function('window.__authUser && window.AgentOfficeAvatar')
            page.evaluate("kladoI18n.setLang('en')")
            page.locator('#nav-avatar').click()
            menu=page.locator('#auth-menu').bounding_box()
            check(menu['x']>=0 and menu['x']+menu['width']<=1440,'first opening measures full menu width without clipping')
            def click_save(selector,index,animal,cloth):
                with page.expect_response(lambda r:r.url.endswith('/me/avatar') and r.request.method=='PATCH') as response:
                    page.locator(selector).nth(index).click()
                check(response.value.status==200,'real click saves '+animal+' / '+cloth+': HTTP 200')
                page.wait_for_function("document.querySelector('#au-avatar-status').textContent.startsWith('Saved.')")
                stored=requests.get(office.BASE+'/api/auth/me',headers=headers).json()['user']
                check((stored['avatar_animal'],stored['avatar_cloth'])==(animal,cloth),'server retains '+animal+' / '+cloth)
            click_save('.au-avatar-cell',3,'cat','blue')
            # Same browser with an agent Bearer was the actual production 403 path.
            page.evaluate('(v)=>sessionStorage.setItem("klado.accessToken",v)',token)
            click_save('.au-cloth-cell',3,'cat','green')
            for i,animal in enumerate(['tiger','ox','horse','cat','rabbit','hippo']):
                click_save('.au-avatar-cell',i,animal,'green')
            for i,cloth in enumerate(['red','blue','yellow','green','black']):
                click_save('.au-cloth-cell',i,'hippo',cloth)
            check(page.locator('.au-avatar-cell svg').evaluate_all("es=>es.every(e=>e.getBoundingClientRect().height>=80 && e.style.getPropertyValue('--shirt').includes('black'))"),'all six large previews wear the selected color')
            check('/' not in page.locator('#au-avatar').inner_text(),'English picker has no bilingual labels')
            page.locator('#nav-avatar').click();page.locator('#nav-avatar').click()
            check(page.locator('.au-avatar-cell.is-on').get_attribute('title')=='Hippo','reopening retains chosen animal')
            page.reload();page.wait_for_function('window.__authUser && window.AgentOfficeAvatar')
            page.locator('#nav-avatar').click()
            check(page.locator('.au-cloth-cell.is-on').get_attribute('title')=='Black','reload retains saved color')
            # A failed request rolls back the preview and offers one inline retry.
            page.route('**/api/auth/me/avatar',lambda r:r.fulfill(status=503,json={'detail':'fixture outage'}))
            page.locator('.au-cloth-cell').nth(0).click()
            page.locator('#au-avatar-retry').wait_for(state='visible')
            check(page.locator('.au-cloth-cell.is-on').get_attribute('title')=='Black','failed save restores confirmed choice')
            page.unroute('**/api/auth/me/avatar')
            page.locator('#au-avatar-retry').click()
            page.wait_for_function("document.querySelector('#au-avatar-status').textContent.startsWith('Saved.')")
            check(page.locator('.au-cloth-cell.is-on').get_attribute('title')=='Red','retry saves the failed choice')
            page.screenshot(path='/private/tmp/avatar-picker-en.png')
            page.evaluate("kladoI18n.setLang('zh')")
            check(page.locator('#au-avatar-title').inner_text()=='办公室形象','open picker title follows Chinese language')
            check(page.locator('.au-avatar-cell').nth(4).inner_text()=='兔子','animal names follow Chinese language')
            check(page.locator('.au-cloth-cell').nth(0).inner_text()=='红','color names follow Chinese language')
            page.screenshot(path='/private/tmp/avatar-picker-zh.png')
            page.evaluate("document.documentElement.dataset.theme='dark'")
            page.screenshot(path='/private/tmp/avatar-picker-dark.png')
            page.set_viewport_size({'width':390,'height':700})
            menu=page.locator('#auth-menu').bounding_box()
            check(menu['x']>=0 and menu['x']+menu['width']<=390 and menu['y']+menu['height']<=700,'mobile menu stays within viewport and scrolls')
            page.locator('.au-cloth-cell').nth(0).scroll_into_view_if_needed()
            page.screenshot(path='/private/tmp/avatar-picker-mobile.png')
            check(not errors,'no uncaught browser errors')
            browser.close()
        print('AVATAR PICKER HTTP + UI: ALL PASSED')
    finally:
        with office.auth_store._db() as c,c.cursor() as q:
            q.execute('DELETE FROM agent_tokens WHERE user_id=%s',(uid,))
        office._cleanup()

if __name__=='__main__':main()
