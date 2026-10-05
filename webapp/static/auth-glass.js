/* The real authentication flow lives in console.js. This file owns only
 * glass interactions and the lazily mounted, local WebGL background. */
(() => {
  const screen = document.getElementById('authScreen');
  const form = document.getElementById('authForm');
  const canvas = document.getElementById('sceneCanvas');
  const status = document.getElementById('oceanStatus');
  let ocean, loading = false, failed = false, pending = false, lastView = '';
  const visible = () => screen.style.display !== 'none' && !document.hidden;
  function unavailable() {
    screen.classList.remove('liquid-live');
    canvas.style.visibility = 'hidden';
    status.textContent = '海景渲染暂不可用，您仍可正常登录或注册。';
    status.hidden = false;
  }
  function restored() {
    screen.classList.add('liquid-live');
    canvas.style.visibility = '';
    status.hidden = true;
    update();
  }
  async function update() {
    const view = form.dataset.view;
    if (visible() && view !== lastView) {
      lastView = view;
      const viewId = {login:'viewLogin', signup:'viewSignup', emailLogin:'viewEmailLogin', forgot:'viewForgot', reset:'viewReset', emailMagic:'viewEmailMagic'}[view];
      if (viewId && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
        document.getElementById(viewId).animate([{opacity:0, transform:'translateY(6px)'}, {opacity:1, transform:'none'}], {duration:220, easing:'cubic-bezier(.2,.7,.2,1)'});
      }
    }
    if (!ocean && !loading && !failed && visible() && form.dataset.setup !== undefined) {
      loading = true;
      try {
        const {mountOcean} = await import('/auth-ocean.mjs?v=20261005-glass-1');
        // A remembered session may have entered the app during the download.
        if (!visible()) return;
        ocean = mountOcean(canvas);
        window.taxPearlsOcean = ocean;
        screen.classList.add('liquid-live');
      } catch (error) {
        failed = true;
        unavailable();
        console.warn('Ocean background unavailable:', error);
      } finally { loading = false; }
    }
    if (!ocean) return;
    ocean.setActive(visible());
    if (visible()) ocean.setGlassPanel(form.getBoundingClientRect(), parseFloat(getComputedStyle(form).borderTopLeftRadius));
  }
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; update(); });
  }
  new MutationObserver(schedule).observe(screen, {attributes:true, subtree:true, attributeFilter:['style','data-view','data-setup','hidden']});
  new ResizeObserver(schedule).observe(form);
  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', schedule, {passive:true});
  document.addEventListener('visibilitychange', () => { if (!visible()) ocean?.setActive(false); else schedule(); });
  window.addEventListener('pagehide', () => ocean?.setActive(false));
  window.addEventListener('pageshow', schedule);
  canvas.addEventListener('ocean-unavailable', unavailable);
  canvas.addEventListener('ocean-restored', restored);
  form.querySelectorAll('[data-password]').forEach(button => button.addEventListener('click', () => {
    const input = document.getElementById(button.dataset.password);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.setAttribute('aria-pressed', String(show));
    button.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    button.querySelector('.pw-eye').style.display = show ? 'none' : '';
    button.querySelector('.pw-eye-off').style.display = show ? '' : 'none';
  }));
  form.addEventListener('pointerdown', event => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const button = event.target.closest('#authSubmit, #signupEntryLink, #backToLogin');
    if (!button || button.disabled) return;
    const rect = button.getBoundingClientRect(), ripple = document.createElement('span');
    ripple.className = 'auth-press-ripple';
    ripple.style.left = `${event.clientX - rect.left}px`;
    ripple.style.top = `${event.clientY - rect.top}px`;
    button.append(ripple);
    setTimeout(() => ripple.remove(), 500);
  });
  schedule();
})();
