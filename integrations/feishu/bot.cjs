'use strict';

// Standalone connection probe: deliberately imports no TaxPearls business service.
const { createHash, createHmac, randomBytes, timingSafeEqual } = require('node:crypto');
const lark = require('@larksuiteoapi/node-sdk');
const { bridgeSettings, createBindingBridge } = require('./binding.cjs');

const ACTIONS = Object.freeze({ create: '创建任务', ongoing: '进行中的任务', completed: '已完成的任务' });
const MAX_AGE = 10 * 60 * 1000;

function readConfig(env = process.env) {
  const appId = (env.TAXPEARLS_FEISHU_APP_ID || '').trim();
  const appSecret = (env.TAXPEARLS_FEISHU_APP_SECRET || '').trim();
  if (!/^cli_[A-Za-z0-9]+$/.test(appId) || !appSecret) {
    throw new Error('请在本目录 .env 填写 TAXPEARLS_FEISHU_APP_ID 和 TAXPEARLS_FEISHU_APP_SECRET。');
  }
  return {
    appId, appSecret,
    tenantKey: (env.TAXPEARLS_FEISHU_TENANT_KEY || '').trim(),
    allowedOpenIds: new Set((env.TAXPEARLS_FEISHU_ALLOWED_OPEN_IDS || '').split(',').map(s => s.trim()).filter(Boolean)),
    binding: bridgeSettings(env),
  };
}

// Never forward SDK arguments: they can contain credentials, URLs or message bodies.
function safeLogger(log = console.log) {
  return {
    trace() {}, debug() {}, info() {},
    warn() { log('[feishu] sdk_warning：请检查应用配置和网络。'); },
    error() { log('[feishu] sdk_error：请检查凭据、应用权限和长连接配置。'); },
  };
}

function createProbe(config, client, { log = console.log, now = Date.now, capacity = 4096, binding = null } = {}) {
  const signingKey = randomBytes(32);
  const seen = new Map();
  const menus = new Map();
  const pending = new Set();
  const permitted = (data, openId, tenant) => data?.app_id === config.appId
    && typeof openId === 'string' && /^ou_[A-Za-z0-9]+$/.test(openId)
    && (!config.tenantKey || tenant === config.tenantKey)
    && (!config.allowedOpenIds.size || config.allowedOpenIds.has(openId));
  const sign = text => createHmac('sha256', signingKey).update(text).digest('base64url');

  function actionValue(action, openId, chatId) {
    const payload = Buffer.from(JSON.stringify({ action, openId, chatId, issuedAt: now() })).toString('base64url');
    return { payload, signature: sign(payload) };
  }

  function card(openId, chatId, clicked = '') {
    return {
      config: { wide_screen_mode: true },
      header: { template: 'blue', title: { tag: 'plain_text', content: 'TaxPearls · 联通测试' } },
      elements: [
        { tag: 'markdown', content: clicked
          ? `已收到【${ACTIONS[clicked]}】的点击回传。\n此处仅验证连接，尚未读取或创建真实任务。`
          : '欢迎使用 TaxPearls，请直接选择下面的入口。\n当前为联通测试，尚未接入真实任务。' },
        { tag: 'action', actions: Object.entries(ACTIONS).map(([action, label]) => ({
          tag: 'button', text: { tag: 'plain_text', content: label },
          type: action === 'create' ? 'primary' : 'default', value: actionValue(action, openId, chatId),
        })) },
        { tag: 'note', elements: [{ tag: 'plain_text', content: '联通测试 · 不调用 AI · 不接收材料 · 按钮有效期 10 分钟' }] },
      ],
    };
  }

  function sendMenu(key, openId, chatId, automatic = false) {
    for (const [key, entry] of seen) if (entry.until <= now() && !entry.inFlight) seen.delete(key);
    for (const [key, entry] of menus) if (entry.until <= now() && !entry.inFlight) menus.delete(key);
    const menuKey = JSON.stringify([chatId, openId]);
    if (automatic && menus.has(menuKey)) return {};
    if (seen.has(key)) return {};
    if (pending.size >= 100 || seen.size >= capacity || !menus.has(menuKey) && menus.size >= capacity) {
      log('[feishu] busy：测试队列已满，请稍后重新发消息。');
      return {};
    }
    const entry = { until: now() + MAX_AGE, inFlight: true };
    seen.set(key, entry);
    const menuEntry = { until: now() + MAX_AGE, inFlight: true };
    menus.set(menuKey, menuEntry);
    // Acknowledge the WebSocket event immediately; HTTP latency must not block it.
    const delivery = Promise.resolve().then(async () => {
      const content = binding ? await binding.menu(openId, chatId, key) : card(openId, chatId);
      const result = await client.im.message.create({
        params: { receive_id_type: 'chat_id' },
        data: {
          receive_id: chatId, msg_type: 'interactive',
          content: JSON.stringify(content),
          uuid: createHash('sha256').update(config.appId + ':' + key).digest('hex').slice(0, 32),
        },
      });
      if (result?.code !== 0 || !result?.data?.message_id) throw new Error('delivery_failed');
      log('[feishu] card_delivered：飞书已返回卡片消息编号。');
    }).catch(() => {
      seen.delete(key);
      if (menus.get(menuKey) === menuEntry) menus.delete(menuKey);
      log('[feishu] delivery_failed：卡片发送失败；请检查发送权限和网络后重新发消息。');
    }).finally(() => { entry.inFlight = false; menuEntry.inFlight = false; pending.delete(delivery); });
    pending.add(delivery);
    return {};
  }

  function onMessage(data) {
    const message = data?.message;
    const sender = data?.sender;
    const openId = sender?.sender_id?.open_id;
    if (!permitted(data, openId, data?.tenant_key || sender?.tenant_key)
      || sender?.sender_type !== 'user' || message?.chat_type !== 'p2p'
      || message.message_type !== 'text' || !/^oc_[A-Za-z0-9]+$/.test(message.chat_id || '')
      || typeof message.message_id !== 'string' || !message.message_id || message.message_id.length > 256) return {};
    log('[feishu] message_received：已收到私聊文字消息。');
    return sendMenu(message.message_id, openId, message.chat_id);
  }

  function onConversation(data, first = false) {
    const openId = first ? data?.user?.open_id : data?.operator_id?.open_id;
    const eventId = first ? data?.uuid : data?.event_id;
    if (!permitted(data, openId, data?.tenant_key) || !/^oc_[A-Za-z0-9]+$/.test(data?.chat_id || '')
      || typeof eventId !== 'string' || !eventId || eventId.length > 256) return {};
    log('[feishu] conversation_entered：收到会话事件，按卡片有效期检查是否需要发送入口。');
    return sendMenu('conversation:' + eventId, openId, data.chat_id, true);
  }

  function onCardAction(data) {
    const rejected = { toast: { type: 'warning', content: '卡片已失效或不属于当前私聊，请重新进入机器人会话获取入口。' } };
    const openId = data?.operator?.open_id;
    const chatId = data?.context?.open_chat_id;
    if (!permitted(data, openId, data?.operator?.tenant_key || data?.tenant_key)) return rejected;
    if (binding && data?.action?.value?.binding_action) return binding.onAction(data);
    try {
      const { payload, signature } = data.action.value;
      if (typeof payload !== 'string' || payload.length > 2048 || typeof signature !== 'string') return rejected;
      const expected = Buffer.from(sign(payload));
      const actual = Buffer.from(signature);
      if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) return rejected;
      const value = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
      if (!Object.hasOwn(ACTIONS, value.action) || value.openId !== openId || value.chatId !== chatId
        || !Number.isFinite(value.issuedAt) || now() < value.issuedAt || now() - value.issuedAt > MAX_AGE) return rejected;
      log('[feishu] card_action_received：' + ACTIONS[value.action]);
      const menuEntry = menus.get(JSON.stringify([chatId, openId]));
      if (menuEntry) menuEntry.until = now() + MAX_AGE;
      return {
        toast: { type: 'success', content: ACTIONS[value.action] + '：按钮回传成功' },
        card: { type: 'raw', data: card(openId, chatId, value.action) },
      };
    } catch { return rejected; }
  }

  return { onMessage, onConversation, onCardAction, drain: () => Promise.all([...pending]) };
}

function createDispatcher(config, client, options = {}) {
  const probe = createProbe(config, client, options);
  const dispatcher = new lark.EventDispatcher({ logger: safeLogger(options.log) }).register({
    'im.message.receive_v1': probe.onMessage,
    'im.chat.access_event.bot_p2p_chat_entered_v1': data => probe.onConversation(data),
    'p2p_chat_create': data => probe.onConversation(data, true),
    'card.action.trigger': probe.onCardAction,
  });
  return { ...probe, dispatcher };
}

async function main() {
  const config = readConfig();
  if (process.argv.includes('--check')) {
    console.log('本地配置格式通过；未联网校验凭据、连接、权限或送达。');
    return;
  }
  const logger = safeLogger();
  const client = new lark.Client({ appId: config.appId, appSecret: config.appSecret, logger, domain: lark.Domain.Feishu });
  const binding = createBindingBridge(config, client);
  const { dispatcher } = createDispatcher(config, client, {binding});
  binding?.start();
  const ws = new lark.WSClient({ appId: config.appId, appSecret: config.appSecret, logger, domain: lark.Domain.Feishu });
  let lastState;
  const timer = setInterval(() => {
    const state = ws.getConnectionStatus().state;
    if (state !== lastState) {
      lastState = state;
      console.log('[feishu] transport_' + state + '（仅表示连接状态，收发需实际测试）');
    }
  }, 1000);
  const stop = () => { clearInterval(timer); binding?.stop(); ws.close({ force: true }); };
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  console.log('正在启动飞书联通测试。进入会话自动显示入口；支持私聊文字与本程序的卡片按钮。');
  try { await ws.start({ eventDispatcher: dispatcher }); }
  catch { stop(); process.exitCode = 1; console.error('飞书启动失败，请检查本地配置和网络。'); }
}

module.exports = { readConfig, safeLogger, createProbe, createDispatcher };
if (require.main === module) main().catch(error => {
  console.error(error?.message?.startsWith('请在本目录') ? error.message : '飞书启动失败，请检查本地配置和依赖。');
  process.exitCode = 1;
});
