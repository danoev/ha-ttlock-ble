// Standalone local UI regression: own headless browser, synthetic metadata only.
// NODE_PATH=<runtime node_modules> node scripts/test_admin_panel.mjs
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const browser = await chromium.launch({
  executablePath: process.env.TTLOCK_TEST_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless: true,
});
try {
  const page = await browser.newPage({viewport:{width:1100,height:1000}});
  const errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.setContent('<!doctype html><html><body></body></html>');
  await page.addScriptTag({path:fileURLToPath(new URL('../custom_components/ttlock_ble/frontend/administration.js',import.meta.url))});
  await page.evaluate(()=>{
    window.calls=[]; window.syntheticFresh=false; window.syntheticName=''; window.legacyDisabled=false;
    const panel=document.createElement('ttlock-access-panel');
    panel.hass={fetchWithAuth:async(url, options)=>{
      const body=JSON.parse(options.body);
      window.calls.push({url,body,headers:options.headers});
      if(body.action==='list') return {ok:true,json:async()=>({locks:[{entry_id:'synthetic-entry',lock:'synthetic-lock',name:'Test lock',admin_key_available:true}]})};
      if(body.action==='read') window.syntheticFresh=true;
      if(body.action==='name') window.syntheticName=body.name;
      if(body.action==='legacy') window.legacyDisabled=body.disabled;
      const records=body.action==='reset'?[]:[{reference:'a'.repeat(32),name:window.syntheticName,record_type:'card_4byte',validity:'permanent',schedule_verified:false,identity_verified:false,present:true,stale:!window.syntheticFresh,alias_confirmed_this_session:false}];
      return {ok:true,json:async()=>({fresh:window.syntheticFresh,records,legacy_pin_writes_disabled:window.legacyDisabled,new_writes:'disabled',inventory:body.action==='read'?{count:1,enumeration_complete:true,transport_interrupted:false}:undefined})};
    }};
    document.body.append(panel);
  });
  const status=page.getByRole('status');
  await status.filter({hasText:'Stale or not yet read'}).waitFor();
  assert(await page.getByRole('button',{name:'Save name',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Read fresh inventory',exact:true}).click();
  await status.filter({hasText:'Fresh observed records'}).waitFor();
  await page.getByRole('textbox',{name:'Label for record 1'}).fill('<img src=x onerror="window.hacked=true">');
  await page.getByRole('button',{name:'Save name',exact:true}).click();
  await status.filter({hasText:'Fresh observed records'}).waitFor();
  assert.equal(await page.locator('ttlock-access-panel img').count(),0);
  assert.equal(await page.evaluate(()=>window.hacked),undefined);
  for(const name of ['Create credential','Modify credential / schedule','Delete credential']) assert(await page.getByRole('button',{name,exact:true}).isDisabled());
  await page.getByRole('button',{name:'Disable legacy PIN write services for this lock',exact:true}).click();
  await page.getByRole('button',{name:'Re-enable legacy PIN write services for this lock',exact:true}).waitFor();
  await page.screenshot({path:'/private/tmp/ttlock-rc16-admin-panel.png',fullPage:true});
  const calls=await page.evaluate(()=>window.calls);
  assert(calls.some(c=>c.body.action==='read'));
  assert(calls.some(c=>c.body.action==='name'));
  assert(calls.some(c=>c.body.action==='legacy'&&c.body.disabled));
  assert(calls.every(c=>c.url==='/api/ttlock_ble/administration'&&c.headers['X-TTLock-Admin']==='1'));
  assert(!calls.some(c=>['create','modify','delete'].includes(c.body.action)));
  assert.deepEqual(errors,[]);
  console.log('Admin panel: load, stale/fresh records, naming, XSS safety, legacy opt-out and disabled writes passed.');
} finally { await browser.close(); }
