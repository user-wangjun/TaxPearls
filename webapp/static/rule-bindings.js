"use strict";
window.RuleBindings=(()=>{
  let generation=0;
  const root=()=>document.getElementById("kgBindings");
  const reset=()=>{generation++;root()?.replaceChildren();};
  async function render(task,updated){
    reset();if(!task)return;
    const ticket=generation,owner=currentUser?.id;
    const metrics=await api("/api/knowledge/business-metrics");
    if(ticket!==generation||owner!==currentUser?.id)return;
    const box=el("details","kg-evidence-section"),summary=el("summary",null,"正式规则指标取数核对");box.append(summary);
    box.append(el("p","muted","选择实际来源记录和取数字段，核实单位、期间及统计调整口径后保存。数值由服务器计算，不能在此填写或覆盖金额。财务报表、历史、工资等额外指标可关联同企业同期的完整材料审计。"));
    const metric=el("select"),field=el("select"),aggregate=el("select"),basis=el("textarea"),records=el("div"),note=el("p","muted"),status=el("p","muted");
    metric.setAttribute("aria-label","正式规则指标");field.setAttribute("aria-label","指标原表取数字段");aggregate.setAttribute("aria-label","指标汇总方式");basis.setAttribute("aria-label","已核实的统计调整口径");basis.maxLength=1500;
    for(const item of metrics){const opt=el("option",null,item.name+"（"+item.unit+"）");opt.value=item.name;metric.append(opt);}
    for(const [value,text]of [["sum","合计所选记录"],["direct","单条直接取数"]]){const opt=el("option",null,text);opt.value=value;aggregate.append(opt);}
    function choose(){
      const item=metrics.find(m=>m.name===metric.value);note.textContent=item.requirements.join("；");records.replaceChildren();field.replaceChildren();
      const fields={account_balance:[["current_credit","本期贷方"],["current_debit","本期借方"],["opening_balance","期初余额"],["closing_balance","期末余额"]],vat_declaration:[["declared_value","申报值"]],invoice_detail:[["amount_excluding_tax","不含税金额"],["tax_amount","税额"]]};
      for(const [value,text]of fields[item.source_kind]||[]){const opt=el("option",null,text);opt.value=value;field.append(opt);}
      const old=task.task.metric_bindings?.[item.name];basis.value=old?.basis||"";
      if(old){field.value=old.field;aggregate.value=old.aggregation;}else{aggregate.value="sum";}
      for(const row of task.processing.records.filter(r=>r.material_type===item.source_kind)){
        const label=el("label"),pick=el("input");pick.type="checkbox";pick.value=row.id;pick.checked=!!old?.record_ids.includes(row.id);pick.dataset.metricSource="1";
        label.style.display="block";label.append(pick,document.createTextNode(" "+row.source_position+" · "+(row.item_label||row.account_name||row.invoice_no||"")+" · "+(fields[item.source_kind]||[]).map(([key,text])=>text+"："+(row[key]??"缺失")).join("；")));records.append(label);
      }
      if(!item.source_kind)records.append(el("p","material-note","该指标需要额外报表或调节底稿，请使用下面的已归档材料关联入口。"));
    }
    metric.onchange=choose;choose();
    const save=el("button",null,"保存指标来源核对");save.type="button";
    save.onclick=async()=>{
      save.disabled=true;status.textContent="正在按原记录核对…";
      try{const body={expected_revision:task.revision,name:metric.value,record_ids:Array.from(records.querySelectorAll("input:checked")).map(n=>n.value),field:field.value,aggregation:aggregate.value,basis:basis.value};const result=await api(`/api/enterprise/input-tasks/${encodeURIComponent(task.id)}/metric-bindings`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});if(ticket===generation&&owner===currentUser?.id)updated(result);}
      catch(error){if(ticket===generation&&owner===currentUser?.id)status.textContent=error.message;}
      finally{if(ticket===generation)save.disabled=false;}
    };
    box.append(metric,note,field,aggregate,records,basis,save,status);
    const audit=el("input"),auditButton=el("button",null,"关联已归档的完整材料指标");audit.setAttribute("aria-label","关联审计编号");audit.placeholder="填写同企业同期且当前有权访问的审计编号";auditButton.type="button";
    auditButton.onclick=async()=>{auditButton.disabled=true;try{const result=await api(`/api/enterprise/input-tasks/${encodeURIComponent(task.id)}/audit-metrics`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_revision:task.revision,audit_id:audit.value})});if(ticket===generation&&owner===currentUser?.id)updated(result);}catch(error){if(ticket===generation&&owner===currentUser?.id)status.textContent=error.message;}finally{if(ticket===generation)auditButton.disabled=false;}};
    box.append(el("h4",null,"额外报表与底稿"),audit,auditButton);root().replaceChildren(box);
  }
  return {render,reset};
})();
