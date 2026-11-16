'use strict';
const { createHash, createHmac, randomBytes, timingSafeEqual } = require('node:crypto');
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
      body:raw, signal:AbortSignal.timeout(operation.startsWith('task_') ? 10000 : 2500), redirect:'error',
    });
    if (!response.ok) throw new Error('binding_request_failed');
    return response.json();
  }
  const button = (label, value, type = 'default') => ({tag:'button', text:{tag:'plain_text',content:label},type,value});
  const card = (title, text, actions = []) => ({
    config:{wide_screen_mode:true}, header:{template:'blue',title:{tag:'plain_text',content:title}},
    elements:[{tag:'div',text:{tag:'plain_text',content:text}}, ...(actions.length ? [{tag:'action',actions}] : [])],
  });
  const withEntryHelp = content => {
    content.elements.push({tag:'note',elements:[{tag:'plain_text',content:'点击输入框上方“账户状态”核对连接，点击“唤出菜单”打开操作卡片；也可发送“菜单”。'}]});
    return content;
  };
  const taskSignature = payload => createHmac('sha256',config.binding.key).update('task-card:' + payload).digest('base64url');
  function taskButton(label, context, action, extra = {}, type = 'default') {
    const payload = Buffer.from(JSON.stringify({...context,...extra,action,issuedAt:now()})).toString('base64url');
    return button(label,{task_payload:payload,task_signature:taskSignature(payload)},type);
  }
  function taskMenu(subject,chat,tag) {
    const context={subject,chat,tag,operation_id:randomBytes(16).toString('hex')};
    return withEntryHelp(card('TaxPearls · 任务菜单','创建任务，查看真实任务进度，或打开已完成的任务。',[
      taskButton('创建任务',context,'create_prompt',{},'primary'),
      taskButton('进行中的任务',context,'list',{view:'ongoing',page:0}),
      taskButton('已完成的任务',context,'list',{view:'completed',page:0}),
    ]));
  }
  function boundStatus(subject,chat,tag) {
    const context={subject,chat,tag,operation_id:randomBytes(16).toString('hex')};
    return withEntryHelp(card('TaxPearls · 绑定状态','当前飞书账户已连接 TaxPearls。解除绑定后，已有任务、材料和报告仍保留在网站。',[
      taskButton('解除绑定',context,'unlink_prompt'),
    ]));
  }
  function websiteButton(label,url) {
    const parsed=new URL(url);
    if (parsed.username || parsed.password || (parsed.protocol!=='https:' && !(parsed.protocol==='http:' && ['127.0.0.1','localhost','[::1]'].includes(parsed.hostname)))) throw new Error('task_url_invalid');
    return {tag:'button',text:{tag:'plain_text',content:label},type:'default',url:parsed.href};
  }
  function taskList(result,context) {
    const labels={needs_completion:'待补充或核对资料',ready_for_analysis:'资料已就绪，可继续核查',parsing:'正在解析',archived:'已完成并归档'};
    const states={queued:'排队中',running:'处理中',done:'处理完成',failed:'处理失败，可在网站重试',superseded:'资料版本已变化'};
    const title=context.view==='completed'?'已完成的任务':'进行中的任务';
    const content=card('TaxPearls · '+title,result.count
      ? `当前账号最近 100 项可见任务中，共 ${result.count} 项；第 ${result.page+1} 页。`
      : `当前账号最近 100 项可见任务中，暂无${title}。`);
    for(const item of result.items) {
      const progress=item.progress;
      const text=[`${item.display_name||'资料处理任务'} · ${labels[item.status]||'待继续处理'}`,
        `已收材料 ${item.files} 份`,
        ...(progress?[`材料处理：${states[progress.state]||'待处理'} · 成功 ${progress.done}/${progress.total}，失败 ${progress.failed}`]:[]),
        ...(item.report_ready&&item.status!=='archived'?['当前资料版本的报告已生成，待完成归档']:[])].join('\n');
      content.elements.push({tag:'div',text:{tag:'plain_text',content:text}}, {tag:'action',actions:[websiteButton('打开任务',item.url)]});
    }
    const actions=[taskButton('刷新列表',context,'list',{page:result.page})];
    if(result.page>0)actions.push(taskButton('上一页',context,'list',{page:result.page-1}));
    if(result.has_more)actions.push(taskButton('下一页',context,'list',{page:result.page+1}));
    content.elements.push({tag:'action',actions});
    return withEntryHelp(content);
  }
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
  async function menu(subject, chat, eventId, command = 'menu') {
    if (command === 'help') return withEntryHelp(card('TaxPearls · 使用帮助', '账户状态：核对飞书与网站的绑定。\n唤出菜单：创建空白任务，查看进行中或已完成的真实任务。\n创建任务需再次确认，企业、期间和材料可在网站中补充。'));
    if (command === 'status') {
      const result = await request('lookup', {subject});
      if (!['bound','unbound'].includes(result.state)) throw new Error('binding_status_invalid');
      if(result.state === 'bound') {
        const current=await request('task_menu',{subject});
        if(!/^[0-9a-f]{64}$/.test(current.binding_tag||''))throw new Error('task_binding_invalid');
        return boundStatus(subject,chat,current.binding_tag);
      }
      return withEntryHelp(card('TaxPearls · 绑定状态','当前飞书账户尚未建立有效绑定。请点击“唤出菜单”，打开绑定入口并完成网站与原飞书私聊的两端确认。'));
    }
    const result = await request('issue', {subject,chat,event_id:eventId});
    if (result.state === 'bound') {
      const current=await request('task_menu',{subject});
      if(!/^[0-9a-f]{64}$/.test(current.binding_tag||''))throw new Error('task_binding_invalid');
      return taskMenu(subject,chat,current.binding_tag);
    }
    const url = new URL(result.url);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1','localhost','[::1]'].includes(url.hostname))) throw new Error('binding_url_invalid');
    return withEntryHelp(card('TaxPearls · 绑定账户', '欢迎使用 TaxPearls 助手。点击下方按钮连接你的企业账号：在网站登录并确认后，再回到此私聊完成最终确认。', [
      {tag:'button',text:{tag:'plain_text',content:'绑定 TaxPearls 账户'},type:'primary',url:url.href},
    ]));
  }
  function onTaskAction(data) {
    const rejected={toast:{type:'warning',content:'操作入口已失效，请重新唤出菜单。'}};
    const value=data?.action?.value;
    if(typeof value?.task_payload!=='string'||value.task_payload.length>2048||typeof value.task_signature!=='string')return rejected;
    let context;
    try {
      const expected=Buffer.from(taskSignature(value.task_payload)),actual=Buffer.from(value.task_signature);
      if(expected.length!==actual.length||!timingSafeEqual(expected,actual))return rejected;
      context=JSON.parse(Buffer.from(value.task_payload,'base64url').toString('utf8'));
      if(context.subject!==data.operator?.open_id||context.chat!==data.context?.open_chat_id
        ||!/^ou_[A-Za-z0-9]+$/.test(context.subject)||!/^oc_[A-Za-z0-9]+$/.test(context.chat)
        ||!/^[0-9a-f]{64}$/.test(context.tag||'')||!/^[0-9a-f]{32}$/.test(context.operation_id||'')
        ||!Number.isFinite(context.issuedAt)||now()<context.issuedAt||now()-context.issuedAt>600000
        ||!['create_prompt','create_confirm','list','unlink_prompt','unlink_confirm','unlink_cancel'].includes(context.action))return rejected;
    }catch{return rejected;}
    const key='task:'+createHash('sha256').update((data.event_id||'')+':'+value.task_payload).digest('hex');
    const accepted=enqueue(async()=>{
      try {
        const values={subject:context.subject,binding_tag:context.tag};
        let content;
        if(context.action==='unlink_prompt') {
          await request('task_menu',values);
          content=card('TaxPearls · 确认解除绑定','解除后，当前飞书账户将不能继续查看或创建 TaxPearls 任务；已有任务、材料和报告保留在网站。需要使用飞书时可重新绑定。',[
            taskButton('确认解除绑定',context,'unlink_confirm',{},'primary'),
            taskButton('保留绑定',context,'unlink_cancel'),
          ]);
        }else if(context.action==='unlink_cancel') {
          const current=await request('task_menu',values);
          content=boundStatus(context.subject,context.chat,current.binding_tag);
        }else if(context.action==='unlink_confirm') {
          const result=await request('binding_unlink',values);
          if(result.state!=='unbound')throw new Error('unlink_not_confirmed');
          content=withEntryHelp(card('TaxPearls · 已解除绑定','飞书与网站账户的连接已解除。已有任务、材料和报告保留在网站。点击“唤出菜单”可重新发起绑定。'));
        }else if(context.action==='create_prompt') {
          await request('task_menu',values);
          content=card('TaxPearls · 确认创建任务','将在当前绑定的 TaxPearls 账号下创建一项空白资料处理任务。创建后可在网站补充企业、检查期间和材料。此次不会启动 AI 检查。',[
            taskButton('确认创建空白任务',context,'create_confirm',{},'primary'),
          ]);
        }else if(context.action==='create_confirm') {
          const result=await request('task_create',{...values,operation_id:context.operation_id});
          content=withEntryHelp(card('TaxPearls · 任务已创建',`${result.display_name||'新建任务'} 已保存到网站。请打开任务补充企业、检查期间和材料。`,[websiteButton('打开任务',result.url)]));
        }else {
          const result=await request('task_list',{...values,view:context.view,page:context.page});
          content=taskList(result,context);
        }
        await send(context.chat,content,key);
        log('[feishu] operation_result_delivered：服务端已返回操作结果卡片。');
      }catch{
        const accountAction=context.action.startsWith('unlink_');
        log('[feishu] operation_unconfirmed：操作结果未确认，请核对网站与当前绑定。');
        await send(context.chat,card(accountAction?'账户操作结果未确认':'任务操作结果未确认',accountAction
          ?'请点击“账户状态”重新核对连接，或返回网站查看绑定状态。'
          :'请刷新任务列表或重新唤出菜单后核对；创建操作请先核对进行中列表，避免重复新建。'),key+':error');
      }
    });
    return {toast:{type:accepted?'info':'warning',content:accepted?'正在处理，请等待结果卡片。':'请求较多，请稍后重试。'}};
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
        const resultCard = card(completed?'TaxPearls · 绑定成功':'TaxPearls · 已取消绑定',
          completed?'两边确认已完成。网页会同步更新绑定状态。点击“唤出菜单”可创建任务或查询真实任务进度。':'本次请求已取消，没有建立新的账户绑定。');
        await send(chat, withEntryHelp(resultCard), value.id + ':' + result.state);
        log('[feishu] binding_' + result.state + '：服务端已确认绑定状态。');
      } catch {
        await send(chat, card('绑定未完成', '请求可能已过期、被取消或不属于当前私聊。请回网站核对状态，不要继续点击旧卡片。'), value.id + ':failed');
      }
    });
    return {toast:{type:accepted?'info':'warning',content:accepted?'正在核对绑定，请等待结果卡片。':'请求较多，请稍后重试。'}};
  }
  return {menu,onAction,onTaskAction,poll,request,drain:()=>Promise.all([...pending]),
    start(){if(!timer){stopped=false;timer=setInterval(poll,2500);poll();}},
    stop(){stopped=true;clearInterval(timer);timer=null;},
  };
}
module.exports = {bridgeSettings,createBindingBridge};
