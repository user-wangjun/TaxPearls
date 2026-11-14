"use strict";
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");

function element(tag = "div", className = "", text = "") {
  const node = {tag, children:[], classes:new Set((className || "").split(" ")), events:{}, value:"", disabled:false,
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; text = ""; },
    setAttribute() {}, removeAttribute() {}, reset() {}, close() {}, focus() {},
    reportValidity() { return !!this.value; },
    addEventListener(name, fn) { this.events[name] = fn; },
    querySelectorAll(selector) {
      const nested = descendants(this).slice(1);
      if (selector === "input" || selector === "input:checked") return nested.filter(item => item.tag === "input" && (selector === "input" || item.checked));
      if (selector.startsWith(".")) {
        const [className, child] = selector.slice(1).split(" ");
        const matches = nested.filter(item => item.classes.has(className));
        return child ? matches.flatMap(item => item.querySelectorAll(child)) : matches;
      }
      return [];
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    get textContent() { return text + this.children.map(item => item.textContent).join(""); },
    set textContent(value) { text = value; this.children = []; }};
  node.classList = {add(...values) { values.forEach(value => node.classes.add(value)); },
    remove(...values) { values.forEach(value => node.classes.delete(value)); }};
  return node;
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
const tick = () => new Promise(resolve => setImmediate(resolve));
const saved = {operation:"balance",status:"ok",checked_at:"2026-10-12T15:14:37Z",
  balances:[{currency:"CNY",total:"14.89"}],message:"账户余额"};
const fresh = {...saved,checked_at:"2026-10-17T15:19:37Z",balances:[{currency:"CNY",total:"11.71"}]};

function harness() {
  const nodes = new Map(), pending = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const context = {URL, Date, console};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("webapp/static/ai-keys.js", "utf8"), context);
  const controller = context.createAIKeyProbes({el:element, $, user:()=>({id:"admin",role:"platform_admin"}),
    api:(url, options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))});
  const catalog = {enabled:true,keys:[{id:"key-one",name:"wang",base_url:"https://api.deepseek.com",
    model:"deepseek-flash",masked_key:"••••",usages:[],health:null,balance:saved}]};
  return {controller,$,pending,catalog,
    brief:()=>descendants($("aiKeyCards")).find(node=>node.classes.has("probe-balance-brief")),
    money:()=>descendants($("aiKeyCards")).find(node=>node.tag==="button" && node.textContent==="查询金额")};
}

(async () => {
  const custom = harness();
  custom.$("aiKeyAddProvider").value = "custom";
  custom.$("aiKeyAddProvider").events.change();
  assert.equal(custom.$("aiKeyAddURL").value, "");
  assert.equal(custom.$("aiKeyAddModel").value, "");
  custom.$("aiKeyAddURL").value = "https://custom.example/v1";
  custom.$("aiKeyAddURL").events.input();
  assert.equal(custom.$("aiKeyAddProvider").value, "custom");
  custom.$("aiTemporaryProvider").value = "custom";
  custom.$("aiTemporaryProvider").events.change();
  assert.equal(custom.$("aiTemporaryURLField").hidden, false);
  assert.equal(custom.$("aiTemporaryURL").required, true);
  custom.$("aiTemporaryKey").value = "synthetic-temporary-key";
  custom.$("btnTemporaryHealth").events.click();
  assert.equal(custom.pending.length, 0, "empty URL must not submit or clear the key");
  assert.equal(custom.$("aiTemporaryKey").value, "synthetic-temporary-key");
  custom.$("aiTemporaryURL").value = "https://custom.example/v1";
  custom.$("btnTemporaryHealth").events.click();
  assert.equal(custom.pending.length, 0, "custom health needs an explicit model");
  custom.$("aiTemporaryModel").value = "custom-model";
  custom.$("btnTemporaryHealth").events.click();
  assert.equal(custom.$("aiTemporaryKey").value, "");
  assert.deepEqual(JSON.parse(custom.pending.shift().options.body), {operation:"health",provider:"custom",
    api_key:"synthetic-temporary-key",model:"custom-model",base_url:"https://custom.example/v1",balance_protocol:""});
  custom.$("aiTemporaryProvider").value = "deepseek";
  custom.$("aiTemporaryProvider").events.change();
  assert.equal(custom.$("aiTemporaryURLField").hidden, true);
  assert.equal(custom.$("aiTemporaryURL").disabled, true);
  custom.$("aiTemporaryKey").value = "synthetic-official-key";
  custom.$("btnTemporaryBalance").events.click();
  assert.equal(Object.hasOwn(JSON.parse(custom.pending.shift().options.body),"base_url"),false);
  custom.controller.reset();
  assert.equal(custom.$("aiTemporaryURL").value, "");
  assert.equal(custom.$("aiTemporaryURLField").hidden, true);

  const h = harness();
  const opening = h.controller.load();
  h.pending.shift().resolve(h.catalog); await tick();
  assert.equal(h.pending.length, 1, "opening must query the provider: " + h.$("aiKeyStatus").textContent);
  assert.equal(JSON.parse(h.pending[0].options.body).operation, "balance");
  assert.ok(!h.brief().textContent.includes("14.89"), "saved balance must never appear as current");
  assert.match(h.brief().textContent, /查询中/);
  h.pending.shift().resolve(fresh); await opening;
  assert.match(h.brief().textContent, /11.71/);

  const fields = h.$("aiKeyCards").querySelector(".probe-usage-choices");
  const boxes = fields.querySelectorAll("input");
  const detection = h.$("aiKeyCards").querySelector(".probe-only-detection input");
  assert.equal(detection.checked, true);
  assert.ok(boxes.every(box => !box.disabled), "detection-only keys must still allow selecting business uses");
  boxes[0].checked = true; fields.events.change();
  assert.equal(detection.checked, false, "selecting a use must turn off detection-only");
  boxes[1].checked = true; fields.events.change();
  const saveUses = descendants(h.$("aiKeyCards")).find(node => node.tag === "button" && node.textContent === "保存用途");
  const savingUses = saveUses.events.click();
  assert.equal(h.pending[0].options.method, "PUT");
  assert.deepEqual(JSON.parse(h.pending[0].options.body).usages, ["extraction", "knowledge"]);
  h.pending.shift().reject(new Error("synthetic save failure")); await savingUses;
  assert.equal(saveUses.disabled, false, "failed saves must allow retry");
  boxes[0].checked = false; fields.events.change();
  assert.equal(detection.checked, false, "remaining uses must keep detection-only off");
  boxes[1].checked = false; fields.events.change();
  assert.equal(detection.checked, true, "clearing the last use restores detection-only");
  boxes[2].checked = true; fields.events.change();
  detection.checked = true; detection.events.change();
  assert.ok(boxes.every(box => !box.checked && !box.disabled), "detection-only clears uses without disabling them");
  const savingDetection = saveUses.events.click();
  assert.deepEqual(JSON.parse(h.pending[0].options.body).usages, []);
  h.pending.shift().reject(new Error("synthetic save failure")); await savingDetection;
  boxes[3].checked = true; fields.events.change();
  descendants(h.$("aiKeyCards")).find(node => node.tag === "button" && node.textContent === "取消").events.click();
  assert.equal(h.$("aiKeyCards").querySelector(".probe-only-detection input").checked, true, "cancel restores saved uses");
  console.log("AI key usages: direct multi-select, detection-only, payloads, retry and cancel passed.");

  const refreshing = h.$("btnRefreshAIKeys").events.click();
  h.pending.shift().resolve(h.catalog); await tick();
  assert.ok(!h.brief().textContent.includes("14.89"));
  h.pending.shift().resolve({operation:"balance",status:"network",message:"网络或 TLS 连接失败。",checked_at:fresh.checked_at});
  await refreshing;
  assert.match(h.brief().textContent, /查询失败/);
  assert.match(h.brief().textContent, /网络或 TLS/);
  assert.ok(!h.brief().textContent.includes("11.71") && !h.brief().textContent.includes("14.89"));
  assert.equal(h.money().disabled, false, "failed automatic queries must remain retryable");

  const retry = h.money().events.click();
  h.pending.shift().resolve(fresh); await retry;
  assert.match(h.brief().textContent, /11.71/);
  const failed = h.money().events.click();
  assert.ok(!h.brief().textContent.includes("11.71"));
  h.pending.shift().reject(new Error("检测过于频繁，请一分钟后重试。")); await failed;
  assert.match(h.brief().textContent, /查询失败/);
  assert.match(h.brief().textContent, /一分钟后重试/);
  assert.ok(!h.brief().textContent.includes("11.71"));

  // A newer list read wins even if the previous balance response arrives later.
  const oldLoad = h.controller.load(); h.pending.shift().resolve(h.catalog); await tick();
  const oldProbe = h.pending.shift();
  const newLoad = h.controller.load(); h.pending.shift().resolve(h.catalog); await tick();
  h.pending.shift().resolve(fresh); await newLoad;
  oldProbe.resolve({...fresh,balances:[{currency:"CNY",total:"999"}]}); await oldLoad;
  assert.match(h.brief().textContent, /11.71/);
  assert.ok(!h.brief().textContent.includes("999"));

  const leaving = h.controller.load(); h.pending.shift().resolve(h.catalog); await tick();
  const outstanding = h.pending.shift(); h.controller.reset();
  outstanding.resolve(fresh); await leaving;
  assert.equal(h.$("aiKeyCards").children.length,0, "logout must not restore balances");
  console.log("AI key balances: opening, refresh, failures, retry and stale-response protection passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });



