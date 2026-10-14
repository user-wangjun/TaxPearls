"use strict";
// Exercise the shipped controller: failed saves retain edits, stale sessions cannot display evidence.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.value="";this.checked=false;this.disabled=false;this.listeners={};this.style={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(key,value){this[key]=value;}
  addEventListener(key,fn){this.listeners[key]=fn;}
  querySelectorAll(selector){const tags=selector.split(",");return all(this).filter(n=>tags.includes(n.tag));}
}
function all(root){return [root,...root.children.flatMap(all)];}
(async()=>{
  const ids=new Map(),$=id=>{if(!ids.has(id))ids.set(id,new Node("div"));return ids.get(id);};
  const errors=[],calls=[];let actor={id:"one",role:"org_admin"},fail=false,delay=null;
  const sample={id:"task",revision:1,task:{company_name:"企业",tax_no:"91310000123456789A",start_date:"2026-01-01",end_date:"2026-01-31",applicable_forms:null},
    processing:{status:"needs_completion",issues:[],notices:[],records:[],boundary:"规则核验前"},documents:[],files:[{id:"excluded-file",deleted_at:null}],source_options:{"excluded-file":{excluded:true,original_name:"进项.xlsx"}},versions:[]};
  const api=async(url,options)=>{calls.push({url,options});if(delay)return await delay;
    if(fail&&url.endsWith("/prepare"))throw new Error("测试保存失败");return url==="/api/enterprise/input-tasks"&&!options?[]:structuredClone(sample);};
  const context={document:{createElement:tag=>new Node(tag)},URL,Blob,setTimeout,console,FormData:class{},};vm.createContext(context);vm.runInContext(fs.readFileSync("webapp/static/core-inputs.js","utf8"),context);
  const controller=context.createCoreInputs({api,el:(tag,_class,text)=>{const n=new Node(tag);n.textContent=text||"";return n;},$,user:()=>actor,showError:msg=>errors.push(msg)});
  await controller.refresh();for(const [id,value] of [["coreCompanyName","企业"],["coreTaxNo","91310000123456789A"],["coreStart","2026-01-01"],["coreEnd","2026-01-31"]])$(id).value=value;
  await $("coreCreateTask").onclick();
  const find=text=>all($("coreTaskResult")).find(n=>n.textContent===text);
  const field=label=>all($("coreTaskResult")).find(n=>n["aria-label"]===label);
  const review=find("资料口径核对");const editor=all($("coreTaskResult")).find(n=>n.tag==="fieldset");assert.ok(review);
  const forms=field("本期适用附表名称（以分号分隔）");forms.value="附表一";editor.listeners.input();
  const before=calls.length;await find("重新读取任务").onclick();assert.equal(calls.length,before);assert.match(errors.at(-1),/未保存/);assert.equal(forms.value,"附表一");
  field("已核实本期适用附表清单（如无适用附表，也须明确确认）").checked=true;
  fail=true;await find("保存核对并重新整理资料").onclick();assert.match(errors.at(-1),/保存失败/);assert.equal(forms.value,"附表一");assert.equal(forms.disabled,false);
  fail=false;await find("保存核对并重新整理资料").onclick();const write=calls.findLast(c=>c.url.endsWith("/prepare"));assert.deepEqual(JSON.parse(write.options.body).applicable_forms,["附表一"]);
  await find("恢复资料：进项.xlsx").onclick();const restored=calls.findLast(c=>c.url.endsWith("/prepare"));assert.deepEqual(JSON.parse(restored.options.body).source_options,{"excluded-file":{excluded:false}});
  let resolve;delay=new Promise(r=>resolve=r);const pending=find("重新读取任务").onclick();actor={id:"two",role:"org_admin"};$("coreMappingAI").checked=$("coreOCR").checked=true;controller.reset();resolve(structuredClone(sample));await pending;
  assert.equal($("coreTaskResult").children.length,0);assert.equal($("coreCompanyName").value,"");assert.equal($("coreMappingAI").checked,false);assert.equal($("coreOCR").checked,false);
  console.log("Core frontend: unsaved guard, real save dispatch, failed-save preservation, excluded file restoration and session isolation passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
