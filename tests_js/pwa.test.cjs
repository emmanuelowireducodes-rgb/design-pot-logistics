const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function worker({badge = false, unread = 3} = {}) {
  const listeners = {}, writes = [], deleted = [], requests = [], badges = [];
  const notifications = [], windows = [], focused = [];
  const offline = {offline:true};
  const assets = ['/static/app.hash.css','/static/pwa.hash.js'];
  let networkFails = false;
  const context = {
    URL, Promise,
    self: {location:{origin:'https://delivery.test'}, navigator:badge?{setAppBadge:async value=>badges.push(value),clearAppBadge:async()=>badges.push(0)}:{}, registration:{showNotification:async(title,options)=>notifications.push({title,options})}, clients:{claim:async()=>{},matchAll:async()=>[],openWindow:async(url)=>windows.push(url)}, addEventListener:(name, fn)=>listeners[name]=fn},
    caches: {open:async()=>({put:async(url,response)=>writes.push({url,response})}), match:async(url)=>url==='/offline/'?offline:{asset:url},
      keys:async()=>['other-app-cache','design-pot-static-old','design-pot-static-v25-1','design-pot-static-v25-2'],delete:async(key)=>deleted.push(key)},
    fetch:async(request, options)=>{requests.push({request,options});if(networkFails)throw new Error('offline');return {ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>({unread}),live:true};}
  };
  const source=fs.readFileSync('static/service-worker.js','utf8').replace('__ASSETS__',JSON.stringify(assets)).replace('__ICON__',JSON.stringify('/static/icon.hash.png'));
  vm.runInNewContext(source,context);
  function fetchEvent(path, method='GET', mode='cors') {
    let response;
    listeners.fetch({request:{url:`https://delivery.test${path}`,method,mode},respondWith:promise=>response=promise});
    return response;
  }
  return {listeners,writes,deleted,requests,fetchEvent,offline,notifications,windows,badges,fail:()=>networkFails=true};
}

test('installation caches only explicit public assets and offline document without cookies',async()=>{
  const w=worker();let promise;w.listeners.install({waitUntil:p=>promise=p});await promise;
  assert.deepEqual(w.writes.map(x=>x.url),['/static/app.hash.css','/static/pwa.hash.js','/offline/']);
  assert.ok(w.requests.every(x=>x.options.credentials==='omit'));
});
test('authenticated navigation always uses network and is never written to cache',async()=>{
  const w=worker();const result=await w.fetchEvent('/admin/finance/','GET','navigate');
  assert.equal(result.live,true);assert.equal(w.writes.length,0);assert.equal(w.requests.length,1);
});
test('offline navigation returns connection page rather than stale operational data',async()=>{
  const w=worker();w.fail();assert.equal(await w.fetchEvent('/deliveries/private/','GET','navigate'),w.offline);
});
test('write actions, unknown assets and cross-origin resources are not intercepted',()=>{
  const w=worker();
  for(const path of ['/deliveries/1/collect/','/locations/resolve/'])assert.equal(w.fetchEvent(path,'POST'),undefined);
  for(const path of ['/locations/private/','/admin/finance/','/admin/finance/settlements/','/client/finance/','/admin/reports/revenue/','/static/unknown.js','/static/app.hash.css?private=1'])assert.equal(w.fetchEvent(path),undefined);
  let responded=false;w.listeners.fetch({request:{url:'https://external.example/private',method:'GET'},respondWith:()=>responded=true});
  assert.equal(responded,false);
});
test('activation removes only old Design-Pot caches',async()=>{
  const w=worker();let promise;w.listeners.activate({waitUntil:p=>promise=p});await promise;
  assert.deepEqual(w.deleted,['design-pot-static-old','design-pot-static-v25-1','design-pot-static-v25-2']);
});
test('push uses generic lock-screen text, safe notification bridge URL, and never suppresses normal alerts',async()=>{
  const w=worker();let pending;w.listeners.push({data:{json:()=>({title:'Private title',body:'Sensitive COD 800',url:'/notifications/123e4567-e89b-12d3-a456-426614174000/open/',tag:'notification-1'})},waitUntil:p=>pending=p});await pending;
  assert.equal(w.notifications.length,1);assert.equal(w.notifications[0].title,'Design-Pot Logistics');assert.equal(w.notifications[0].options.body,'You have a new update. Open the app to view it.');
  assert.equal(w.notifications[0].options.silent,undefined);
  assert.equal(w.notifications[0].options.badge,undefined);
  assert.equal(w.notifications[0].options.data.url,'/notifications/123e4567-e89b-12d3-a456-426614174000/open/');
  let click;w.listeners.notificationclick({notification:{data:{url:w.notifications[0].options.data.url},close:()=>{}},waitUntil:p=>click=p});await click;
  assert.deepEqual(w.windows,['https://delivery.test/notifications/123e4567-e89b-12d3-a456-426614174000/open/']);
});
test('push rejects external or unrelated navigation URLs',async()=>{
  const w=worker();let pending;w.listeners.push({data:{json:()=>({url:'https://evil.example/'})},waitUntil:p=>pending=p});await pending;
  assert.equal(w.notifications[0].options.data.url,'/');
  let click;w.listeners.notificationclick({notification:{data:{url:'https://evil.example/'},close:()=>{}},waitUntil:p=>click=p});await click;
  assert.deepEqual(w.windows,['https://delivery.test/']);
});
test('push synchronizes app badge from the authenticated server count when supported',async()=>{
  const w=worker({badge:true,unread:7});let pending;w.listeners.push({data:{json:()=>({url:'/'})},waitUntil:p=>pending=p});await pending;
  assert.deepEqual(w.badges,[7]);
  assert.equal(w.requests.length,1);
  assert.equal(w.requests[0].request,'/notifications/state/');
  assert.equal(w.requests[0].options.credentials,'include');
  assert.equal(w.requests[0].options.cache,'no-store');
});
test('zero server unread count clears a supported app badge',async()=>{
  const w=worker({badge:true,unread:0});let pending;w.listeners.push({data:{json:()=>({url:'/'})},waitUntil:p=>pending=p});await pending;
  assert.deepEqual(w.badges,[0]);
});
