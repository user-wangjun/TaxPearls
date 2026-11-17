'use strict';
(() => {
  const el = id => document.getElementById(id);
  const callback = location.pathname === '/connect/feishu/callback' ? new URLSearchParams(location.search) : null;
  const oauthResult = callback ? {state:callback.get('state') || '', code:callback.get('code') || '', error:callback.get('error') || ''} : null;
  if (callback) history.replaceState(null, '', '/connect/feishu');
  const initial = new URLSearchParams(location.hash.slice(1)).get('ticket');
  let ticket = initial || '', callbackHandled = false, oauthReady = false, accountProof = '', callbackMessage = '';
  try {
    if (callback) sessionStorage.removeItem('tp.feishu.ticket');
    else if (initial) sessionStorage.setItem('tp.feishu.ticket', initial);
    else ticket = sessionStorage.getItem('tp.feishu.ticket') || '';
  } catch (_) {}
  if (location.hash) history.replaceState(null, '', location.pathname);
  let timer, reading = false, generation = 0, entryReady = false, bindingReady = false, terminal = false, webProof = '';
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
        try { await api('/api/channels/feishu/bindings/' + encodeURIComponent(row.id), undefined, 'DELETE'); show('success', false); say('已解除飞书绑定。'); await refresh(); }
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
        if (current !== generation) return;
        bindingReady = entry.binding_ready;
        oauthReady = entry.oauth_ready === true;
        el('bot-link').href = entry.url; el('qr').src = '/api/channels/feishu/qr.svg'; entryReady = true;
        el('success-bot').href = entry.url;
        el('intro').textContent = ticket && bindingReady ? '核对当前账号，在网页与原飞书私聊中完成确认。' : oauthReady ? '核对下方 TaxPearls 账号，再前往飞书授权绑定。' : bindingReady ? '用飞书扫一扫打开助手，再点击助手发来的“绑定账户”。' : '用飞书扫一扫或点击链接打开助手。';
        say(entry.message);
      }
      if (current !== generation) return;
      show('entry', (!ticket && !oauthReady) || !bindingReady);
      if (!bindingReady) {
        show('login', false); show('account', false); show('bindings', false); show('claim', false); show('waiting', false); show('oauth', false);
        say('账号绑定暂不可用，可先扫码或点击链接打开飞书助手。');
        return;
      }
      if (oauthResult && !callbackHandled) {
        callbackHandled = true;
        if (!oauthResult.state) throw new Error('飞书授权缺少校验信息，请重新发起绑定。');
        say('正在验证飞书授权并建立绑定，请稍候；网络较慢时可能需要几分钟，请勿重复授权或刷新页面…');
        const result = await api('/api/channels/feishu/oauth/complete', oauthResult);
        if (current !== generation) return;
        if (result.state !== 'completed') {
          callbackMessage = '已取消飞书授权，尚未建立绑定。可点击下方按钮重新授权。';
        } else {
          clearTicket(); show('success', true); show('oauth', false);
          el('success-bot').href = result.url;
          el('intro').textContent = '你的飞书账户已与 TaxPearls 账号连接。';
          say('飞书绑定成功，正在打开助手；如浏览器询问，请选择“打开飞书”。');
          location.assign(result.url);
          return;
        }
      }
      if (!ticket) {
        const user = await api(oauthReady ? '/api/channels/feishu/oauth/account' : '/api/me', undefined, 'GET');
        if (current !== generation) return;
        if (oauthReady) { account(user.account); accountProof = user.account_proof; }
        else account({...user,organization:user.org_name || user.org_id});
        const bound = await loadBindings();
        show('oauth', oauthReady && !bound); show('login', false);
        if (bound) { show('success', true); say('飞书账户已绑定，可直接打开助手。'); el('intro').textContent = '你的飞书账户已与下方 TaxPearls 账号连接。'; }
        else say(callbackMessage || (oauthReady ? '请核对当前账号，点击下方按钮前往飞书授权。' : '可管理已有绑定，或用飞书扫码打开助手。'));
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
      if (!entryReady) el('intro').textContent = '当前无法加载飞书助手入口，请重新读取或联系管理员。';
      show('oauth', false); accountProof = '';
      show('oauth-retry', !!callback);
      if (error.status === 401) {
        show('account', false); show('claim', false); show('waiting', false);
        show('login', !callback);
        if (callback) say('授权未完成：请在原来已登录 TaxPearls 的浏览器中重新打开绑定页面并授权。');
      }
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
    generation++; webProof = ''; accountProof = ''; clearTimeout(timer);
    await api('/api/logout', {});
    show('account', false); show('claim', false); show('bindings', false); show('login', true); show('oauth', false); show('success', false);
    say('请登录要绑定的 TaxPearls 账号。');
  }));
  el('oauth-start').addEventListener('click', () => act(el('oauth-start'), async () => {
    const result = await api('/api/channels/feishu/oauth/begin', {account_proof:accountProof});
    location.assign(result.url);
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
  el('entry-refresh').addEventListener('click', () => {
    if (reading) return;
    entryReady = false; terminal = false; show('entry', false);
    refresh();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => { generation++; clearTimeout(timer); });
  refresh();
})();
