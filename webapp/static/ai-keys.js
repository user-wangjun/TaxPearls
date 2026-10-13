"use strict";

function createAIKeyProbes({api, el, $, user}) {
  let revision = 0;
  let adding = false;
  let editing = null, editorEpoch = 0, defaults = {};
  let modelsEpoch = 0;
  let upstreamModels = [], activeModel = -1;
  let modelCapabilities = {}, capabilityCredential = "", revealedSavedKey = "";
  const effortLabels = {default:"上游默认",none:"关闭思考",enabled:"开启思考",
    minimal:"极低（minimal）",low:"低（low）",medium:"中（medium）",high:"高（high）",
    xhigh:"超高（xhigh）",max:"最大（max）"};
  function credentialContext() {
    const secret = $("aiKeyAddSecret").value.trim();
    return JSON.stringify([$("aiKeyAddURL").value.trim().replace(/\/+$/, ""),
      editing && (!secret || secret === revealedSavedKey) ? "saved:" + editing.id : "draft:" + secret]);
  }
  function currentCapability() {
    const model = $("aiKeyAddModel").value.trim();
    if (capabilityCredential === credentialContext()) return modelCapabilities[model] || {state:"unknown",levels:[]};
    if (editing && model === editing.model && $("aiKeyAddURL").value.trim().replace(/\/+$/, "") === editing.base_url &&
        (!$("aiKeyAddSecret").value.trim() || $("aiKeyAddSecret").value.trim() === revealedSavedKey)) return editing.reasoning_capability || {state:"unknown",levels:[]};
    return {state:"unknown",levels:[]};
  }
  function renderReasoning(value=$("aiKeyReasoning").value || "default") {
    const info = currentCapability(), select = $("aiKeyReasoning");
    const modes = ["default", ...(info.can_disable === true ? ["none"] : []),
      ...(info.can_enable === true && (!info.levels?.length || value === "enabled") ? ["enabled"] : []), ...(info.levels || [])];
    select.replaceChildren();
    for (const mode of modes) { const option = el("option",null,effortLabels[mode]); option.value = mode; select.append(option); }
    if (!modes.includes(value)) {
      const legacy = editing?.reasoning_legacy && value === editing.reasoning_effort && info.state === "unknown";
      const option = el("option",null,(effortLabels[value] || value) + (legacy ? "（原配置，未确认支持）" : "（未确认支持，请更换）"));
      option.value = value; option.disabled = !legacy; select.append(option);
    }
    select.value = value;
    const message = {unknown:"请从上游获取模型能力，未确认的等级不可选。",
      unsupported:"上游未声明支持调节，保持默认。",
      toggle:"仅支持思考开关，不提供等级调节。",
      levels:(info.levels || []).includes("medium") ? "仅展示上游支持的等级。" : "此模型不提供 medium（中）档。"};
    $("aiKeyReasoningStatus").textContent = (message[info.state] || message.unknown) +
      (info.can_enable && info.can_disable === false ? " 必须开启思考，不能关闭。" : "");
  }
  const usages = [["extraction","材料提取兜底"],["knowledge","知识问答"],
    ["report","风险解读与报告"],["tutor","教学答疑"]];
  function usageName(values) { return values.length ? values.map(value=>usages.find(item=>item[0]===value)?.[1] || value).join("、") : "仅检测"; }
  function usageChoices(container, values=[], name="") {
    container.replaceChildren();
    const fields = el("fieldset", "probe-usage-choices"), legend = el("legend",null,"业务用途（可多选）");
    fields.append(legend);
    for (const [value, label] of usages) {
      const item = el("label"), box = el("input"); box.type="checkbox"; box.value=value; box.checked=values.includes(value);
      if (name) box.name=name;
      item.append(box,el("span",null,label)); fields.append(item);
    }
    const only = el("label", "probe-only-detection"), toggle = el("input"); toggle.type="checkbox"; toggle.checked=!values.length;
    only.append(toggle,el("span",null,"仅检测（不参与业务调用）"));
    const update = () => { for (const box of fields.querySelectorAll("input")) box.disabled=toggle.checked; };
    toggle.addEventListener("change", update); update();
    container.append(fields,only);
    return () => toggle.checked ? [] : [...fields.querySelectorAll("input:checked")].map(box=>box.value);
  }
  let editorUsages = () => [];
  function reset() {
    revision++;
    $("aiKeyCards").replaceChildren();
    $("aiTemporaryKey").value = "";
    $("aiTemporaryResult").replaceChildren();
    $("aiKeyStatus").textContent = "";
    $("aiKeyAddForm").reset();
    if ($("aiKeyAddDialog").open) $("aiKeyAddDialog").close();
    clearEditor();
  }
  function current(token, owner) {
    return token === revision && user()?.id === owner && user()?.role === "platform_admin";
  }
  function summarize(summary, result, operation) {
    if (!summary) return;
    summary.replaceChildren();
    summary.classList.remove("probe-ok", "probe-failed");
    if (operation === "health") {
      const labels = {ok:"可用",invalid_key:"密钥失效",insufficient_balance:"余额不足",
        forbidden:"权限不足",rate_limited:"限流",timeout:"超时",network:"连接失败"};
      summary.textContent = result ? labels[result.status] || "检测失败" : "待检测";
    } else {
      summary.append(el("span", "muted", result?.scope === "key_limit" ? "密钥额度" : "余额 / 额度"));
      summary.append(el("strong", "probe-balance-value", result?.status === "ok" && result.balances
        ? result.balances.map(item => item.total === null ? "未设额度上限" : `${item.currency} ${item.total}`).join(" / ")
        : result ? "暂无法查询" : "尚未查询"));
      if (result) summary.append(el("span", "muted probe-checked", new Date(result.checked_at).toLocaleString("zh-CN", {hour12:false})));
    }
    if (result) {
      summary.title = result.message;
      summary.classList.add(result.status === "ok" && result.available !== false ? "probe-ok" : "probe-failed");
    }
  }
  function display(box, result, operation, summary) {
    summarize(summary, result, operation);
    box.replaceChildren();
    if (!result) { box.append(el("p", "muted", operation === "health" ? "尚未测活" : "尚未查询金额")); return; }
    box.append(el("p", result.status === "ok" ? "probe-ok" : "probe-failed",
      (operation === "health" ? "测活：" : "金额：") + result.message));
    if (result.status === "ok" && result.balances) {
      for (const balance of result.balances) {
        box.append(el("p", "probe-amount", balance.total === null
          ? "未设置密钥额度上限（账户余额未查询）" : `${balance.currency} ${balance.total}`));
        const detail = [];
        if (balance.granted !== undefined) detail.push("赠金 " + balance.granted);
        if (balance.topped_up !== undefined) detail.push("充值余额 " + balance.topped_up);
        if (balance.usage !== undefined) detail.push("累计用量 " + balance.usage);
        if (detail.length) box.append(el("p", "muted", detail.join(" · ") + " " + balance.currency));
      }
      if (result.available === false) box.append(el("p", "probe-failed", "服务商报告当前账户无可用余额。"));
    }
    const date = new Date(result.checked_at).toLocaleString("zh-CN", {hour12:false});
    box.append(el("p", "muted", `${date} · ${result.elapsed_ms} ms` +
      (result.http_status ? ` · HTTP ${result.http_status}` : "") +
      (result.protocol ? ` · 协议 ${result.protocol}` : "")));
  }
  async function probe(body, box, button, operation, summary) {
    const token = revision, owner = user()?.id;
    button.disabled = true;
    box.replaceChildren(el("p", "muted", operation === "health" ? "正在测活…" : "正在查询金额…"));
    if (summary) {
      summary.textContent = operation === "health" ? "检测中…" : "查询中…";
      summary.classList.remove("probe-ok", "probe-failed"); summary.removeAttribute("title");
    }
    try {
      const result = await api("/api/admin/ai-keys/probe", {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
      });
      if (current(token, owner)) display(box, result, operation, summary);
    } catch (err) {
      if (current(token, owner)) {
        box.replaceChildren(el("p", "probe-failed", err.message));
        if (summary) { summary.textContent = "检测失败"; summary.classList.add("probe-failed"); }
      }
    } finally { if (current(token, owner)) button.disabled = false; }
  }
  async function load() {
    if (user()?.role !== "platform_admin") return;
    const token = ++revision, owner = user().id;
    $("aiTemporaryKey").value = ""; $("aiTemporaryResult").replaceChildren();
    $("aiKeyStatus").textContent = "正在读取密钥配置…"; $("aiKeyCards").replaceChildren();
    try {
      const data = await api("/api/admin/ai-keys");
      if (!current(token, owner)) return;
      defaults = data;
      $("aiKeyStatus").textContent = `${data.keys.length} 把密钥 · 业务 AI ${data.enabled ? "已启用" : "未启用，可手动检测"}`;
      if (!data.keys.length) $("aiKeyCards").append(el("p", "muted", "服务端尚未配置密钥，可使用下面的临时密钥检测。"));
      for (const key of data.keys) {
        const card = el("article", "sheet probe-card");
        const identity = el("div", "probe-identity"), icon = el("span", "probe-service-icon", "AI");
        try { const host = new URL(key.base_url).hostname; icon.textContent = host === "api.deepseek.com" ? "DS" : host === "openrouter.ai" ? "OR" : "AI"; } catch (_) {}
        icon.setAttribute("aria-hidden", "true");
        const info = el("div", "probe-info"), title = el("div", "probe-title"), badge = el("span", "probe-badge");
        badge.setAttribute("role", "status");
        title.append(el("h3", null, key.name), badge);
        info.append(title, el("p", "probe-endpoint", key.base_url || "接口配置待核对"),
          el("p", "muted probe-model", key.model + " · " + key.masked_key));
        identity.append(icon, info);
        const brief = el("div", "probe-balance-brief");
        brief.setAttribute("role", "status");
        const actions = el("div", "probe-actions"), health = el("div", "probe-result"), balance = el("div", "probe-result");
        const test = el("button", "btn", "测活"), money = el("button", "btn pdf", "查询金额"),
          edit = el("button", "btn pdf", "编辑"), drop = el("button", "btn probe-delete", "删除");
        test.type = money.type = edit.type = drop.type = "button";
        test.disabled = money.disabled = !!key.problem;
        test.addEventListener("click", () => probe({key_id:key.id, operation:"health"}, health, test, "health", badge));
        money.addEventListener("click", () => probe({key_id:key.id, operation:"balance"}, balance, money, "balance", brief));
        edit.addEventListener("click", () => openEditor(key));
        const purpose = el("details", "probe-purpose"), summary = el("summary",null,usageName(key.usages));
        summary.setAttribute("aria-label", "修改用途：" + usageName(key.usages));
        const chooser = el("div", "probe-usage-popover"), choices = el("div");
        let readChoices = usageChoices(choices,key.usages);
        const savePurpose = el("button","btn pdf","保存用途"), cancelPurpose = el("button","btn pdf","取消");
        savePurpose.type=cancelPurpose.type="button";
        const choiceActions=el("div","probe-header-actions");choiceActions.append(cancelPurpose,savePurpose);
        const choiceStatus=el("p","muted");choiceStatus.setAttribute("role","status");
        chooser.append(choices,choiceStatus,choiceActions);purpose.append(summary,chooser);
        cancelPurpose.addEventListener("click",()=>{purpose.open=false; readChoices=usageChoices(choices,key.usages); choiceStatus.textContent="";});
        purpose.addEventListener("toggle",()=>{
          if (purpose.open) $("aiKeyCards").querySelectorAll(".probe-purpose").forEach(item=>{if(item!==purpose)item.open=false;});
        });
        savePurpose.addEventListener("click", async () => {
          const token = revision, owner = user()?.id;
          const selected = readChoices();
          if (!selected.length && !choices.querySelector(".probe-only-detection input").checked) { choiceStatus.textContent="请勾选业务用途，或开启仅检测。";return; }
          savePurpose.disabled = true; choiceStatus.textContent="正在保存…";
          try {
            await api("/api/admin/ai-keys/" + encodeURIComponent(key.id), {method:"PUT",
              headers:{"Content-Type":"application/json"}, body:JSON.stringify({revision:key.revision, usages:selected})});
            if (current(token, owner)) await load();
          } catch (err) {
            if (current(token, owner)) choiceStatus.textContent = "用途保存失败：" + err.message;
          } finally { savePurpose.disabled = false; }
        });
        drop.addEventListener("click", async () => {
          if (!confirm(`从本系统删除「${key.name}」？后续网页调用将不再使用它。`)) return;
          const owner = user()?.id;
          drop.disabled = true;
          try {
            await api("/api/admin/ai-keys/" + encodeURIComponent(key.id), {
              method:"DELETE", headers:{"Content-Type":"application/json"}, body:JSON.stringify({revision:key.revision})
            });
            if (user()?.id === owner && user()?.role === "platform_admin") await load();
          } catch (err) {
            if (user()?.id === owner) $("aiKeyStatus").textContent = "删除失败：" + err.message;
          } finally { drop.disabled = false; }
        });
        actions.append(test, money, edit, drop);
        const details = el("details", "probe-details"), detailBody = el("div", "probe-detail-body");
        details.append(el("summary", null, "检测详情")); detailBody.append(health, balance); details.append(detailBody);
        card.append(identity, brief, purpose, actions, details);
        if (key.problem) { card.append(el("p", "probe-failed probe-config-problem", key.problem)); details.open = true; }
        display(health, key.health, "health", badge); display(balance, key.balance, "balance", brief); $("aiKeyCards").append(card);
      }
    } catch (err) { if (current(token, owner)) $("aiKeyStatus").textContent = err.message; }
  }
  $("btnRefreshAIKeys").addEventListener("click", load);
  function clearEditor() {
    editing = null; editorEpoch++;
    $("aiKeyAddForm").reset(); $("aiKeyAddSecret").type = "password";
    $("aiKeyAddSecret").value = ""; $("btnRevealAIKey").textContent = "查看 key";
    $("btnRevealAIKey").disabled = false;
    revealedSavedKey = "";
    $("aiKeyAddForm").querySelectorAll("details").forEach(item => { item.open = false; });
    clearModels();
  }
  function clearModels() {
    modelsEpoch++;
    upstreamModels = []; activeModel = -1;
    closeModelChoices(); $("aiKeyModelChoices").replaceChildren(); $("btnChooseAIModel").hidden = true;
    $("aiKeyModelStatus").textContent = ""; $("btnFetchAIModels").disabled = false;
    modelCapabilities = {}; capabilityCredential = "";
    renderReasoning();
  }
  for (const id of ["aiKeyAddURL", "aiKeyAddSecret"]) $(id).addEventListener("input", () => { clearModels(); renderReasoning("default"); });
  function closeModelChoices() {
    $("aiKeyModelChoices").hidden = true;
    $("aiKeyAddModel").setAttribute("aria-expanded", "false");
    $("btnChooseAIModel").setAttribute("aria-expanded", "false");
    $("aiKeyAddModel").removeAttribute("aria-activedescendant");
  }
  function highlightModel(index) {
    activeModel = (index + upstreamModels.length) % upstreamModels.length;
    [...$("aiKeyModelChoices").children].forEach((option, i) => option.classList.toggle("is-active", i === activeModel));
    const option = $("aiKeyModelChoices").children[activeModel];
    $("aiKeyAddModel").setAttribute("aria-activedescendant", option.id);
    option.scrollIntoView({block:"nearest"});
  }
  function openModelChoices() {
    if (!upstreamModels.length) return;
    [...$("aiKeyModelChoices").children].forEach((option, i) => option.setAttribute("aria-selected", String(upstreamModels[i] === $("aiKeyAddModel").value)));
    $("aiKeyModelChoices").hidden = false;
    $("aiKeyAddModel").setAttribute("aria-expanded", "true");
    $("btnChooseAIModel").setAttribute("aria-expanded", "true");
    highlightModel(Math.max(0, upstreamModels.indexOf($("aiKeyAddModel").value)));
  }
  function chooseModel(index) {
    $("aiKeyAddModel").value = upstreamModels[index];
    [...$("aiKeyModelChoices").children].forEach((option, i) => option.setAttribute("aria-selected", String(i === index)));
    closeModelChoices(); renderReasoning("default"); $("aiKeyAddModel").focus({preventScroll:true});
  }
  $("aiKeyAddModel").addEventListener("input", () => { closeModelChoices(); renderReasoning("default"); });
  $("btnChooseAIModel").addEventListener("click", () => {
    if ($("aiKeyModelChoices").hidden) { $("aiKeyAddModel").focus({preventScroll:true}); openModelChoices(); }
    else closeModelChoices();
  });
  $("aiKeyAddModel").addEventListener("keydown", event => {
    if (!upstreamModels.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if ($("aiKeyModelChoices").hidden) openModelChoices();
      else highlightModel(activeModel + (event.key === "ArrowDown" ? 1 : -1));
    } else if (!$("aiKeyModelChoices").hidden && event.key === "Enter") {
      event.preventDefault(); chooseModel(activeModel);
    } else if (!$("aiKeyModelChoices").hidden && event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); closeModelChoices();
    } else if (event.key === "Tab") closeModelChoices();
  });
  $("aiKeyAddDialog").addEventListener("pointerdown", event => {
    if (!event.target.closest(".probe-model-combobox")) closeModelChoices();
  });
  $("btnFetchAIModels").addEventListener("click", async () => {
    if (adding || user()?.role !== "platform_admin") return;
    const body = {base_url:$("aiKeyAddURL").value.trim(), api_key:$("aiKeyAddSecret").value.trim()};
    if (!body.base_url || (!body.api_key && !editing)) { $("aiKeyModelStatus").textContent = "请先填写接口 URL 和 key。"; return; }
    if (editing) { body.key_id = editing.id; body.revision = editing.revision; }
    clearModels();
    const token = editorEpoch, requestToken = modelsEpoch, owner = user()?.id;
    const valid = () => token === editorEpoch && requestToken === modelsEpoch &&
      user()?.id === owner && user()?.role === "platform_admin" && $("aiKeyAddDialog").open && !adding;
    $("btnFetchAIModels").disabled = true; $("aiKeyModelStatus").textContent = "正在从上游获取…";
    try {
      const data = await api("/api/admin/ai-keys/models", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if (!valid()) return;
      modelCapabilities = data.capabilities || {}; capabilityCredential = credentialContext();
      renderReasoning();
      upstreamModels = data.models;
      const choices = $("aiKeyModelChoices");
      for (const [index, model] of upstreamModels.entries()) {
        const option = el("button", "probe-model-option", model); option.type = "button"; option.tabIndex = -1;
        option.id = "aiKeyModelOption-" + index; option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(model === $("aiKeyAddModel").value));
        option.addEventListener("click", () => chooseModel(index)); choices.append(option);
      }
      $("btnChooseAIModel").hidden = !upstreamModels.length;
      $("aiKeyModelStatus").textContent = data.models.length ? `获取到 ${data.models.length} 个模型，请选择；也可手动填写。` : "上游返回了空模型列表，可手动填写。";
    } catch (err) { if (valid()) $("aiKeyModelStatus").textContent = err.message + (err.message.includes("手动填写") ? "" : " 可手动填写模型名称。"); }
    finally { if (valid()) $("btnFetchAIModels").disabled = false; }
  });
  function openEditor(key=null) {
    if (user()?.role !== "platform_admin" || adding) return;
    clearEditor(); editing = key;
    $("aiKeyAddTitle").textContent = key ? "编辑密钥" : "加入密钥";
    $("btnSaveAIKey").textContent = key ? "保存修改" : "加入密钥";
    $("aiKeyAddName").value = key?.name || "";
    $("aiKeyAddURL").value = key?.base_url || defaults.default_base_url || "";
    $("aiKeyAddModel").value = key?.model || defaults.default_model || "";
    editorUsages = usageChoices($("aiKeyAddUsage"),key?.usages || [],"ai-business-usage");
    $("aiKeyEditProtocol").value = key?.balance_protocol || "auto";
    $("aiKeyEditJSON").checked = key?.json_mode ?? defaults.default_json_mode ?? true;
    $("aiKeyEditVision").checked = key?.vision ?? defaults.default_vision ?? false;
    renderReasoning(key?.reasoning_effort || "default");
    $("aiKeyAddSecret").required = !key;
    $("aiKeyAddSecret").placeholder = key ? "已保存；留空保持原 key" : "填写 API key";
    $("btnRevealAIKey").hidden = !key;
    $("aiKeyAddStatus").textContent = ""; $("aiKeyAddDialog").showModal();
    $("aiKeyAddForm").querySelector(".probe-editor-body").scrollTop = 0;
  }
  $("btnAddAIKey").addEventListener("click", () => openEditor());
  $("btnCancelAIKey").addEventListener("click", () => { if (!adding) $("aiKeyAddDialog").close(); });
  $("aiKeyAddDialog").addEventListener("cancel", event => { if (adding) event.preventDefault(); });
  $("aiKeyAddDialog").addEventListener("close", clearEditor);
  $("aiKeyAddProvider").addEventListener("change", () => {
    clearModels();
    const provider = $("aiKeyAddProvider").value;
    $("aiKeyAddURL").value = provider === "deepseek" ? "https://api.deepseek.com" : provider === "openrouter" ? "https://openrouter.ai/api/v1" : defaults.default_base_url || "";
    $("aiKeyAddModel").value = provider === "deepseek" ? "deepseek-flash" : provider === "configured" ? defaults.default_model || "" : "";
    renderReasoning("default");
  });
  $("btnRevealAIKey").addEventListener("click", async () => {
    if (!editing || adding) return;
    if ($("aiKeyAddSecret").type === "text") { $("aiKeyAddSecret").type = "password"; $("btnRevealAIKey").textContent = "查看 key"; return; }
    const token = editorEpoch, key = editing, owner = user()?.id;
    $("btnRevealAIKey").disabled = true;
    try {
      if (!$("aiKeyAddSecret").value) {
        const result = await api("/api/admin/ai-keys/" + encodeURIComponent(key.id) + "/reveal", {method:"POST",
          headers:{"Content-Type":"application/json"}, body:JSON.stringify({revision:key.revision})});
        if (token !== editorEpoch || owner !== user()?.id || !$("aiKeyAddDialog").open) return;
        revealedSavedKey = result.api_key;
        if (!$("aiKeyAddSecret").value) $("aiKeyAddSecret").value = result.api_key;
      }
      if (token === editorEpoch && owner === user()?.id) { $("aiKeyAddSecret").type = "text"; $("btnRevealAIKey").textContent = "隐藏 key"; }
    } catch (err) { if (token === editorEpoch && owner === user()?.id) $("aiKeyAddStatus").textContent = err.message; }
    finally { if (token === editorEpoch) $("btnRevealAIKey").disabled = false; }
  });
  $("aiKeyAddForm").addEventListener("submit", async event => {
    event.preventDefault();
    if (adding || user()?.role !== "platform_admin") return;
    renderReasoning();
    if ($("aiKeyReasoning").selectedOptions[0]?.disabled) { $("aiKeyAddStatus").textContent = "当前模型未确认支持此思考程度，请在更多设置中选择可用档位。"; return; }
    const owner = user().id, key = editing, token = editorEpoch;
    const selectedUsages = editorUsages();
    if (!selectedUsages.length && !$("aiKeyAddUsage").querySelector(".probe-only-detection input").checked) { $("aiKeyAddStatus").textContent="请勾选业务用途，或开启仅检测。"; return; }
    const body = {name:$("aiKeyAddName").value.trim(), api_key:$("aiKeyAddSecret").value.trim(),
      base_url:$("aiKeyAddURL").value.trim(), model:$("aiKeyAddModel").value.trim(),
      usages:selectedUsages, balance_protocol:$("aiKeyEditProtocol").value,
      json_mode:$("aiKeyEditJSON").checked, vision:$("aiKeyEditVision").checked,
      reasoning_effort:$("aiKeyReasoning").value};
    if (editing?.reasoning_legacy && currentCapability().state === "unknown" && body.reasoning_effort === editing.reasoning_effort) delete body.reasoning_effort;
    if (key) body.revision = key.revision;
    else body.provider = $("aiKeyAddProvider").value;
    clearModels(); adding = true; $("btnFetchAIModels").disabled = true; $("btnSaveAIKey").disabled = true; $("btnCancelAIKey").disabled = true;
    $("aiKeyAddSecret").value = ""; $("aiKeyAddStatus").textContent = "正在加密保存…";
    try {
      await api("/api/admin/ai-keys" + (key ? "/" + encodeURIComponent(key.id) : ""), {method:key ? "PUT" : "POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      if (user()?.id === owner && user()?.role === "platform_admin" && token === editorEpoch) { $("aiKeyAddDialog").close(); await load(); }
    } catch (err) { if (user()?.id === owner && token === editorEpoch) $("aiKeyAddStatus").textContent = "保存失败：" + err.message; }
    finally { adding = false; $("btnFetchAIModels").disabled = false; $("btnSaveAIKey").disabled = false; $("btnCancelAIKey").disabled = false; }
  });
  $("aiTemporaryProvider").addEventListener("change", () => {
    $("aiTemporaryModel").value = $("aiTemporaryProvider").value === "deepseek" ? "deepseek-flash" : "";
    $("aiTemporaryResult").replaceChildren();
  });
  for (const operation of ["health", "balance"]) {
    const button = $(operation === "health" ? "btnTemporaryHealth" : "btnTemporaryBalance");
    button.addEventListener("click", () => {
      const key = $("aiTemporaryKey").value.trim();
      if (!key) { $("aiTemporaryResult").replaceChildren(el("p", "probe-failed", "请先填写临时密钥。")); return; }
      const body = {operation, api_key:key, provider:$("aiTemporaryProvider").value,
        model:$("aiTemporaryModel").value.trim(), balance_protocol:$("aiTemporaryProtocol").value};
      $("aiTemporaryKey").value = ""; probe(body, $("aiTemporaryResult"), button, operation);
    });
  }
  return {load, reset};
}
