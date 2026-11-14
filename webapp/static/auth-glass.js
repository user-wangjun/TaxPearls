/* The real authentication flow lives in auth.js. This file owns only
 * glass interactions and the lazily mounted, local WebGL background. */
(() => {
  const screen = document.getElementById('authScreen');
  const form = document.getElementById('authForm');
  const canvas = document.getElementById('sceneCanvas');
  const status = document.getElementById('oceanStatus');
  let ocean, loading = false, failed = false, pending = false, lastView = '', revealId = 0, revealComplete = false;
  let startTimer = null, idleId = null, lastInteraction = 0;
  const visible = () => screen.style.display !== 'none' && !document.hidden;
  const canPrepare = () => visible() && form.dataset.ready === '1' && !form.dataset.entering && !form.dataset.busy;
  function cancelPreparation() {
    clearTimeout(startTimer); startTimer = null;
    if (idleId !== null) window.cancelIdleCallback?.(idleId);
    idleId = null;
  }
  function prepareWhenIdle() {
    if (ocean || loading || failed || startTimer !== null || idleId !== null || !canPrepare()) return;
    // The session check and form paint have already finished. Start promptly
    // unless recent input needs a longer quiet interval.
    const delay = lastInteraction ? Math.max(0, 1200 - (performance.now() - lastInteraction)) : 0;
    const queueStart = () => {
      startTimer = null;
      const start = () => {
        idleId = null;
        if (!canPrepare()) return;
        if (lastInteraction && performance.now() - lastInteraction < 1200) { prepareWhenIdle(); return; }
        mountWhenReady();
      };
      if (window.requestIdleCallback) idleId = window.requestIdleCallback(start, {timeout:2000});
      else start();
    };
    // Register before this frame's idle slot. An extra zero-delay timer can
    // miss that slot and add another refresh of waiting on fast devices.
    // The timer fallback still gives browsers without idle callbacks a task
    // boundary before starting the module download.
    if (delay === 0 && window.requestIdleCallback) queueStart();
    else startTimer = setTimeout(queueStart, delay);
  }
  function unavailable() {
    revealId++;
    revealComplete = false;
    screen.classList.remove('liquid-live', 'ocean-ready');
    canvas.style.visibility = 'hidden';
    status.textContent = '海景渲染暂不可用，您仍可正常登录或注册。';
    status.hidden = false;
  }
  async function restored() {
    const id = ++revealId;
    revealComplete = false;
    screen.classList.remove('liquid-live');
    screen.classList.add('ocean-ready');
    canvas.style.visibility = '';
    status.hidden = true;
    update();
    // The live frame starts at the poster's exact pose. Begin the gentle
    // motion ramp during the crossfade instead of adding a second still wait.
    if (visible()) ocean?.releaseOpening();
    const duration = parseFloat(getComputedStyle(canvas).transitionDuration) * 1000;
    if (duration > 0) await new Promise(resolve => {
      let timer;
      const finish = () => { clearTimeout(timer); canvas.removeEventListener('transitionend', ended); resolve(); };
      const ended = event => { if (event.target === canvas && event.propertyName === 'opacity') finish(); };
      canvas.addEventListener('transitionend', ended);
      timer = setTimeout(finish, duration + 100);
    });
    if (id !== revealId) return;
    revealComplete = true;
    if (visible()) {
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches) screen.classList.add('liquid-live');
      ocean?.releaseOpening();
    }
  }
  async function update() {
    const view = form.dataset.view;
    if (visible() && form.dataset.setup !== undefined && view && view !== lastView) {
      const changingView = !!lastView;
      lastView = view;
      const viewId = {login:'viewLogin', signup:'viewSignup', emailLogin:'viewEmailLogin', forgot:'viewForgot', reset:'viewReset', emailMagic:'viewEmailMagic'}[view];
      if (changingView && viewId && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
        document.getElementById(viewId).animate([{opacity:0, transform:'translateY(6px)'}, {opacity:1, transform:'none'}], {duration:220, easing:'cubic-bezier(.2,.7,.2,1)'});
      }
    }
    if (!canPrepare()) cancelPreparation();
    else prepareWhenIdle();
    if (!ocean) return;
    ocean.setActive(visible());
    if (visible()) ocean.setGlassPanel(form.getBoundingClientRect(), parseFloat(getComputedStyle(form).borderTopLeftRadius));
    if (visible() && revealComplete) {
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches) screen.classList.add('liquid-live');
      ocean.releaseOpening();
    }
  }
  async function mountWhenReady() {
    if (!ocean && !loading && !failed && canPrepare()) {
      loading = true;
      try {
        // Download while the form paints; create WebGL only after both finish.
        const [{mountOcean}] = await Promise.all([
          import('/auth-ocean.mjs?v=20261027-ocean-start-9'),
          new Promise(resolve => requestAnimationFrame(resolve)),
        ]);
        // A remembered session may have entered the app during the download.
        if (!canPrepare()) return;
        ocean = await mountOcean(canvas, {holdOpening:true});
        window.taxPearlsOcean = ocean;
        ocean.setActive(visible());
        if (visible()) ocean.setGlassPanel(form.getBoundingClientRect(), parseFloat(getComputedStyle(form).borderTopLeftRadius));
        // Keep the matching static opening frame until full-quality WebGL is
        // ready; the low-resolution sky bootstrap is never exposed to users.
        await ocean.whenSkyReady();
        restored();
      } catch (error) {
        failed = true;
        unavailable();
        console.warn('Ocean background unavailable:', error);
      } finally { loading = false; if (!ocean && !failed) prepareWhenIdle(); }
    }
  }
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; update(); });
  }
  new MutationObserver(schedule).observe(screen, {attributes:true, subtree:true, attributeFilter:['style','data-view','data-setup','data-ready','data-entering','data-busy','hidden']});
  new ResizeObserver(schedule).observe(form);
  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', schedule, {passive:true});
  document.addEventListener('visibilitychange', () => { if (!visible()) { cancelPreparation(); ocean?.setActive(false); } else schedule(); });
  window.addEventListener('pagehide', () => { cancelPreparation(); ocean?.setActive(false); });
  window.addEventListener('pageshow', schedule);
  for (const event of ['pointerdown', 'keydown', 'input']) form.addEventListener(event, () => {
    lastInteraction = performance.now(); cancelPreparation(); prepareWhenIdle();
  }, {passive:true});
  canvas.addEventListener('ocean-unavailable', unavailable);
  canvas.addEventListener('ocean-restored', () => ocean?.whenSkyReady().then(restored, unavailable));
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
