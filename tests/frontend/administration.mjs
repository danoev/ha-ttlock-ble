// Synthetic browser fixtures only: no HA instance, physical lock or real credential.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require('playwright');
const source=await readFile(new URL('../../custom_components/ttlock_ble/frontend/administration.js',import.meta.url),'utf8');
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
try {
  const page=await browser.newPage();
  const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  const requests=[];
  let failure=null;
  let heldRoute, holdRead=false;
  const snapshot={fresh:true,legacy_pin_writes_disabled:false,records:[{reference:'a'.repeat(32),name:'Stored label',record_type:'pin_type_1',validity:'permanent',present:true,stale:false,observation:'observed',limited_pin_coverage:true,revocation_verified:false,alias_confirmed_this_session:true}]};
  await page.route('http://fixture.invalid/**',async route=>{
    const url=route.request().url();
    if (url.endsWith('/panel.js')) return route.fulfill({contentType:'application/javascript',body:source});
    if (!url.endsWith('/api/ttlock_ble/administration')) return route.fulfill({contentType:'text/html',body:'<!doctype html><body></body>'});
    const request=route.request().postDataJSON(); requests.push(request);
    if (request.action==='list') return route.fulfill({json:{locks:[{entry_id:'a',lock:'lock-a',name:'Front'},{entry_id:'b',lock:'lock-b',name:'Other'}]}});
    if (holdRead && request.action==='read') { heldRoute=route; return; }
    if (failure==='network') return route.abort('connectionfailed');
    if (failure==='json') return route.fulfill({contentType:'application/json',body:'invalid-json'});
    if (typeof failure==='number') return route.fulfill({status:failure,json:{error:'synthetic-private-error-never-display'}});
    if (request.action==='name') snapshot.records[0].name=request.name;
    return route.fulfill({json:failure==='partial' ? {...snapshot,fresh:false,records:snapshot.records.map(r=>({...r,stale:true,present:null,observation:'previously_observed_unverified'})),inventory:{count:0,enumeration_complete:false,transport_interrupted:true}} : snapshot});
  });
  await page.goto('http://fixture.invalid/');
  await page.evaluate(async()=>{
    await import('/panel.js');
    const panel=document.createElement('ttlock-access-panel');
    panel.hass={fetchWithAuth:(path,init)=>fetch(path,init)};
    document.body.append(panel);
  });
  const status=page.getByRole('status');
  await status.filter({hasText:'Fresh observed records'}).waitFor();
  const label=page.getByRole('textbox',{name:'Label for record 1',exact:true});
  assert.equal(await label.inputValue(),'Stored label');
  for (const kind of [503,401,400,'network','json','partial']) {
    failure=kind;
    await page.getByRole('button',{name:'Read fresh inventory',exact:true}).click();
    await status.filter({hasText:'Inventory unverified'}).waitFor();
    assert.equal(await label.inputValue(),'Stored label');
    assert(await label.isDisabled());
    assert(await page.getByRole('button',{name:'Save name',exact:true}).isDisabled());
    assert(!(await page.locator('body').innerText()).includes('synthetic-private-error-never-display'));
  }
  // A different lock must never show the first lock's cached private labels.
  failure='network';
  await page.getByRole('combobox',{name:'Lock',exact:true}).selectOption('1');
  await status.filter({hasText:'Inventory unverified'}).waitFor();
  assert.equal(await label.count(),0);
  await page.getByRole('combobox',{name:'Lock',exact:true}).selectOption('0');
  await label.waitFor();
  assert.equal(await label.inputValue(),'Stored label');
  failure=null;
  await page.getByRole('button',{name:'Read fresh inventory',exact:true}).click();
  await status.filter({hasText:'Fresh observed records'}).waitFor();
  assert(!(await label.isDisabled()));
  assert.equal(await label.inputValue(),'Stored label');
  snapshot.records[0]={...snapshot.records[0],stale:true,present:false,observation:'not_observed'};
  await page.getByRole('button',{name:'Read fresh inventory',exact:true}).click();
  await page.getByText('not observed in this limited PIN read',{exact:false}).waitFor();
  assert(await label.isDisabled());
  // Disconnect cancels old requests; a reattached panel must load its own state.
  holdRead=true;
  await page.getByRole('button',{name:'Read fresh inventory',exact:true}).click();
  await page.getByRole('status').filter({hasText:'Reading; explicit lock control'}).waitFor();
  snapshot.records[0]={...snapshot.records[0],name:'Reattached label',stale:false,present:true,observation:'observed'};
  await page.evaluate(()=>{
    const panel=document.querySelector('ttlock-access-panel'); panel.remove(); document.body.append(panel);
  });
  await status.filter({hasText:'Fresh observed records'}).waitFor();
  assert.equal(await label.inputValue(),'Reattached label');
  if (heldRoute) await heldRoute.fulfill({json:{...snapshot,fresh:false,records:[]}}).catch(()=>{});
  assert.equal(await label.inputValue(),'Reattached label');
  for (const name of ['Create credential','Modify credential / schedule','Delete credential']) assert(await page.getByRole('button',{name,exact:true}).isDisabled());
  assert(!requests.some(r=>['reset','create','delete','modify','name'].includes(r.action)));
  assert.deepEqual(errors,[]);
  console.log('PASS: Chromium retained scoped labels across HTTP/auth/network/JSON/partial-read failures, disabled stale naming, recovered without reset, and distinguished limited PIN absence. No writes.');
} finally { await browser.close(); }
