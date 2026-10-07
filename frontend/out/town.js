/* Persistent town locations come only from the API. No invented occupants. */
(function(){
  'use strict';
  var town=null,officeData=null,inOffice=false,selected=null,view=null,drag=null;
  var host=document.getElementById('town-view'),tools=document.getElementById('town-office-tools');
  var t=function(s){return window.kladoI18n?kladoI18n.t(s):s;};
  function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
  function button(text,fn,cls){var b=el('button',cls||'btn btn-ghost',t(text));b.type='button';b.addEventListener('click',fn);return b;}
  function svg(tag,attrs,text){var e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.keys(attrs||{}).forEach(function(k){e.setAttribute(k,attrs[k]);});if(text!==undefined)e.textContent=text;return e;}
  async function api(path,init){var r=await fetch(appAbsUrl('api/auth/'+path),Object.assign({credentials:'same-origin'},init||{}));if(!r.ok){var e=new Error(t(r.status===403?'无权访问这个办公室或 Agent / This office or agent is private':'请求失败，请重试 / Request failed. Please retry.'));throw e;}return r.json();}
  function address(o){return t(o.kind==='team'?'团队办公楼 / Team office':'写字楼 / Tower')+' '+o.building_id+(o.kind==='team'?'':(' · '+t('楼层 / Floor')+' '+o.floor+' · '+t('单元 / Suite')+' '+o.unit));}
  function officeTitle(o){return o.name+' · '+address(o);}
  function pt(x,y,z){return [x-y,(x+y)*.5-(z||0)];}
  function poly(points,fill){return svg('polygon',{points:points.map(function(p){return pt.apply(null,p).join(',');}).join(' '),fill:fill,stroke:'var(--off-edge)','stroke-width':.6});}
  function box(g,x,y,w,d,z,h,type){
    g.appendChild(poly([[x,y,z+h],[x+w,y,z+h],[x+w,y+d,z+h],[x,y+d,z+h]],'var(--off-m-'+type+')'));
    g.appendChild(poly([[x+w,y,z+h],[x+w,y+d,z+h],[x+w,y+d,z],[x+w,y,z]],'var(--off-m-'+type+'-a)'));
    g.appendChild(poly([[x,y+d,z+h],[x+w,y+d,z+h],[x+w,y+d,z],[x,y+d,z]],'var(--off-m-'+type+'-b)'));
  }
  function location(id){var n=id-1;return {x:(n%6)*210,y:Math.floor(n/6)*210};}
  function tree(g,x,y){box(g,x,y,4,4,0,15,'wood');var p=pt(x+2,y+2,22);g.appendChild(svg('ellipse',{cx:p[0],cy:p[1],rx:12,ry:16,fill:'var(--off-m-plant)'}));}
  function drawBuilding(root,b){
    var p=location(b.id),x=p.x+36,y=p.y+34,high=b.kind==='personal',h=high?164:72,w=high?66:112,d=high?70:82;
    var g=svg('g',{'class':'town-building'+(b.mine?' is-mine':''),'data-building-id':b.id,role:'button',tabindex:0,'aria-label':t(high?'写字楼 / Tower':'团队办公楼 / Team office')+' '+b.id});
    g.appendChild(svg('title',{},t(high?'个人办公室 · 高层写字楼 / Personal offices · Tower':'团队办公室 · 多层办公楼 / Team offices · Campus')));
    box(g,p.x+12,p.y+12,174,174,0,5,'floor');
    box(g,x,y,w,d,5,h,high?'wall':'wood');
    box(g,x+5,y+5,w-10,d-10,h+5,6,'chair');
    for(var floor=0;floor<(high?6:3);floor++){
      var z=17+floor*(high?24:19);
      for(var col=0;col<(high?3:5);col++){
        var wx=x+7+col*(high?19:20);
        g.appendChild(poly([[wx,y+d+.4,z],[wx+11,y+d+.4,z],[wx+11,y+d+.4,z+13],[wx,y+d+.4,z+13]],'var(--off-glass)'));
      }
      for(var side=0;side<3;side++)g.appendChild(poly([[x+w+.4,y+8+side*20,z],[x+w+.4,y+19+side*20,z],[x+w+.4,y+19+side*20,z+13],[x+w+.4,y+8+side*20,z+13]],'var(--off-glass)'));
    }
    box(g,x+w/2-9,y+d,18,3,5,18,'chair');
    tree(g,p.x+157,p.y+30);tree(g,p.x+153,p.y+148);
    var label=pt(p.x+94,p.y+166,0);
    g.appendChild(svg('rect',{x:label[0]-54,y:label[1]-11,width:108,height:23,rx:11,fill:b.mine?'var(--accent-fill)':'var(--surface)'}));
    g.appendChild(svg('text',{x:label[0],y:label[1]+4,'text-anchor':'middle',fill:b.mine?'var(--text-on-accent)':'var(--text-2)','font-size':10,'font-weight':600},t(high?'写字楼 / Tower':'办公楼 / Campus')+' '+String(b.id).padStart(2,'0')));
    if(b.mine){var tip=pt(x+w/2,y+d/2,h+32);g.appendChild(svg('text',{x:tip[0],y:tip[1],'text-anchor':'middle',fill:'var(--accent)','font-size':12,'font-weight':700},t('我的办公室 / My office')));}
    function choose(){if(drag&&drag.moved)return;selected=b;renderInfo();}
    g.addEventListener('click',choose);g.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();choose();}});root.appendChild(g);
  }
  function setView(){var s=document.getElementById('town-map');if(s&&view)s.setAttribute('viewBox',[view.x,view.y,view.w,view.h].join(' '));}
  function focusMine(){if(!town)return;var p=location(town.office.building_id),c=pt(p.x+90,p.y+90,50);var w=innerWidth<800?500:860;view={x:c[0]-w/2,y:c[1]-w*.315,w:w,h:w*.63};setView();}
  function zoom(f){if(!view)return;var w=Math.max(350,Math.min(4500,view.w*f)),h=w*.63;view.x+=(view.w-w)/2;view.y+=(view.h-h)/2;view.w=w;view.h=h;setView();}
  function renderInfo(){
    var aside=document.getElementById('town-info');if(!aside||!town)return;aside.textContent='';var b=selected||town.buildings.find(function(x){return x.mine;});
    aside.appendChild(el('span','town-eyebrow',t(b&&b.mine?'我的地址 / YOUR ADDRESS':'楼宇信息 / BUILDING')));
    aside.appendChild(el('h2','',b&&b.mine?town.office.name:t(b&&b.kind==='personal'?'高层写字楼 / Office tower':'多层办公楼 / Team campus')));
    if(b)aside.appendChild(el('p','town-address',b.mine?address(town.office):t('楼宇 / Building')+' '+b.id));
    if(b)aside.appendChild(el('p','town-copy',b.occupied+' / '+b.capacity+' '+t('办公室已入驻 / offices occupied')));
    if(b&&b.mine){aside.appendChild(button('进入办公室 / Enter office',enter,'btn btn-primary'));aside.appendChild(el('p','town-copy',t('每个 Agent 一张桌子，各自汇报工作。 / One desk per agent, with independent work updates.')));}
    else aside.appendChild(el('p','town-copy',t('这里是其他用户的办公室，内部工作仅对成员开放。 / This office is private to its members.')));
    var guide=el('div','town-guide');guide.appendChild(el('h3','',t('小镇的两种邻居 / Two ways to move in')));
    guide.appendChild(el('p','',t('高层写字楼 · 个人用户的独立办公室 / Towers · Private offices for individuals')));
    guide.appendChild(el('p','',t('多层办公楼 · 团队成员共享的办公空间 / Campuses · Shared offices for teams')));
    guide.appendChild(el('p','town-copy',t('新注册自动分配地址。楼宇沿街区扩展，已有地址保持不变。 / Registration assigns an address. New blocks grow without moving existing offices.')));aside.appendChild(guide);
  }
  function render(){
    if(!town)return;host.textContent='';
    var hd=el('div','town-heading'),intro=el('div');intro.appendChild(el('span','town-eyebrow','KLADO TOWN'));intro.appendChild(el('h1','',t('每个 Agent，都有自己的位置 / A place for every agent')));
    intro.appendChild(el('p','town-copy',town.total_buildings+' '+t('栋楼宇 / buildings')+' · '+town.total_offices+' '+t('间办公室 / offices')));hd.appendChild(intro);
    var actions=el('div','town-actions');actions.appendChild(button('我的位置 / My address',function(){selected=null;focusMine();renderInfo();}));actions.appendChild(button('刷新 / Refresh',load));actions.appendChild(button('进入办公室 / Enter office',enter,'btn btn-primary'));hd.appendChild(actions);host.appendChild(hd);
    var grid=el('div','town-grid'),canvas=el('div','town-canvas'),s=svg('svg',{id:'town-map',role:'group','aria-label':t('小镇地图 / Town map')});
    var world=svg('g');
    var ids=town.buildings.map(function(b){return b.id;});var max=Math.max.apply(null,ids.concat([6]));var rows=Math.ceil(max/6);
    // Roads and undeveloped garden plots make growth visible without fake tenants.
    box(world,-24,-24,1300,Math.max(440,rows*210+24),-6,6,'plant');
    for(var r=0;r<=rows;r++)world.appendChild(poly([[-24,r*210-12,1],[1276,r*210-12,1],[1276,r*210+4,1],[-24,r*210+4,1]],'var(--off-m-chair-b)'));
    for(var c=0;c<=6;c++)world.appendChild(poly([[c*210-12,-24,1],[c*210+4,-24,1],[c*210+4,rows*210+24,1],[c*210-12,rows*210+24,1]],'var(--off-m-chair-b)'));
    town.buildings.slice().sort(function(a,b){var x=location(a.id),y=location(b.id);return x.x+x.y-y.x-y.y;}).forEach(function(b){drawBuilding(world,b);});
    if(town.buildings.length<4){[2,3,4].filter(function(id){return ids.indexOf(id)<0;}).forEach(function(id){var p=location(id);box(world,p.x+20,p.y+20,150,150,1,2,'floor');for(var k=0;k<3;k++)tree(world,p.x+45+k*40,p.y+70);var z=pt(p.x+95,p.y+130,3);world.appendChild(svg('text',{x:z[0],y:z[1],'text-anchor':'middle',fill:'var(--text-3)','font-size':10},t('预留街区 / Future block')));});}
    s.appendChild(world);canvas.appendChild(s);
    var controls=el('div','town-map-controls');controls.appendChild(button('−',function(){zoom(1.2);}));controls.appendChild(button('+',function(){zoom(1/1.2);}));controls.appendChild(button('定位 / Locate',focusMine));if(town.next_offset!==null)controls.appendChild(button('更多街区 / More blocks',loadMore));canvas.appendChild(controls);
    canvas.appendChild(el('div','town-map-hint',t('拖动探索 · 滚轮缩放 · 点击楼宇 / Drag to explore · Scroll to zoom · Select a building')));
    s.addEventListener('wheel',function(e){e.preventDefault();zoom(e.deltaY>0?1.08:1/1.08);},{passive:false});
    s.addEventListener('pointerdown',function(e){if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,start:Object.assign({},view),moved:false};});
    s.addEventListener('pointermove',function(e){if(!drag)return;var dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4){drag.moved=true;s.setPointerCapture(e.pointerId);}var rect=s.getBoundingClientRect();view.x=drag.start.x-dx*view.w/rect.width;view.y=drag.start.y-dy*view.h/rect.height;setView();});
    s.addEventListener('pointerup',function(){setTimeout(function(){drag=null;},0);});s.addEventListener('pointercancel',function(){drag=null;});
    grid.appendChild(canvas);var aside=el('aside','town-info');aside.id='town-info';grid.appendChild(aside);host.appendChild(grid);if(!view)focusMine();else setView();renderInfo();
  }
  async function load(){try{town=await api('town');selected=null;render();renderOffice();}catch(e){host.textContent='';host.appendChild(el('p','town-copy',e.message));host.appendChild(button('重试 / Retry',load));}}
  async function loadMore(){if(!town||town.next_offset===null)return;try{var more=await api('town?offset='+town.next_offset),seen=new Set(town.buildings.map(function(b){return b.id;}));more.buildings.forEach(function(b){if(!seen.has(b.id))town.buildings.push(b);});town.next_offset=more.next_offset;render();}catch(e){notify(e.message);}}
  function enter(){inOffice=true;host.hidden=true;document.getElementById('town-interior').hidden=false;document.getElementById('town-office-bar').hidden=false;tools.hidden=false;renderOffice();window.dispatchEvent(new Event('resize'));if(window.AgentOffice)AgentOffice.refresh();loadMessages();}
  function leave(){inOffice=false;host.hidden=false;document.getElementById('town-interior').hidden=true;document.getElementById('town-office-bar').hidden=true;tools.hidden=true;load();}
  function notify(message){if(window.kldDialog)kldDialog.notify(message,'err');}
  function renderOffice(){if(!town)return;var bar=document.getElementById('town-office-bar');bar.textContent='';bar.appendChild(button('← 返回小镇 / Back to town',leave));bar.appendChild(el('strong','',officeTitle(town.office)));bar.appendChild(button('接入新 Agent / Connect an agent',function(){authGate.openAgentCodes();},'btn btn-primary'));
    if(!inOffice||!officeData)return;
    tools.textContent='';var directory=el('section','town-directory');directory.appendChild(el('h2','',t('办公室成员 / Office agents')));directory.appendChild(el('p','town-copy',t('每个授权码连接一个 Agent；不要让多个 Agent 共用同一码。 / Use a separate access code for each agent.')));
    var list=el('div','town-agent-list');officeData.seats.forEach(function(a){var row=el('div','town-agent-card');if(window.AgentOfficeAvatar)row.appendChild(AgentOfficeAvatar.thumbnail(a.avatar.animal,a.avatar.cloth));row.appendChild(el('strong','',a.name));row.appendChild(el('small','', 'Agent #'+a.agent_id+(a.legacy?' · '+t('旧接入 / Legacy'):'')));row.appendChild(el('span','',t(a.revoked?'授权已撤销 / Revoked':a.presence==='active'?'近期在线 / Recently online':a.presence==='never'?'等待接入 / Not connected':'离线 / Away')));if(a.editable)row.appendChild(button('编辑 / Edit',function(){editAgent(a);}));list.appendChild(row);});directory.appendChild(list);
    if(!officeData.seats.length)directory.appendChild(el('p','town-copy',t('还没有 Agent。点击“接入新 Agent”，为它命名并生成专属授权码。 / No agents yet. Connect an agent to name it and create its own access code.')));
    tools.appendChild(directory);var mailbox=el('section','town-mailbox');mailbox.id='town-mailbox';tools.appendChild(mailbox);loadMessages();
  }
  async function loadMessages(){if(!inOffice)return;try{var data=await api('office-messages'),box=document.getElementById('town-mailbox');if(!box)return;box.textContent='';box.appendChild(el('h2','',t('消息与任务交接 / Messages & handoffs')));box.appendChild(el('p','town-copy',t('仅展示 Agent 通过接口实际发送的内容。接收方轮询收件箱后确认或完成任务。 / These are actual API messages. Receiving agents poll their inbox to acknowledge or complete tasks.')));
    if(!data.messages.length)box.appendChild(el('p','town-copy',t('还没有消息。接入说明包含消息和交接接口。 / No messages yet. The connection guide includes the messaging API.')));
    var states={pending:'待接收 / Pending',read:'已读 / Read',accepted:'已接受 / Accepted',completed:'已完成 / Completed',declined:'已拒绝 / Declined'};
    data.messages.forEach(function(m){var row=el('article','town-message');row.appendChild(el('small','',m.sender_name+' → '+m.recipient_name+' · '+t(states[m.state])+' · '+new Date(m.updated_at).toLocaleString()));row.appendChild(el('strong','',t(m.kind==='handoff'?'任务交接 / Handoff':'消息 / Message')+' · '+m.subject));row.appendChild(el('p','',m.body));box.appendChild(row);});
  }catch(e){var box=document.getElementById('town-mailbox');if(box){box.textContent=e.message;box.appendChild(button('重试 / Retry',loadMessages));}}}
  function editAgent(a){
    var d=document.getElementById('town-agent-dialog');d.textContent='';var form=el('form');form.appendChild(el('h2','',t('编辑 Agent / Edit agent')));
    var name=el('input');name.value=a.name;name.maxLength=80;name.required=true;name.id='town-agent-name';
    var label=el('label','',t('名称 / Name'));label.htmlFor=name.id;form.appendChild(label);form.appendChild(name);
    var choices=window.AgentOfficeAvatar,draft={animal:a.avatar.animal,cloth:a.avatar.cloth};
    var animals=el('div','town-avatar-grid'),cloths=el('div','town-cloth-grid');
    function paintChoices(){
      animals.textContent='';cloths.textContent='';
      choices.ANIMALS.forEach(function(x){var b=button('',function(){draft.animal=x.key;paintChoices();},'town-avatar-choice');
        b.dataset.animal=x.key;b.setAttribute('aria-label',t(x.label));b.setAttribute('aria-pressed',String(x.key===draft.animal));
        b.appendChild(choices.thumbnail(x.key,draft.cloth));b.appendChild(el('span','',t(x.label)));animals.appendChild(b);});
      choices.CLOTHS.forEach(function(x){var b=button('',function(){draft.cloth=x.key;paintChoices();},'town-cloth-choice');
        b.dataset.cloth=x.key;b.setAttribute('aria-label',t(x.label));b.setAttribute('aria-pressed',String(x.key===draft.cloth));
        var swatch=el('i');swatch.style.setProperty('--c','var(--off-cloth-'+x.key+')');b.appendChild(swatch);b.appendChild(el('span','',t(x.label)));cloths.appendChild(b);});
    }
    form.appendChild(el('label','',t('形象 / Avatar')));form.appendChild(animals);
    form.appendChild(el('label','',t('衣服颜色 / Shirt color')));form.appendChild(cloths);paintChoices();
    var error=el('p','town-copy');error.setAttribute('role','status');form.appendChild(error);var actions=el('div','town-actions');actions.appendChild(button('取消 / Cancel',function(){d.close();}));var save=el('button','btn btn-primary',t('保存 / Save'));save.type='submit';actions.appendChild(save);form.appendChild(actions);
    form.addEventListener('submit',async function(e){e.preventDefault();save.disabled=true;try{await api('office-agents/'+a.agent_id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.value,animal:draft.animal,cloth:draft.cloth})});d.close();await AgentOffice.refresh();}catch(err){error.textContent=err.message;}finally{save.disabled=false;}});d.appendChild(form);d.showModal();
  }
  document.addEventListener('klado:office-data',function(e){officeData=e.detail;renderOffice();});
  document.addEventListener('klado:lang',function(){render();renderOffice();});
  document.addEventListener('visibilitychange',function(){if(!document.hidden&&inOffice)loadMessages();});
  setInterval(function(){if(!document.hidden&&document.getElementById('page-home').getBoundingClientRect().width){if(inOffice)loadMessages();else load();}},30000);
  window.KladoTown={editAgent:editAgent,enter:enter};load();
})();
