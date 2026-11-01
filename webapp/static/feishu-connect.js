'use strict';
(() => {
  const el = id => document.getElementById(id);
  const initial = new URLSearchParams(location.hash.slice(1)).get('ticket');
  let ticket = initial || '';
  try {
    if (initial) sessionStorage.setItem('tp.feishu.ticket', initial);
    else ticket = sessionStorage.getItem('tp.feishu.ticket') || '';
  } catch (_) {}
  if (location.hash) history.replaceState(null, '', location.pathname);
  let timer, reading = false, generation = 0, entryReady = false, terminal = false, webProof = '';
  const show = (id, value) => { el(id).hidden = !value; };
  const say = value => { el('status').textContent = value; };
  const clearTicket = () => { ticket = ''; try { sessionStorage.removeItem('tp.feishu.ticket'); } catch (_) {} };
  async function api(path, body, method = 'POST') {
    const response = await fetch(path, { method, credentials:'same-origin', cache:'no-store',
      headers: method === 'GET' ? {} : {'Content-Type':'application/json'},
      ...(body === undefined ? {} : {body:JSON.stringify(body)}) });
    const data = await response.json();
    if (!response.ok) { const error = new Error(typeof data.detail === 'string' ? data.detail : '操作失败，请重试。'); error.status = response.status; throw error; }
    return data;
  }
  function account(value) {
    el('account-name').textContent = value.username;
    el('account-display').textContent = value.display_name;
    el('account-org').textContent = value.organization;
    show('account', true); show('login', false);
  }
  async function loadBindings() {
    const rows = (await api('/api/channels/feishu/bindings', undefined, 'GET')).filter(row => row.channel === 'feishu');
    el('binding-list').replaceChildren();
    for (const row of rows) {
      const p = document.createElement('p'); p.textContent = '飞书 · 已绑定';
      const button = document.createElement('button'); button.className = 'secondary'; button.textContent = '解除绑定';
      button.addEventListener('click', async () => {
        if (!window.confirm('确认解除当前飞书绑定？解除后，旧绑定将不能继续使用。')) return;
        button.disabled = true;
        try { await api('/api/channels/feishu/bindings/' + encodeURIComponent(row.id), undefined, 'DELETE'); say('已解除飞书绑定。'); await loadBindings(); }
        catch (error) { say(error.message); button.disabled = false; }
      });
      el('binding-list').append(p, button);
    }
    show('bindings', rows.length > 0);
    return rows.length;
  }
  async function refresh() {
    if (reading || terminal) return;
    reading = true; const current = generation;
    clearTimeout(timer);
    try {
      if (!entryReady) {
        const entry = await api('/api/channels/feishu/entry', undefined, 'GET');
        el('bot-link').href = entry.url; el('qr').src = '/api/channels/feishu/qr.svg'; entryReady = true;
      }
      if (current !== generation) return;
      show('entry', !ticket);
      if (!ticket) {
        const user = await api('/api/me', undefined, 'GET');
        if (current !== generation) return;
        account({...user,organization:user.org_name || user.org_id});
        await loadBindings(); say('可管理已有绑定，或用飞书扫码打开助手。'); show('login', false);
        return;
      }
      const result = await api('/api/channels/feishu/status', {ticket});
      if (current !== generation) return;
      webProof = result.web_proof;
      account(result.account);
      el('expiry').textContent = '本次链接剩余约 ' + Math.ceil(result.expires_in / 60) + ' 分钟';
      show('claim', result.state === 'pending'); show('waiting', result.state === 'web_confirmed');
      el('switch').disabled = result.state === 'web_confirmed';
      if (result.state === 'completed') {
        say('绑定成功。你可以回到飞书使用助手。'); el('intro').textContent = '你的飞书账户已与下方 TaxPearls 账号连接。'; clearTicket(); el('expiry').textContent = ''; await loadBindings();
      } else if (result.state === 'cancelled') {
        say('本次绑定已取消。请回到飞书重新发起。'); clearTicket(); terminal = true; el('expiry').textContent = '';
      } else {
        say(result.state === 'pending' ? '请核对当前账号，再确认绑定。' : '网站已确认，等待原飞书账号最终确认。');
        if (result.state === 'web_confirmed') timer = setTimeout(refresh, 2500);
      }
    } catch (error) {
      if (current !== generation) return;
      say(error.message);
      if (error.status === 401) { show('account', false); show('login', true); show('claim', false); show('waiting', false); }
      if (error.status === 404 || error.status === 409) { show('claim', false); show('waiting', false); terminal = true; }
    } finally { reading = false; }
  }
  async function act(button, action) {
    button.disabled = true;
    try { await action(); }
    catch (error) { say(error.message); }
    finally { button.disabled = false; }
  }
  el('login-form').addEventListener('submit', event => {
    event.preventDefault();
    act(event.submitter, async () => {
      try { await api('/api/login', {username:el('username').value.trim(), password:el('password').value}); }
      finally { el('password').value = ''; }
      terminal = false; await refresh();
    });
  });
  el('switch').addEventListener('click', () => act(el('switch'), async () => {
    generation++; webProof = ''; clearTimeout(timer);
    await api('/api/logout', {});
    show('account', false); show('claim', false); show('bindings', false); show('login', true);
    say('请登录要绑定的 TaxPearls 账号。');
  }));
  el('confirm').addEventListener('click', () => act(el('confirm'), async () => {
    try { await api('/api/channels/feishu/claim', {ticket,web_proof:webProof}); }
    catch (error) { show('claim', false); webProof = ''; throw error; }
    await refresh();
  }));
  el('cancel').addEventListener('click', () => act(el('cancel'), async () => {
    await api('/api/channels/feishu/cancel', {ticket}); await refresh();
  }));
  el('refresh').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => { generation++; clearTimeout(timer); });
  refresh();
})();
