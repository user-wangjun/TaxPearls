"use strict";
// Mount existing teaching controllers inside the native workspace, with scoped DOM and lifecycle.
window.createTrainingWorkspace = ({api, valid}) => {
  let revision=0,controller=null,observer=null,pending=null;
  function destroy(){revision++;pending?.abort();pending=null;controller?.destroy();controller=null;observer?.disconnect();observer=null;}
  async function mount(container,staff){
    destroy();const ticket=revision,prefix="teaching-"+ticket+"-";
    pending=new AbortController();container.replaceChildren();container.classList.add("training-embedded");
    const loading=document.createElement("p");loading.className="muted";loading.textContent="正在读取教学内容…";container.append(loading);
    try{
      const html=await api('/api/training/workspace/template/'+(staff?'staff':'student'),{signal:pending.signal});
      if(ticket!==revision||!valid())return;
      const parsed=new DOMParser().parseFromString(html,'text/html'),fragment=document.createDocumentFragment();
      const toolbar=document.createElement('div');toolbar.className='sheet row';
      for(const id of ['who','header-actions']){const node=parsed.getElementById(id);if(node)toolbar.append(node);}
      fragment.append(toolbar);
      const content=parsed.getElementById('app-view');
      content.querySelector('#school-membership')?.remove();
      fragment.append(content);
      const modal=parsed.getElementById('pass-modal');if(modal)fragment.append(modal);
      container.replaceChildren(fragment);
      function decorate(node){
        if(node.nodeType!==1)return;
        for(const item of [node,...node.querySelectorAll('*')]){
          if(item!==container&&item.id&&!item.id.startsWith(prefix))item.id=prefix+item.id;
          for(const attr of ['for','aria-controls','aria-describedby','aria-labelledby']){
            if(item.hasAttribute(attr))item.setAttribute(attr,item.getAttribute(attr).split(/\s+/).map(id=>id.startsWith(prefix)?id:prefix+id).join(' '));
          }
          if(item.classList.contains('card')){item.classList.remove('card');item.classList.add('sheet');}
          if(item.classList.contains('item')){item.classList.remove('item');item.classList.add('data-item');}
          if(item.classList.contains('badge')||item.classList.contains('chip'))item.classList.add('role-chip');
          if(item.tagName==='BUTTON'){item.classList.add('btn');if(item.classList.contains('primary'))item.dataset.trainingPrimary='true';if(item.dataset.trainingPrimary!=='true')item.classList.add('re');item.classList.remove('primary','small');}
          if(item.id===prefix+'logout-btn')item.hidden=true;
        }
      }
      decorate(container);
      observer=new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)decorate(node);});
      observer.observe(container,{childList:true,subtree:true});
      const rewrite=selector=>selector.replace(/#([\w-]+)/g,(_,id)=>'#'+(id.startsWith(prefix)?id:prefix+id));
      const scoped={
        getElementById:id=>container.querySelector('#'+CSS.escape(prefix+id))||container.querySelector('#'+CSS.escape(id)),
        querySelector:selector=>container.querySelector(rewrite(selector))||container.querySelector(selector),
        querySelectorAll:selector=>Array.from(new Set([...container.querySelectorAll(rewrite(selector)),...container.querySelectorAll(selector)])),
        createElement:tag=>document.createElement(tag),body:document.body
      };
      controller=(staff?window.createTrainingStaff:window.createTrainingStudent)(scoped,{embedded:true});
      await controller.ready;
    }catch(error){
      if(ticket!==revision||!valid()||error.name==='AbortError')return;
      container.replaceChildren();const message=document.createElement('p');message.className='error-box';message.textContent=error.message;container.append(message);
    }
  }
  return {mount,destroy};
};
