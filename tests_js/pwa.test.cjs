const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function worker() {
  const listeners = {}, writes = [], deleted = [], requests = [];
  const notifications = [], windows = [];
  const offline = {offline:true};
  const assets = ['/static/app.hash.css','/static/pwa.hash.js'];
  let networkFails = false;
  const context = {
    URL, Promise,
    self: {location:{origin:'https://delivery.test'}, registration:{showNotification:async(title,options)=>notifications.push({title,options})}, clients:{claim:async()=>{},matchAll:async()=>[],openWindow:async(url)=>windows.push(url)}, addEventListener:(name, fn)=>listeners[name]=fn},
    caches: {open:async()=>({put:async(url,response)=>writes.push({url,response})}), match:async(url)=>url==='/offline/'?offline:{asset:url},
      keys:async()=>['other-app-cache','design-pot-static-old','design-pot-static-v25-1','design-pot-static-v25-2'],delete:async(key)=>deleted.push(key)},
    fetch:async(request, options)=>{requests.push({request,options});if(networkFails)throw new Error('offline');return {ok:true,redirected:false,live:true};}
  };
  const source=fs.readFileSync('static/service-worker.js','utf8').replace('__ASSETS__',JSON.stringify(assets)).replace('__ICON__',JSON.stringify('/static/icon.hash.png'));
  vm.runInNewContext(source,context);
  function fetchEvent(path, method='GET', mode='cors') {
    let response;
    listeners.fetch({request:{url:`https://delivery.test${path}`,method,mode},respondWith:promise=>response=promise});
    return response;
  }
  return {listeners,writes,deleted,requests,fetchEvent,offline,notifications,windows,fail:()=>networkFails=true};
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
test('push is displayed immediately with safe text and notification click opens same-origin app route',async()=>{
  const w=worker();let pending;w.listeners.push({data:{json:()=>({title:'Private title',body:'Sensitive COD 800',url:'/client/notifications/#notification-1',tag:'notification-1'})},waitUntil:p=>pending=p});await pending;
  assert.equal(w.notifications.length,1);assert.equal(w.notifications[0].title,'Design-Pot Logistics');assert.equal(w.notifications[0].options.body,'You have a new update. Open the app to view it.');
  let click;w.listeners.notificationclick({notification:{data:{url:'https://evil.example/'},close:()=>{}},waitUntil:p=>click=p});await click;
  assert.deepEqual(w.windows,['https://delivery.test/']);
});
