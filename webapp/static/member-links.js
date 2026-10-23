"use strict";
window.TPMemberLinks = (() => {
  const element=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const names={accountant:"员工（会计）",teacher:"教师",student:"我的学生"};
  function button(text,action,primary=false){const n=element("button","btn tm-button"+(primary?"":" re"),text);n.type="button";n.addEventListener("click",action);return n;}
  function showMessage(container,text,error=false){container.textContent=text;container.setAttribute("role",error?"alert":"status");}
  function links(container,options){
    container.classList.add("tp-members");container.replaceChildren();
    const message=element("div","tm-message");message.setAttribute("aria-live","polite");container.append(message);
    const valid=options.valid||(()=>true);
    async function mutate(task){if(!valid())return;const previous=new Map(Array.from(container.querySelectorAll("button"),b=>[b,b.disabled]));previous.forEach((_,b)=>b.disabled=true);try{await task();}catch(err){if(valid())showMessage(message,err.message,true);}finally{if(valid())previous.forEach((disabled,b)=>b.disabled=disabled);}}
    for(const role of options.roles||[]){if(!options.links.some(l=>l.role===role))container.append(button("生成"+names[role]+"邀请链接",()=>mutate(()=>options.issue(role)),true));}
    for(const link of options.links){
      const item=element("section","tm-link"),heading=element("div","tm-row tm-between"),actions=element("div","tm-row");
      heading.append(element("strong",null,names[link.role]||link.role),element("span","tm-status role-chip",link.active?"可用":"已停用"));
      item.append(heading,element("p","tm-muted","累计加入 "+link.used_count+" 人 · 接受邀请时核销席位"));
      if(link.path){
        const address=new URL(link.path,location.origin).href;
        const input=element("input","tm-url");input.readOnly=true;input.value=address;input.setAttribute("aria-label",names[link.role]+"邀请链接");item.append(input);
        const copy=button("复制链接",async()=>{if(!valid())return;try{await navigator.clipboard.writeText(address);if(valid())showMessage(message,"邀请链接已复制。");}catch{if(valid()){input.focus();input.select();showMessage(message,"自动复制失败，请复制已选中的链接。",true);}}});copy.disabled=!link.active;actions.append(copy);
      }else item.append(element("p","tm-muted","此历史链接未保留可复制凭证，请刷新生成新链接。"));
      actions.append(button("刷新链接",()=>{if(valid()&&confirm("刷新后旧链接立即失效，已加入成员不受影响。继续？"))mutate(()=>options.refresh(link));}),button(link.active?"停用链接":"启用链接",()=>{if(valid()&&confirm(link.active?"停用后无法再通过此链接加入，现有成员保留。继续？":"确认重新启用此链接？"))mutate(()=>options.toggle(link));}));
      item.append(actions);container.append(item);
    }
  }
  function school(container,{request,valid,afterChange,embedded=false}){
    container.classList.add("tp-members");let version=0,snapshot=null,tab="mine";
    const status=element("div","tm-message");status.setAttribute("aria-live","polite");
    async function load(){const n=++version;container.replaceChildren(element("p","tm-muted","正在读取席位和邀请链接…"));try{const [data,invites,students]=await Promise.all([request("/api/training/membership"),request("/api/training/membership/links"),request("/api/training/membership/students")]);if(n!==version||!valid())return;snapshot=data;render(data,invites,students.students);}catch(err){if(n===version&&valid()){container.replaceChildren(status);showMessage(status,err.message,true);}}}
    async function action(task){if(!valid())return;const n=version,previous=new Map(Array.from(container.querySelectorAll("button"),b=>[b,b.disabled]));previous.forEach((_,b)=>b.disabled=true);try{await task();if(n!==version||!valid())return;await load();if(valid())await afterChange?.();}catch(err){if(n===version&&valid())showMessage(status,err.message,true);}finally{if(valid())previous.forEach((disabled,b)=>{if(b.isConnected)b.disabled=disabled;});}}
    function stat(label,value){const box=element("div","tm-stat kpi");box.append(element("div","tm-muted",label),element("div","tm-value value",value));return box;}
    function render(data,invites,students){
      container.replaceChildren();status.textContent="";
      const head=element("div","tm-row tm-between");head.append(element("h2",null,"成员邀请与席位"),element("span","tm-muted",data.school_name));if(!embedded)container.append(head);
      const nav=element("div","tm-tabs");
      if(data.administrator){const all=button("全校席位",()=>{tab="school";render(data,invites,students);});all.setAttribute("aria-pressed",String(tab==="school"));nav.append(all);}
      const mine=button("我的学生",()=>{tab="mine";render(data,invites,students);});mine.setAttribute("aria-pressed",String(tab==="mine"));nav.append(mine);container.append(nav,status);
      const schoolView=data.administrator&&tab==="school",stats=element("div","tm-stats");
      if(schoolView){stats.append(stat("教职工席位（含管理员）",data.quota.teachers_used+" / "+data.quota.teacher_quota),stat("已启用学生",data.quota.students_used+" / "+data.quota.student_quota),stat("学生额度待分配",data.quota.unallocated));}
      else stats.append(stat("我的学生额度",data.mine.seats),stat("已使用",data.mine.used),stat("剩余可用",data.mine.remaining));container.append(stats);
      if(schoolView){
        const section=element("section","tm-section");section.append(element("h3",null,"教师学生额度分配"),element("p","tm-muted","管理员本人也是教师，可给自己分配。学校总额度由平台顶级管理员设置。"));
        const table=element("table"),thead=element("thead"),tr=element("tr");for(const t of ["教职人员","已使用","剩余","学生额度"])tr.append(element("th",null,t));thead.append(tr);table.append(thead);const tbody=element("tbody");
        for(const t of data.teachers){const row=element("tr");for(const [label,text]of [["教职人员",t.display_name+(t.id===data.actor_id?"（我）":"")+(t.role!=="teacher"?" · 高校管理员":"")],["已使用",t.quota.used],["剩余",t.quota.remaining]]){const td=element("td",null,text);td.dataset.label=label;row.append(td);}const td=element("td");td.dataset.label="学生额度";const controls=element("div","tm-row"),input=element("input","tm-quota");input.type="number";input.min="0";input.max="100000";input.step="1";input.value=t.quota.seats;input.setAttribute("aria-label",t.display_name+"的学生额度");const save=button("保存",()=>{const seats=Number(input.value);if(!/^\d+$/.test(input.value)||!Number.isSafeInteger(seats)){showMessage(status,"请输入零或正整数额度。",true);return;}action(()=>request("/api/training/membership/teachers/"+encodeURIComponent(t.id)+"/quota",{method:"PUT",body:{seats,revision:t.quota.revision}}));});controls.append(input,save);td.append(controls);row.append(td);tbody.append(row);}table.append(tbody);section.append(table);container.append(section);
        if(data.unassigned_students.length){const legacy=element("section","tm-section");legacy.append(element("h3",null,"待确认教师归属的已有学生"),element("p","tm-muted","这些学生已占用学校席位。确认归属后转入对应教师名下，不重复扣减学校席位。"));for(const student of data.unassigned_students){const line=element("div","tm-row"),select=element("select");select.setAttribute("aria-label",student.name+"的归属教师");for(const t of data.teachers.filter(t=>t.active)){const option=element("option",null,t.display_name+(t.id===data.actor_id?"（我）":""));option.value=t.id;select.append(option);}line.append(element("span",null,student.name+" · "+student.student_no),select,button("确认归属",()=>action(()=>request("/api/training/membership/students/"+encodeURIComponent(student.id)+"/teacher",{method:"PUT",body:{teacher_id:select.value}}))));legacy.append(line);}container.append(legacy);}
      }else{
        const roster=element("section","tm-section");roster.append(element("h3",null,"我的学生"));
        if(!students.length)roster.append(element("p","tm-muted","尚无学生，复制下方邀请链接发给自己的学生。"));
        for(const p of students){const row=element("div","data-item tm-row tm-between");row.append(element("span",null,p.name+" · "+p.student_no),element("span","tm-muted",p.active?"已启用":"已停用"),button(p.active?"停用并释放席位":"重新启用",()=>action(()=>request("/api/training/staff/students/"+encodeURIComponent(p.id)+"/toggle",{method:"POST",body:{active:!p.active}}))));roster.append(row);}container.append(roster);
      }
      const inviteSection=element("section","tm-section");inviteSection.append(element("h3",null,schoolView?"教师邀请链接":"我的学生邀请链接"),element("p","tm-muted",schoolView?"教师加入后，再为其分配学生额度。":"通过此链接加入的学生归属你本人，接受时核销你的学生席位。"));const list=element("div");inviteSection.append(list);container.append(inviteSection);
      const role=schoolView?"teacher":"student";links(list,{links:invites.links.filter(l=>l.role===role),roles:[role],valid,issue:r=>action(()=>request("/api/training/membership/links",{method:"POST",body:{role:r}})),refresh:l=>action(()=>request("/api/training/membership/links/"+l.id,{method:"PUT",body:{revision:l.revision}})),toggle:l=>action(()=>request("/api/training/membership/links/"+l.id,{method:"PUT",body:{revision:l.revision,active:!l.active}}))});
    }
    load();return {load,destroy(){version++;snapshot=null;container.replaceChildren();},get snapshot(){return snapshot;}};
  }
  return {links,school};
})();
