"use strict";
// Authentication stays independent of workspace controllers and WebGL.
function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined && text !== null && text !== "") n.textContent = text;
  return n;
}

const $ = (id) => document.getElementById(id);
let currentUser = null;
async function api(url, options = {}) {
  const {onEvent,...request}=options;
  let r;
  try {
    r = await fetch(url, request);
  } catch (e) {
    throw new Error(location.protocol === "file:"
      ? "当前是直接打开的本地文件，请求不到后端接口。请通过服务地址访问，例如 http://127.0.0.1:8000 。"
      : "无法连接服务器，请确认服务已启动后重试。");
  }
  const ct = r.headers.get("content-type") || "";
  if(r.ok&&onEvent){
    if(!ct.includes("application/x-ndjson"))throw new Error("检查进度响应无效，请重新读取任务。");
    return readCoreCheckStream(r,onEvent);
  }
  const body = ct.includes("application/json") ? await r.json().catch(() => null) : await r.text();
  if (!r.ok) {
    const detail = body?.detail;
    const message = Array.isArray(detail) ? detail.map(e=>e.msg||"字段校验失败").join("；") : detail;
    const error = new Error(message || (typeof body === "string" && body ? body : "") || `请求失败（${r.status}）`); error.status=r.status; error.requiresCollege=body?.requires_college===true; throw error;
  }
  return body;
}

const TOAST_PEEK = 16;
const TOAST_MAX = 5;
function placeToast(el, i, count, lift) {
  // 队头（最新）i=0 在最上方、完全可见；i 越大越旧、越靠下，只露出下缘
  const y = i * TOAST_PEEK + (lift || 0);
  const scale = 1 - Math.min(i, 4) * 0.04;
  el.style.transform = `translateY(${y}px) scale(${scale})`;
  el.style.zIndex = String(100 - i);
  el._y = y;
  el._scale = scale;
}
function relayoutToasts() {
  const live = Array.from(document.getElementById("toastStack").children).filter((t) => !t._closing);
  live.forEach((t, i) => placeToast(t, i, live.length));
}
function showToast(msg, type = "error", stayMs) {
  const stack = document.getElementById("toastStack");
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.setAttribute("role", type === "error" ? "alert" : "status");
  const span = document.createElement("span");
  span.className = "msg";
  span.textContent = msg;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "x";
  close.setAttribute("aria-label", "关闭");
  close.textContent = "×";
  el.append(span, close);
  stack.prepend(el);
  const liveCount = () => Array.from(stack.children).filter((t) => !t._closing).length;
  placeToast(el, 0, 1, -22);  // 起始：悬在栈顶上方，落入后旧卡下移让位
  el.style.opacity = "0";
  void el.offsetWidth;
  el.style.opacity = "1";
  placeToast(el, 0, 1);
  relayoutToasts();
  let timer = null;
  const dismiss = () => {
    if (el._closing) return;
    el._closing = true;
    if (timer) { clearTimeout(timer); timer = null; }
    el.classList.add("closing");
    el.style.transform = `translateY(${el._y - 40}px) scale(${el._scale})`;
    el.style.opacity = "0";
    relayoutToasts();
    setTimeout(() => el.remove(), 700);
  };
  close.addEventListener("click", dismiss);
  el._dismiss = dismiss;
  const delay = stayMs || (type === "error" ? 6000 : 3600);
  timer = setTimeout(dismiss, delay);
  const live = Array.from(stack.children).filter((t) => !t._closing);
  if (live.length > TOAST_MAX) live.slice(TOAST_MAX).forEach((t) => t._dismiss && t._dismiss());
}
function showError(msg) { showToast(msg, "error"); }
function hideError() { document.querySelectorAll("#toastStack .toast.error").forEach((t) => t._dismiss && t._dismiss()); }
// Public scripts are loaded once. Failed downloads may be retried without duplicate handlers.
const clientScriptLoads = new Map();
function loadClientScript(src) {
  if (clientScriptLoads.has(src)) return clientScriptLoads.get(src);
  const promise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = () => resolve();
    script.onerror = () => { clientScriptLoads.delete(src); script.remove(); reject(new Error("页面组件加载失败，请重试。")); };
    document.head.append(script);
  });
  clientScriptLoads.set(src, promise);
  return promise;
}
let workspaceScripts = null;
async function enterApp(user) {
  const form = $("authForm");
  const version = authViewVersion;
  form.dataset.entering = "1";
  $("authSubmit").disabled = true;
  try {
    if (!workspaceScripts) {
      workspaceScripts = Promise.all([
        loadClientScript("/university-workspace.js?v=20261027-login-first-1"),
        loadClientScript("/console.js?v=20261027-login-first-1")
      ]).then(() => loadClientScript("/workspace.js?v=mobile-navigation-20261026-2"))
        .catch(error => { workspaceScripts = null; throw error; });
    }
    await workspaceScripts;
    if (version !== authViewVersion) { delete form.dataset.entering; return; }
    enterWorkspace(user);
  } catch (error) {
    delete form.dataset.entering;
    if (form.dataset.ready !== "1") showAuthView("login");
    throw error;
  }
}
async function startAuthentication() {
  $("authSubmit").disabled = true;
  try { await bootstrap(); }
  catch (error) {
    if ($("authForm").dataset.ready !== "1") $("authSubmit").textContent = "重新连接";
    showToast(error.message);
  } finally {
    if (!$("authForm").dataset.entering) $("authSubmit").disabled = false;
  }
}
function updateAuthCollege() {
  const setup = $("authForm").dataset.setup === "1";
  const login = $("authForm").dataset.view === "login" && !setup;
  const needsCollege = login && $("loginCollege").dataset.needed === "1";
  $("loginCollegeField").hidden = !needsCollege;
  $("loginCollege").required = needsCollege;
  $("loginCollege").disabled = !needsCollege;
  if (login) {
    $("loginUserLabel").textContent = "邮箱、用户名或学号";
    $("loginUser").placeholder = "请输入邮箱、用户名或学号";
    $("loginUser").inputMode = "text";
  }
}
$("loginUser").addEventListener("input", () => {
  delete $("loginCollege").dataset.needed;
  $("loginCollege").value = "";
  updateAuthCollege();
});
// Purge credentials saved by earlier versions; passwords are never stored in browser storage.
try { localStorage.removeItem("taxpearls.rememberedLogin.v1"); } catch {}
$("pwToggle").addEventListener("click", () => {
  const input = $("loginPass");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  document.querySelector("#pwToggle .pw-eye").style.display = show ? "none" : "";
  document.querySelector("#pwToggle .pw-eye-off").style.display = show ? "" : "none";
  $("pwToggle").setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  $("pwToggle").setAttribute("aria-pressed", String(show));
});

let pendingEmailToken="",emailRegistrationProof=null,authResetProof="",authViewVersion=0,authSubmitBusy=false;
// Opening a mail link in the existing tab can change only the fragment.
// Reload to show explicit confirmation; GET still never redeems the token.
window.addEventListener("hashchange", () => {
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get("email")) { location.reload(); return; }
  if (currentUser?.workspace === "university") return;
  if (currentUser && params.get("panel")) switchPanel(params.get("panel"));
});
async function bootstrap() {
  const params = new URLSearchParams(location.search);
  pendingEmailToken = new URLSearchParams(location.hash.slice(1)).get("email") || params.get("reset") || "";
  let pathInvite = "";
  if(location.pathname.startsWith("/p/")){
    try{pathInvite=decodeURIComponent(location.pathname.slice(3));}catch{history.replaceState(null,"","/");}
  }
  const inviteCode = (pathInvite || params.get("invite") || "").trim();
  if (pendingEmailToken) history.replaceState(null, "", "/");
  if (inviteCode) {
    history.replaceState(null, "", "/");
    $("signupCode").value = inviteCode;
    $("authForm").dataset.inviteCode = inviteCode;
    $("authForm").dataset.inviteFromUrl = "1";
  }
  const s = await api("/api/status");
  const setup = s.needs_setup;
  $("authForm").dataset.setup = setup ? "1" : "0";
  $("loginPass").autocomplete = setup ? "new-password" : "current-password";
  if (pendingEmailToken && !setup) return showAuthView("emailMagic");
  const signedUser=s.user;
  if(signedUser)return enterApp(signedUser);
  if (inviteCode && !setup) return showAuthView("signup");
  showAuthView("login");
  const notice = params.get("notice");
  if (notice) showToast(notice);
  if (s.session_conflict) showToast("检测到多个登录状态，请重新登录以确认当前账号。");
}

const authViews = ["viewLogin", "viewForgot", "viewReset", "viewSignup", "viewEmailLogin", "viewEmailMagic"];
const authViewMap = {
  login: "viewLogin", forgot: "viewForgot", reset: "viewReset", signup: "viewSignup",
  emailLogin:"viewEmailLogin",emailMagic:"viewEmailMagic",
};
const authViewLabels = {
  login: ["欢迎回来", "登录", ""],
  emailLogin:["邮箱登录","验证并登录","在当前浏览器申请并使用验证码或邮件链接。"],
  emailMagic:["确认邮件验证","确认本人操作并继续","不会在打开链接时自动登录或修改密码。"],
  forgot: ["找回密码", "发送重置邮件", ""],
  reset: ["设置新密码", "设置新密码", ""],
  signup: ["受邀入驻", "创建账号", "使用平台或机构管理员提供的邀请码完成注册。"],
};
function updateSignupPasswordValidation() {
  const password = $("signupPass"), confirmation = $("signupPass2");
  const hint = $("signupPasswordHint"), match = $("signupPasswordMatch");
  const active = $("authForm").dataset.view === "signup";
  const value = password.value, canonical = value.normalize("NFKC");
  const length = [...value].length;
  const classes = [/\p{Lowercase}/u, /\p{Uppercase}/u, /\p{Decimal_Number}/u, /[^\p{L}\p{N}\p{White_Space}\u001c-\u001f]/u]
    .filter(pattern => pattern.test(canonical)).length;
  const error = !active || !value ? "" : length < 10 || length > 128
    ? "密码长度须为 10–128 位。"
    : classes < 3 ? "大小写字母、数字、符号至少包含三类（空白不算符号）。" : "";
  const mismatch = active && confirmation.value && confirmation.value !== value
    ? "两次输入的密码不一致。" : "";
  password.setCustomValidity(error);
  confirmation.setCustomValidity(mismatch);
  password.setAttribute("aria-invalid", String(!!error));
  confirmation.setAttribute("aria-invalid", String(!!mismatch));
  hint.textContent = error || (active && value ? "✓ 长度和字符类型符合要求。" : "10–128 位，大小写字母、数字、符号至少包含三类（空白不算符号）。");
  hint.dataset.state = error ? "error" : active && value ? "valid" : "";
  match.textContent = mismatch || (active && confirmation.value ? "✓ 两次输入的密码一致。" : "");
  match.dataset.state = mismatch ? "error" : active && confirmation.value ? "valid" : "";
}
function showAuthView(view) {
  authViewVersion++;
  const form = $("authForm");
  form.dataset.ready="1";
  const setup = form.dataset.setup === "1";
  const [title, submitText, step] = authViewLabels[view] || authViewLabels.login;
  $("loginUserLabel").textContent = setup ? "用户名" : "邮箱或账号";
  $("loginUser").placeholder = setup ? "设置管理员用户名" : "请输入邮箱、用户名或学号";
  $("loginUser").inputMode = "text";
  $("loginRememberField").hidden = setup || view !== "login";
  $("loginRemember").disabled = setup || view !== "login";
  (view==="emailLogin"?$("emailLoginVerification"):$("viewSignup")).append($("emailVerificationFields"));
  authViews.forEach((id) => {
    const on = id === authViewMap[view];
    $(id).style.display = on ? "" : "none";
    $(id).querySelectorAll("input").forEach((i) => { i.required = on; });
  });
  $("forgotLink").style.display = ["login","emailLogin"].includes(view)&&!setup ? "" : "none";
  $("signupEntryLink").style.display = ["login","emailLogin"].includes(view)&&!setup ? "" : "none";
  $("emailLoginEntryLink").style.display = view==="login"&&!setup?"":"none";
  $("passwordLoginEntryLink").style.display = view==="emailLogin"&&!setup?"":"none";
  $("backToLogin").style.display = ["login","emailLogin"].includes(view) ? "none" : "";
  $("authStep").textContent = setup ? "" : step;
  $("authTitle").textContent = setup ? "初始化平台管理员" : title;
  $("authDescription").textContent = setup ? "创建首位管理员，开启税海旅程。" : ({login:"连接每一份证据，拾取每一条线索。", signup:"从一份邀请，开启你的税海旅程。", emailLogin:"使用验证码或邮件链接，安全回到税海。", forgot:"验证绑定邮箱，找回你的账号。", reset:"设置新密码，保护账号与证据。", emailMagic:"确认本人发起的邮件验证。"}[view] || "");
  const linkRow = view === "login" ? document.querySelector(".auth-remember-row") : document.querySelector(".auth-secondary-row");
  if ($("forgotLink").parentElement !== linkRow) linkRow.append($("forgotLink"));
  $("authSubmit").textContent = setup ? "创建管理员" : submitText;
  $("authSubmit").disabled=authSubmitBusy;
  form.dataset.view = view;
  // Captcha gates email delivery, not final registration (which proves the email).
  $("captchaAnswer").required = false;
  const verified=view==="signup"&&!!emailRegistrationProof;
  $("emailVerificationFields").hidden=verified;$("signupEmailCode").required=!verified&&["signup","emailLogin"].includes(view);
  $("signupProofStatus").hidden=!verified;$("signupEmail").readOnly=verified;
  $("signupInviteField").hidden=verified&&emailRegistrationProof.has_invite;
  $("signupCode").required=view==="signup"&&!$("signupInviteField").hidden;
  if(verified){
    $("signupEmail").value=emailRegistrationProof.email;
    $("signupProofStatus").textContent=emailRegistrationProof.has_invite
      ?"邮箱已验证，将使用发起请求时绑定的邀请完成注册。请设置密码；刷新或关闭此页后需重新申请验证。"
      :"邮箱已验证，请填写邀请码并设置密码。刷新或关闭此页后需重新申请验证。";
    if(emailRegistrationProof.has_invite)$("signupCode").value="";
  }
  updateSignupPasswordValidation();
  if(view==="emailLogin"||(view==="signup"&&!verified))loadCaptcha();
  updateAuthCollege();
}
for (const id of ["signupPass", "signupPass2"]) {
  $(id).addEventListener("input", updateSignupPasswordValidation);
  $(id).addEventListener("change", updateSignupPasswordValidation);
}
$("forgotLink").addEventListener("click", () => showAuthView("forgot"));
$("signupEntryLink").addEventListener("click", () => {emailRegistrationProof=null;showAuthView("signup");});
$("emailLoginEntryLink").addEventListener("click",()=>showAuthView("emailLogin"));
$("passwordLoginEntryLink").addEventListener("click",()=>showAuthView("login"));
$("backToLogin").addEventListener("click", () => {emailRegistrationProof=null;authResetProof="";pendingEmailToken="";showAuthView("login");});

let captchaBusy = false;
async function loadCaptcha() {
  if (captchaBusy) return;
  captchaBusy = true;
  updateSignupSendButton();
  const img = $("captchaImage");
  img.style.opacity = ".45";
  try {
    const result = await api("/api/auth/captcha");
    $("authForm").dataset.captchaId = result.captcha_id;
    img.src = result.image;
    img.alt = "人机验证码，看不清可点击刷新";
    $("captchaAnswer").value = "";
    $("captchaAnswer").disabled = false;
  } catch (err) {
    $("authForm").dataset.captchaId = "";
    img.removeAttribute("src");
    img.alt = "验证码加载失败，点击刷新 ↻";
    $("captchaAnswer").disabled = true;
  } finally { captchaBusy = false; img.style.opacity = "1"; updateSignupSendButton(); }
}
$("captchaImage").addEventListener("click", loadCaptcha);
$("captchaRefresh").addEventListener("click", loadCaptcha);

let sendCooldown = 0,signupSendBusy=false;
function emailAddressInput(){return $("authForm").dataset.view==="emailLogin"?$("emailLoginAddress"):$("signupEmail");}
function updateSignupSendButton(){
  const emailInput=emailAddressInput();
  $("signupSendBtn").disabled=signupSendBusy||captchaBusy||sendCooldown>0||!$("authForm").dataset.captchaId||!$("captchaAnswer").value.trim()||!emailInput.value.trim()||!emailInput.validity.valid;
}
$("signupEmail").addEventListener("input",updateSignupSendButton);
$("emailLoginAddress").addEventListener("input",updateSignupSendButton);
$("captchaAnswer").addEventListener("input",updateSignupSendButton);
function startCooldown(seconds) {
  sendCooldown = seconds;
  const btn = $("signupSendBtn");
  btn.disabled = true;
  const tick = () => {
    if (sendCooldown <= 0) { btn.textContent = "获取验证码"; updateSignupSendButton(); return; }
    btn.textContent = `重新发送(${sendCooldown}s)`;
    sendCooldown -= 1;
    setTimeout(tick, 1000);
  };
  tick();
}

$("signupSendBtn").addEventListener("click", async () => {
  if(signupSendBusy||sendCooldown>0)return;
  const form = $("authForm");
  const email = emailAddressInput().value.trim(),version=authViewVersion;
  const purpose=form.dataset.view==="emailLogin"?"login":"register";
  const captchaId = form.dataset.captchaId || "";
  const captchaAnswer = $("captchaAnswer").value.trim();
  if (!email) { showToast("请先填写邮箱。"); return; }
  if (!captchaId || !captchaAnswer) { showToast("请先完成人机验证。"); return; }
  signupSendBusy=true;updateSignupSendButton();
  try {
    const result = await api("/api/auth/email/start", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({email, captcha_id:captchaId, captcha_answer:captchaAnswer,purpose,invite_code:purpose==="register"?$("signupCode").value.trim():""})});
    if(version!==authViewVersion||email!==emailAddressInput().value.trim())return;
    if (result.exists) {
      $("signupEmailHint").textContent = "该邮箱已注册，请直接登录；忘记密码可用登录页的「忘记密码？」找回。";
      return;
    }
    $("signupEmailHint").textContent = purpose === "register" ? "验证码已发送，10 分钟内有效。" : "验证申请已提交，请查收邮箱。";
    showToast(purpose==="register"?"验证邮件已发送，请查收邮箱。":"验证申请已提交，请查收邮箱。", "success", 6000);
    startCooldown(60);
    loadCaptcha();
  } catch (err) {
    if(version===authViewVersion){showToast(err.message);loadCaptcha();}
  } finally {signupSendBusy=false;updateSignupSendButton();}
});

$("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if(authSubmitBusy)return;
  if($("authForm").dataset.ready!=="1"){startAuthentication();return;}
  const form = e.currentTarget;
  const view = form.dataset.view || "login";
  updateSignupPasswordValidation();
  if (!form.reportValidity()) return;
  const version=authViewVersion;
  const submitLabel=$("authSubmit").textContent;
  authSubmitBusy=true;$("authForm").dataset.busy="1";$("authSubmit").disabled=true;
  try {
    if(view==="emailMagic"){
      const result=await api("/api/auth/email/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:pendingEmailToken})});
      if(version!==authViewVersion)return;
      pendingEmailToken="";
      if(result.purpose==="login"){await enterApp(result.user);return;}
      if(result.purpose==="reset"){authResetProof=result.proof;showAuthView("reset");return;}
      if(result.join_path){
        if(!/^\/p\/[A-Z0-9]{1,128}$/.test(result.join_path))throw new Error("邀请链接无效，请重新打开邀请页。");
        try{sessionStorage.setItem("taxpearls:invitation-proof",JSON.stringify({...result,path:result.join_path}));}
        catch{throw new Error("无法打开邀请注册页，请返回邀请页填写邮箱验证码。");}
        location.assign(result.join_path);return;
      }
      emailRegistrationProof=result;showAuthView("signup");return;
    }
    if(view==="emailLogin"){
      const result=await api("/api/auth/email/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("emailLoginAddress").value.trim(),code:$("signupEmailCode").value.trim()})});
      if(version===authViewVersion)await enterApp(result.user);return;
    }
    if (view === "forgot") {
      $("authSubmit").textContent="正在提交…";
      const resetEmail=$("resetEmail").value.trim();
      await api("/api/auth/password/reset", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({email:resetEmail})});
      if(version===authViewVersion&&resetEmail===$("resetEmail").value.trim())showToast("重置申请已提交，请查收邮箱。", "success", 6000);
      return;
    }
    if (view === "reset") {
      if ($("newPass").value !== $("newPass2").value) throw new Error("两次输入的新密码不一致。");
      await api("/api/auth/password/reset/confirm", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({token:authResetProof, password:$("newPass").value})});
      if(version!==authViewVersion)return;
      history.replaceState(null, "", location.pathname);
      authResetProof="";$("newPass").value="";$("newPass2").value="";
      showAuthView("login");
      showToast("密码已重置，请使用新密码登录。", "success");
      return;
    }
    if (view === "signup") {
      if ($("signupPass").value !== $("signupPass2").value) throw new Error("两次输入的密码不一致。");
      if (!emailRegistrationProof&&!$("signupEmailCode").value.trim()) throw new Error("请填写邮箱验证码。");
      await api("/api/register/complete", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({email:$("signupEmail").value.trim(), code:$("signupEmailCode").value.trim(),
                             invite_code:$("signupCode").value.trim(), password:$("signupPass").value,email_proof:emailRegistrationProof?.proof||""})});
      const user = await api("/api/me");
      if(version===authViewVersion)await enterApp(user);
      return;
    }
    const setup = form.dataset.setup === "1";
    const body = {username: $("loginUser").value.trim(), password: $("loginPass").value, remember: !setup && $("loginRemember").checked};
    if (!setup && $("loginCollege").dataset.needed === "1") body.college = $("loginCollege").value.trim();
    if (setup) body.display_name = body.username;
    let loginResult = await api(setup ? "/api/setup" : "/api/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    if (setup) { form.dataset.setup = "0"; loginResult = await api("/api/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username:body.username,password:body.password})}); }
    const user = loginResult.user;
    $("loginPass").value = "";
    if(version===authViewVersion)await enterApp(user);
  } catch (err) {
    if (version === authViewVersion) {
      if (err.requiresCollege) { $("loginCollege").dataset.needed = "1"; updateAuthCollege(); $("loginCollege").focus(); }
      showToast(err.message);
    }
  }
  finally{authSubmitBusy=false;delete $("authForm").dataset.busy;$("authSubmit").disabled=false;if(version===authViewVersion)$("authSubmit").textContent=submitLabel;}
});


startAuthentication();
