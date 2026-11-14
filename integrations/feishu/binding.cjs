'use strict';
const { createHash, createHmac, randomBytes } = require('node:crypto');
const PATH = '/api/internal/feishu-binding';

function bridgeSettings(env = process.env) {
  if (env.TAXPEARLS_FEISHU_BINDING_ENABLED !== '1') return null;
  const value = (env.TAXPEARLS_FEISHU_BRIDGE_URL || '').replace(/\/$/, '');
  const key = env.TAXPEARLS_FEISHU_BRIDGE_KEY || '';
  const url = new URL(value);
  const local = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((!local && url.protocol !== 'https:') || url.username || url.password || url.pathname !== '/'
    || url.search || url.hash || !/^[a-fA-F0-9]{64}$/.test(key)) throw new Error('binding_config_invalid');
  return { base: url.origin, key: Buffer.from(key, 'hex') };
}

function createBindingBridge(config, client, { fetcher = fetch, log = console.log, now = Date.now } = {}) {
  if (!config.binding) return null;
  const pending = new Set();
  let timer, polling = false, stopped = false;
  async function request(operation, values = {}) {
    const raw = JSON.stringify({ app_id: config.appId, operation, ...values });
    const stamp = String(Math.floor(now() / 1000)), nonce = randomBytes(16).toString('hex');
    const input = ['POST', PATH, stamp, nonce, createHash('sha256').update(raw).digest('hex')].join('\n');
    const signature = createHmac('sha256', config.binding.key).update(input).digest('hex');
    const response = await fetcher(config.binding.base + PATH, {
      method:'POST', headers:{'Content-Type':'application/json','x-tp-time':stamp,'x-tp-nonce':nonce,'x-tp-signature':signature},
      body:raw, signal:AbortSignal.timeout(2500), redirect:'error',
    });
    if (!response.ok) throw new Error('binding_request_failed');
    return response.json();
  }
  const button = (label, value, type = 'default') => ({tag:'button', text:{tag:'plain_text',content:label},type,value});
  const card = (title, text, actions = []) => ({
    config:{wide_screen_mode:true}, header:{template:'blue',title:{tag:'plain_text',content:title}},
    elements:[{tag:'div',text:{tag:'plain_text',content:text}}, ...(actions.length ? [{tag:'action',actions}] : [])],
  });
  async function send(chat, content, key) {
    const result = await client.im.message.create({params:{receive_id_type:'chat_id'},data:{
      receive_id:chat,msg_type:'interactive',content:JSON.stringify(content),
      uuid:createHash('sha256').update(config.appId + ':binding:' + key).digest('hex').slice(0,32),
    }});
    if (result?.code !== 0 || !result?.data?.message_id) throw new Error('binding_send_failed');
  }
  function enqueue(work) {
    if (stopped || pending.size >= 100) return false;
    const task = Promise.resolve().then(work).catch(() => {
      log('[feishu] binding_failed：绑定服务暂不可用，请稍后重试；未宣称绑定成功。');
    }).finally(() => pending.delete(task));
    pending.add(task); return true;
  }
  async function menu(subject, chat, eventId) {
    const result = await request('issue', {subject,chat,event_id:eventId});
    if (result.state === 'bound') return card('TaxPearls · 已绑定账户', '你的飞书账户已连接 TaxPearls。任务功能尚在接入中，可前往网站管理绑定。');
    const url = new URL(result.url);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1','localhost','[::1]'].includes(url.hostname))) throw new Error('binding_url_invalid');
    return card('TaxPearls · 绑定账户', '先连接你的 TaxPearls 企业账号。点击下方按钮登录并确认，再回到此私聊完成最终确认。', [
      {tag:'button',text:{tag:'plain_text',content:'绑定 TaxPearls 账户'},type:'primary',url:url.href},
    ]);
  }
  async function poll() {
    if (polling || stopped) return;
    polling = true;
    try {
      const {items} = await request('pending');
      for (const item of items) {
        if (stopped || !/^ou_[A-Za-z0-9]+$/.test(item.subject) || !/^oc_[A-Za-z0-9]+$/.test(item.chat)
          || config.allowedOpenIds.size && !config.allowedOpenIds.has(item.subject)) continue;
        const values = {id:item.id,confirmation:item.confirmation};
        await send(item.chat, card('确认绑定 TaxPearls', '网站已确认账号：' + item.account_hint + '\n只有你本人刚刚操作了绑定，才点击确认。若非本人操作，请拒绝。', [
          button('确认绑定',{binding_action:'confirm',...values},'primary'),
          button('拒绝绑定',{binding_action:'cancel',...values}),
        ]), 'prompt:' + item.id);
        log('[feishu] binding_confirmation_sent：已发送原飞书账号最终确认卡片。');
      }
    } catch { log('[feishu] binding_poll_failed：未能读取绑定确认队列。'); }
    finally { polling = false; }
  }
  function onAction(data) {
    const value = data?.action?.value;
    if (!['confirm','cancel'].includes(value?.binding_action) || !/^[0-9a-f]{32}$/.test(value?.id || '')
      || !/^[0-9a-f]{64}$/.test(value?.confirmation || '')) return {toast:{type:'warning',content:'绑定请求无效，请从网站重新发起。'}};
    const subject = data.operator.open_id, chat = data.context?.open_chat_id;
    if (!/^oc_[A-Za-z0-9]+$/.test(chat || '')) return {toast:{type:'warning',content:'请在原私聊中确认。'}};
    const accepted = enqueue(async () => {
      try {
        const result = await request(value.binding_action, {subject,chat,id:value.id,confirmation:value.confirmation});
        const completed = result.state === 'completed';
        if (!completed && result.state !== 'cancelled') throw new Error('invalid_state');
        await send(chat, card(completed?'TaxPearls · 绑定成功':'TaxPearls · 已取消绑定',
          completed?'两边确认已完成。网页会同步更新绑定状态。当前尚未接入真实任务操作。':'本次请求已取消，没有建立新的账户绑定。'), value.id + ':' + result.state);
        log('[feishu] binding_' + result.state + '：服务端已确认绑定状态。');
      } catch {
        await send(chat, card('绑定未完成', '请求可能已过期、被取消或不属于当前私聊。请回网站核对状态，不要继续点击旧卡片。'), value.id + ':failed');
      }
    });
    return {toast:{type:accepted?'info':'warning',content:accepted?'正在核对绑定，请等待结果卡片。':'请求较多，请稍后重试。'}};
  }
  return {menu,onAction,poll,request,drain:()=>Promise.all([...pending]),
    start(){if(!timer){stopped=false;timer=setInterval(poll,2500);poll();}},
    stop(){stopped=true;clearInterval(timer);timer=null;},
  };
}
module.exports = {bridgeSettings,createBindingBridge};
