'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const lark = require('@larksuiteoapi/node-sdk');
const { readConfig, safeLogger, createDispatcher } = require('./bot.cjs');

const env = { TAXPEARLS_FEISHU_APP_ID: 'cli_test', TAXPEARLS_FEISHU_APP_SECRET: 'synthetic-secret' };
function fixture(options = {}, configEnv = {}) {
  const calls = [], logs = [];
  const client = { im: { message: { create: async request => {
    calls.push(request); return { code: 0, data: { message_id: 'om_reply' } };
  } } } };
  const config = readConfig({ ...env, ...configEnv });
  return { calls, logs, client, config, ...createDispatcher(config, client, { log: s => logs.push(s), ...options }) };
}
function event(type, body, header = {}) {
  return { schema: '2.0', header: { app_id: 'cli_test', event_type: type, event_id: 'evt_1', tenant_key: 'tenant_test', ...header }, event: body };
}
function message(patch = {}) {
  return event('im.message.receive_v1', {
    sender: { sender_id: { open_id: 'ou_test' }, sender_type: 'user', tenant_key: 'tenant_test' },
    message: { message_id: 'om_input', chat_id: 'oc_test', chat_type: 'p2p', message_type: 'text', content: JSON.stringify({ text: '菜单' }), ...patch },
  });
}
const dispatch = (f, e) => f.dispatcher.invoke(e, { needCheck: false }); // Authenticated WS dispatch contract.
async function menu(f) {
  await dispatch(f, message()); await f.drain();
  return JSON.parse(f.calls[0].data.content);
}
function click(value, patch = {}) {
  return event('card.action.trigger', { operator: { open_id: 'ou_test', tenant_key: 'tenant_test' }, context: { open_chat_id: 'oc_test', open_message_id: 'om_reply' }, action: { tag: 'button', value }, ...patch });
}
function entered(eventId = 'visit_1') {
  return event('im.chat.access_event.bot_p2p_chat_entered_v1', {
    chat_id: 'oc_test', operator_id: { open_id: 'ou_test' },
  }, { event_id: eventId });
}
function firstConversation() {
  return { uuid: 'first_1', type: 'event_callback', event: {
    type: 'p2p_chat_create', app_id: 'cli_test', tenant_key: 'tenant_test', chat_id: 'oc_test',
    user: { open_id: 'ou_test' }, operator: { open_id: 'ou_test' },
  } };
}

test('entering a conversation sends the menu without a text message', async () => {
  const f = fixture(); await dispatch(f, entered()); await f.drain();
  assert.equal(f.calls.length, 1);
  const card = JSON.parse(f.calls[0].data.content);
  assert.match(card.elements[0].content, /请直接选择/);
  assert.equal((await dispatch(f, click(card.elements[1].actions[0].value))).toast.type, 'success');
});
test('first creation and repeated enter events share the same automatic menu suppression', async () => {
  const f = fixture();
  await Promise.all([dispatch(f, firstConversation()), dispatch(f, entered()), dispatch(f, entered('visit_2'))]);
  await f.drain(); await dispatch(f, entered('visit_3')); await f.drain();
  assert.equal(f.calls.length, 1);
});
test('reopening refreshes an expired menu while manual messages still work', async () => {
  let stamp = 1000; const f = fixture({ now: () => stamp });
  await menu(f); await dispatch(f, entered()); await f.drain(); assert.equal(f.calls.length, 1);
  stamp += 600001; await dispatch(f, entered('visit_2')); await f.drain(); assert.equal(f.calls.length, 2);
  await dispatch(f, message({ message_id: 'om_new' })); await f.drain(); assert.equal(f.calls.length, 3);
});
test('enter events cannot bypass application, user, tenant or chat validation', async () => {
  const f = fixture({}, { TAXPEARLS_FEISHU_TENANT_KEY: 'tenant_test', TAXPEARLS_FEISHU_ALLOWED_OPEN_IDS: 'ou_test' });
  const foreign = entered(); foreign.header.app_id = 'cli_other';
  const tenant = entered(); tenant.header.tenant_key = 'tenant_other';
  const user = entered(); user.event.operator_id.open_id = 'ou_other';
  const malformed = entered(); delete malformed.event.operator_id;
  const chat = entered(); chat.event.chat_id = 'bad';
  const noId = entered(); noId.header.event_id = '';
  for (const value of [foreign, tenant, user, malformed, chat, noId]) await dispatch(f, value);
  await f.drain(); assert.equal(f.calls.length, 0);
});
test('automatic send failures permit retry without waiting for the suppression window', async () => {
  const f = fixture(); let attempt = 0;
  f.client.im.message.create = async request => {
    f.calls.push(request);
    if (++attempt === 1) throw new Error('synthetic');
    return { code: 0, data: { message_id: 'om_retry' } };
  };
  await dispatch(f, entered()); await f.drain(); await dispatch(f, entered()); await f.drain();
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].data.uuid, f.calls[1].data.uuid);
});
test('automatic menus are isolated per user and chat', async () => {
  const f = fixture(); const other = entered('visit_other');
  other.event.chat_id = 'oc_other'; other.event.operator_id.open_id = 'ou_other';
  await dispatch(f, entered()); await dispatch(f, other); await f.drain(); assert.equal(f.calls.length, 2);
});

test('missing credentials fail without printing values', () => {
  assert.throws(() => readConfig({ TAXPEARLS_FEISHU_APP_SECRET: 'secret-not-for-logs' }), error => !error.message.includes('secret-not-for-logs'));
  assert.throws(() => readConfig({ ...env, TAXPEARLS_FEISHU_APP_ID: 'https://example.com' }));
});
test('SDK logs never forward credentials, payloads, URLs or errors', () => {
  const logs = [], logger = safeLogger(s => logs.push(s));
  for (const method of ['trace', 'debug', 'info', 'warn', 'error']) logger[method]('synthetic-secret', { content: 'private-body' });
  assert.equal(logs.length, 2);
  assert.ok(logs.every(s => !s.includes('synthetic-secret') && !s.includes('private-body')));
});
test('a private message creates a real SDK request containing the three approved entries', async () => {
  const f = fixture(); const card = await menu(f);
  assert.deepEqual(card.elements[1].actions.map(a => a.text.content), ['创建任务', '进行中的任务', '已完成的任务']);
  assert.equal(f.calls[0].params.receive_id_type, 'chat_id');
  assert.equal(f.calls[0].data.receive_id, 'oc_test');
  assert.match(card.elements[0].content, /尚未接入真实任务/);
});
test('all three card buttons return an immediate toast and replacement card', async () => {
  const f = fixture(); const card = await menu(f);
  for (const action of card.elements[1].actions) {
    const result = await dispatch(f, click(action.value));
    assert.equal(result.toast.type, 'success');
    assert.equal(result.card.type, 'raw');
    assert.ok(result.card.data.elements[0].content.includes(action.text.content));
  }
  assert.equal(f.calls.length, 1); // Callbacks have no business side effects or additional API calls.
});
test('duplicate and concurrent message events send only one card', async () => {
  const f = fixture();
  await Promise.all([dispatch(f, message()), dispatch(f, message()), dispatch(f, message())]);
  await f.drain(); assert.equal(f.calls.length, 1);
});
test('slow outgoing HTTP does not block event acknowledgment', async () => {
  const f = fixture(); let release;
  f.client.im.message.create = () => new Promise(resolve => { release = resolve; });
  await dispatch(f, message());
  assert.equal(typeof release, 'function');
  release({ code: 0, data: { message_id: 'om_reply' } }); await f.drain();
});
test('groups, bots, files and foreign app events are ignored', async () => {
  const f = fixture();
  const bot = message(); bot.event.sender.sender_type = 'app';
  const foreign = message(); foreign.header.app_id = 'cli_other';
  const malformed = message(); malformed.event.sender.sender_id.open_id = undefined;
  for (const e of [message({ chat_type: 'group' }), message({ message_type: 'file' }), bot, foreign, malformed]) await dispatch(f, e);
  await f.drain(); assert.equal(f.calls.length, 0);
});
test('optional tenant and tester restrictions apply to messages and clicks', async () => {
  const f = fixture({}, { TAXPEARLS_FEISHU_TENANT_KEY: 'tenant_test', TAXPEARLS_FEISHU_ALLOWED_OPEN_IDS: 'ou_test' });
  const wrongTenant = message(); wrongTenant.header.tenant_key = 'tenant_other';
  const wrongUser = message(); wrongUser.event.sender.sender_id.open_id = 'ou_other';
  await dispatch(f, wrongTenant); await dispatch(f, wrongUser); assert.equal(f.calls.length, 0);
  const card = await menu(f);
  const response = await dispatch(f, click(card.elements[1].actions[0].value, { operator: { open_id: 'ou_other', tenant_key: 'tenant_test' } }));
  assert.equal(response.toast.type, 'warning');
});
test('tampered, expired, forwarded and malformed buttons cannot update a card', async () => {
  let stamp = 1000; const f = fixture({ now: () => stamp }); const card = await menu(f);
  const value = card.elements[1].actions[0].value;
  for (const e of [click({ ...value, signature: 'wrong' }), click(value, { context: { open_chat_id: 'oc_other' } }), click(value, { operator: { open_id: 'ou_other' } }), click(null)]) {
    assert.equal((await dispatch(f, e)).toast.type, 'warning');
  }
  stamp += 600001;
  assert.equal((await dispatch(f, click(value))).toast.type, 'warning');
});
test('restart invalidates old probe cards', async () => {
  const f = fixture(), other = fixture(); const card = await menu(f);
  assert.equal((await dispatch(other, click(card.elements[1].actions[0].value))).toast.type, 'warning');
});
test('API rejection is reported as failure and redelivery keeps the same idempotency key', async () => {
  const f = fixture(); let attempt = 0;
  f.client.im.message.create = async request => {
    f.calls.push(request); return ++attempt === 1 ? { code: 99991672 } : { code: 0, data: { message_id: 'om_reply' } };
  };
  await dispatch(f, message()); await f.drain(); await dispatch(f, message()); await f.drain();
  assert.equal(f.calls.length, 2); assert.equal(f.calls[0].data.uuid, f.calls[1].data.uuid);
  assert.ok(f.logs.some(s => s.includes('delivery_failed')));
});
test('bounded dedup storage recovers after expiry', async () => {
  let stamp = 1000; const f = fixture({ capacity: 1, now: () => stamp });
  await menu(f); await dispatch(f, message({ message_id: 'om_second' })); await f.drain(); assert.equal(f.calls.length, 1);
  stamp += 600001; await dispatch(f, message({ message_id: 'om_second' })); await f.drain(); assert.equal(f.calls.length, 2);
});
test('installed SDK carries a new card callback through a WebSocket event frame and encodes its response', async () => {
  const f = fixture(); const card = await menu(f);
  const ws = new lark.WSClient({ appId: f.config.appId, appSecret: f.config.appSecret, logger: safeLogger(() => {}) });
  try {
    ws.eventDispatcher = f.dispatcher;
    const replies = []; ws.sendMessage = frame => replies.push(frame);
    await ws.handleEventData({ headers: Object.entries({ type: 'event', message_id: 'frame_1', trace_id: 'trace_1', sum: '1', seq: '0' }).map(([key, value]) => ({ key, value })), payload: Buffer.from(JSON.stringify(click(card.elements[1].actions[0].value))) });
    assert.equal(replies.length, 1);
    const envelope = JSON.parse(Buffer.from(replies[0].payload).toString());
    assert.equal(envelope.code, 200);
    const result = JSON.parse(Buffer.from(envelope.data, 'base64').toString());
    assert.equal(result.toast.type, 'success'); assert.equal(result.card.type, 'raw');
  } finally { ws.close({ force: true }); }
});
