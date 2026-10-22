/* Local admin-only metadata panel. Never accept a PIN, UID or biometric ID. */
class TTLockAccessPanel extends HTMLElement {
  constructor() { super(); this._root = this.attachShadow({mode: "open"}); this._snapshots = new Map(); this._generation=0; }
  set hass(value) {
    this._hass = value;
    if (!this._started && this.isConnected) this.start();
  }
  connectedCallback() { if (this._hass && !this._started) this.start(); }
  disconnectedCallback() {
    this._generation++; this._busy=false;
    this._abort?.abort(); this._records = null; this._started = false;
    this._snapshots.clear();
    this._root.replaceChildren();
  }
  el(tag, text, parent = this._root) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    parent.append(element); return element;
  }
  async start() {
    const generation=++this._generation;
    this._started = true; this._root.replaceChildren();
    const style = this.el('style');
    style.textContent = ':host{display:block;padding:24px;max-width:950px;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family, system-ui, sans-serif)}button,select,input{font:inherit;padding:10px;margin:6px;max-width:100%;box-sizing:border-box}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid var(--divider-color)}.warning{border-left:4px solid #b66d00;padding:12px}button:disabled{opacity:.55}';
    this.el('h1', 'TTLock access');
    this.el('p', 'Administrator labels for lock records. Labels do not identify a person or authorise entry. No new credential writes are enabled.');
    this.el('p', 'Never enter a PIN, card UID or fingerprint identifier as a name. Names are private HA metadata and remain visible to host administrators and backups.', this._root).className = 'warning';
    this._status = this.el('p', 'Loading…'); this._status.setAttribute('role','status');
    this._lock = this.el('select'); this._lock.setAttribute('aria-label','Lock');
    this._method = this.el('select'); this._method.setAttribute('aria-label','Access method');
    for (const [value, label] of [['keypad_pin','Keypad PIN'],['fingerprint','Fingerprint'],['ic_card','IC card (4 byte)']]) {
      const option = this.el('option',label,this._method); option.value = value;
    }
    this._lock.onchange = () => this.state(); this._method.onchange = () => this.state();
    this._read = this.button('Read fresh inventory', () => this.action('read'));
    this.el('p', 'Read-only inventories yield to Lock/Unlock. Cursor completion does not discover all unused offline/batch PINs. S31 counts were corroborated: PIN 1, fingerprint 2, card 2; other firmware requires validation. Schedules remain unverified.');
    this._summary = this.el('p'); this._table = this.el('div');
    this._legacy = this.button('Disable legacy PIN write services for this lock', () => this.action('legacy', {disabled: !this._legacyDisabled}));
    this.el('p', 'Legacy add/delete/clear PIN services are deprecated for confidential use. HA can record their inputs before the handler. Disabling stops BLE writes but cannot erase values already sent to HA. Existing automations remain enabled until an administrator explicitly disables them.', this._root).className='warning';
    this.button('Forget local labels and references', () => {
      if (window.confirm('Forget local labels for this lock and method? This does not delete credentials on the lock.')) this.action('reset',{confirm:'forget_local_labels'});
    });
    for (const label of ['Create credential','Modify credential / schedule','Delete credential']) this.button(label,()=>{}).disabled=true;
    this.el('p','Credential writes: disabled pending disposable-credential hardware validation. Alarmo automatic disarming remains disabled. Use the independent door contact for intrusion detection.');
    try {
      const data = await this.request({action:'list'});
      if (generation!==this._generation || !this.isConnected) return;
      this._locks=data.locks;
      for (const [index, lock] of data.locks.entries()) { const option=this.el('option',lock.name,this._lock); option.value=String(index); }
      await this.state();
    } catch { if (generation===this._generation && this.isConnected) this._status.textContent='Administration unavailable. Use a normal administrator frontend session on the same HA origin.'; }
  }
  button(text, handler, parent=this._root) { const button=this.el('button',text,parent); button.type='button'; button.onclick=handler; return button; }
  async request(body) {
    this._abort?.abort(); const controller = new AbortController(); this._abort=controller;
    const timer = setTimeout(()=>controller.abort(),45000);
    try {
      const response = await this._hass.fetchWithAuth('/api/ttlock_ble/administration', {
        method:'POST', headers:{'Content-Type':'application/json','X-TTLock-Admin':'1'},
        body:JSON.stringify(body), cache:'no-store', signal:controller.signal,
      });
      if (!response.ok) {
        const error = new Error('Administration request failed');
        error.category = [401,403].includes(response.status) ? 'administrator_session_required' : response.status===400 ? 'invalid_request_or_stale_reference' : 'administration_unavailable';
        throw error;
      }
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  async state() { return this.action('state'); }
  async action(action, extra={}) {
    if (this._busy) return;
    const lock=this._locks?.[Number(this._lock.value)];
    if (!lock) { this._status.textContent='No loaded TTLock locks.'; return; }
    this._busy=true;
    const scope=JSON.stringify([lock.entry_id,lock.lock,this._method.value]);
    const generation=this._generation;
    // Freeze selectors so an in-flight result cannot be labelled as a different lock.
    for (const el of this._root.querySelectorAll('button,select,input')) el.disabled=true;
    this._status.textContent=action==='read'?'Reading; explicit lock control has priority…':'Updating local metadata…';
    try {
      const data=await this.request({action,entry_id:lock.entry_id,lock:lock.lock,method:this._method.value,...extra});
      if (generation!==this._generation || !this.isConnected) return;
      this._snapshots.set(scope,data);
      this.render(data);
      this._status.textContent=data.fresh?'Fresh observed records. Names are labels, not identity.':'Stale or not yet read. Read a fresh inventory to assign names.';
      if (action==='read' && !data.fresh) this._status.textContent=`Inventory unverified (${data.inventory?.transport_interrupted?'transport_interrupted':'inventory_not_verified'}). Last-known records and labels are retained.`;
    } catch (error) {
      if (generation!==this._generation || !this.isConnected) return;
      const category=error.name==='AbortError' ? 'cancelled_or_timed_out' : ['administrator_session_required','invalid_request_or_stale_reference','administration_unavailable'].includes(error.category) ? error.category : 'connection_or_response_failed';
      const cached=this._snapshots.get(scope);
      // Preserve only this lock/method's last known metadata. A failed response
      // cannot establish physical absence, freshness, or permission to name.
      const unverified={...(cached || {}),fresh:false,records:(cached?.records || []).map(record=>({...record,present:null,stale:true,observation:'previously_observed_unverified'}))};
      if (cached) this._snapshots.set(scope,unverified);
      this.render(unverified);
      this._summary.textContent='Last-known local labels only. Inventory could not be verified; no physical deletion is established.';
      this._status.textContent=`Inventory unverified (${category}). Last-known records and labels are retained. Read again; no credential write was attempted.`;
    } finally {
      if (generation===this._generation && this.isConnected) {
      this._busy=false;
      this._lock.disabled=false; this._method.disabled=false;
      // Rebuild controls from safe state; writes always stay disabled.
      for (const button of this._root.querySelectorAll('button')) if (!['Create credential','Modify credential / schedule','Delete credential','Save name'].includes(button.textContent)) button.disabled=false;
      this._legacy.disabled=!this._legacyKnown;
      }
    }
  }
  render(data) {
    this._records=data.records; this._legacyDisabled=data.legacy_pin_writes_disabled;
    this._legacyKnown=typeof this._legacyDisabled==='boolean';
    this._legacy.textContent=this._legacyDisabled?'Re-enable legacy PIN write services for this lock':'Disable legacy PIN write services for this lock';
    this._summary.textContent=data.inventory?`Observed ${data.inventory.count}; cursor enumeration ${data.inventory.enumeration_complete?'complete':'incomplete'}. PIN coverage can still be partial. Transport interrupted: ${Boolean(data.inventory.transport_interrupted)}.`:'Local labels only; freshness expires after five minutes.';
    this._table.replaceChildren(); const table=this.el('table',undefined,this._table); const head=this.el('tr',undefined,table);
    for (const name of ['Record','Validity / evidence','Administrator label']) this.el('th',name,head);
    for (const [index, record] of data.records.entries()) {
      const row=this.el('tr',undefined,table);
      this.el('td',`${index+1}. ${({card_4byte:"IC card",fingerprint_record:"Fingerprint",pin_type_1:"Permanent PIN",pin_type_3:"Timed PIN",pin_type_4:"Cyclic PIN"})[record.record_type] || "Unknown record"} (${record.stale?'stale': 'observed'})`,row);
      const evidence=record.observation==='observed' ? 'currently observed and fresh' : record.observation==='not_observed' ? (record.limited_pin_coverage ? 'not observed in this limited PIN read; status unknown' : 'no longer observed in the completed inventory; deletion unconfirmed') : 'previously observed; currently unverified';
      this.el('td',`${record.validity}; schedule unverified; ${evidence}. Physical deletion is not verified.`,row);
      const cell=this.el('td',undefined,row), input=this.el('input',undefined,cell);
      input.type='text'; input.maxLength=64; input.value=record.name; input.autocomplete='off'; input.setAttribute('aria-label',`Label for record ${index+1}`);
      input.disabled=record.stale;
      const save=this.button('Save name',()=>this.action('name',{reference:record.reference,name:input.value}),cell); save.disabled=record.stale;
      if (record.name && !record.alias_confirmed_this_session) this.el('p','Stored label unconfirmed after restart; review and save to confirm.',cell);
    }
  }
}
customElements.define('ttlock-access-panel', TTLockAccessPanel);
