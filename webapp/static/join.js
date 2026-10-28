"use strict";
(() => {
  const $=id=>document.getElementById(id);let invitation=null,existing=false,busy=false,sending=false,captchaId="",cooldown=0,emailProof=null;
  const code=decodeURIComponent(location.pathname.split("/").pop()||"");
  // Consume the handoff once; keep the short-lived proof only in this page's memory.
  try {
    const saved=sessionStorage.getItem("taxpearls:invitation-proof");
    sessionStorage.removeItem("taxpearls:invitation-proof");
    if(saved){
      const proof=JSON.parse(saved);
      if(proof.path===location.pathname&&typeof proof.proof==="string"&&typeof proof.email==="string"&&Date.parse(proof.expires_at)>Date.now())emailProof=proof;
    }
  } catch { /* Normal email-code registration remains available. */ }
  async function request(path,body){const response=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{cache:"no-store"});const data=await response.json();if(!response.ok)throw new Error(data.detail||"请求失败，请稍后重试。");return data;}
  function fail(error){$("join-error").textContent=error.message||String(error);}
  async function captcha(){const data=await request("/api/auth/captcha");captchaId=data.captcha_id;$("join-captcha-image").src=data.image;$("join-captcha").value="";}
  function view(value){existing=value;if(emailProof&&!value)$("join-email").value=emailProof.email;$("join-email").readOnly=!!emailProof&&!value;$("join-verification-fields").hidden=!!emailProof;$("join-new-fields").hidden=value;$("join-confirm-box").hidden=value;$("join-password-rule").hidden=value;$("join-password-label").textContent=value?"账号密码":"设置密码";$("join-password").autocomplete=value?"current-password":"new-password";$("join-new").setAttribute("aria-pressed",String(!value));$("join-existing").setAttribute("aria-pressed",String(value));$("join-accept").checked=false;$("join-error").textContent="";}
  $("join-new").addEventListener("click",()=>view(false));$("join-existing").addEventListener("click",()=>view(true));
  $("join-captcha-refresh").addEventListener("click",()=>captcha().catch(fail));
  $("join-send").addEventListener("click",async()=>{if(sending||cooldown)return;sending=true;$("join-send").disabled=true;$("join-error").textContent="";try{if(!$("join-email").reportValidity())return;const result=await request("/api/auth/email/start",{email:$("join-email").value.trim(),purpose:"register",invite_code:code,captcha_id:captchaId,captcha_answer:$("join-captcha").value.trim()});if(result.exists){view(true);$("join-success").textContent=result.message;return;}$("join-success").textContent="验证码已发送，10 分钟内有效。";cooldown=60;const timer=setInterval(()=>{cooldown--;$("join-send").textContent=cooldown?`重新发送（${cooldown}秒）`:"获取验证码";$("join-send").disabled=!!cooldown;if(!cooldown)clearInterval(timer);},1000);}catch(err){fail(err);await captcha().catch(()=>{});}finally{sending=false;$("join-send").disabled=!!cooldown;}});
  $("join-form").addEventListener("submit",async event=>{event.preventDefault();if(busy||!invitation)return;$("join-error").textContent="";if(!$("join-accept").checked){fail("请确认加入所显示的机构及角色。");return;}if(!existing){if(!$("join-name").value.trim()){fail("请填写姓名。");return;}if(invitation.role==="student"&&!$("join-no").value.trim()){fail("请填写学号。");return;}if($("join-password").value!==$("join-confirm").value){fail("两次输入的密码不一致。");return;}if(!emailProof&&!$("join-code").value.trim()){fail("请填写邮箱验证码。");return;}}busy=true;$("join-submit").disabled=true;try{const result=await request("/api/join",{invite_code:code,email:$("join-email").value.trim(),password:$("join-password").value,code:$("join-code").value.trim(),email_proof:existing?"":emailProof?.proof||"",display_name:$("join-name").value.trim(),student_no:$("join-no").value.trim(),existing,accepted:true});$("join-password").value="";$("join-confirm").value="";location.assign(result.destination);}catch(err){fail(err);}finally{busy=false;$("join-submit").disabled=false;}});
  (async()=>{try{invitation=await request("/api/join/"+encodeURIComponent(code));const role={accountant:"员工（会计）",teacher:"教师",student:"学生"}[invitation.role];$("join-title").textContent="你收到了一份"+role+"邀请";$("join-context").textContent=invitation.organization+(invitation.teacher?" · 邀请教师："+invitation.teacher:"");$("join-consent-text").textContent="确认以"+role+"身份加入「"+invitation.organization+"」"+(invitation.teacher?"，归属"+invitation.teacher+"名下":"");$("join-student-fields").hidden=invitation.role!=="student";$("join-form").hidden=false;
      if(emailProof){
        $("join-email").value=emailProof.email;
        view(false);
        $("join-success").textContent="邮箱已验证。请填写姓名"+(invitation.role==="student"?"和学号":"")+"、设置密码，并确认加入所显示的机构及角色。刷新或关闭此页后需重新申请验证。";
      }else await captcha();}catch(err){$("join-title").textContent=invitation?"接受邀请":"邀请暂不可用";fail(err);}})();
})();
