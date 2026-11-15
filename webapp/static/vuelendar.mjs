import {createApp, h, nextTick} from 'vue';
import VRangeSelector from 'vuelendar/components/vl-range-selector';

// Chinese labels use the upstream components, not a second calendar renderer.
const Calendar = VRangeSelector.components.VlCalendar;
const Month = Calendar.components.VlCalendarMonth;
Month.computed.monthName = function () { return `${this.month + 1}月`; };
Month.computed.daysNames = () => ['一', '二', '三', '四', '五', '六', '日'];

export function mount(target, {onComplete, onDraft}) {
  let view;
  const popup = target.closest('[role="dialog"]');
  let anchor, watching = false, sizeObserver;
  function position() {
    if (!anchor || popup.hidden) return;
    const viewport = window.visualViewport;
    const leftEdge = viewport?.offsetLeft || 0, topEdge = viewport?.offsetTop || 0;
    const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight, gap = 12;
    popup.style.maxHeight = `${Math.max(120, height - gap * 2)}px`;
    const rect = anchor.getBoundingClientRect(), box = popup.getBoundingClientRect();
    const left = Math.max(leftEdge + gap, Math.min(rect.right - box.width, leftEdge + width - box.width - gap));
    const below = rect.bottom + 8, above = rect.top - box.height - 8;
    const preferred = below + box.height <= topEdge + height - gap ? below : above >= topEdge + gap ? above : below;
    const top = Math.max(topEdge + gap, Math.min(preferred, topEdge + height - box.height - gap));
    popup.style.left = `${left}px`; popup.style.top = `${top}px`;
  }
  function reposition(event) {
    if (event?.type === 'scroll' && popup.contains(event.target)) return;
    position();
  }
  function close() {
    if (popup.matches(':popover-open')) popup.hidePopover();
    window.removeEventListener('resize', reposition);
    window.removeEventListener('scroll', reposition, true);
    window.visualViewport?.removeEventListener('resize', reposition);
    window.visualViewport?.removeEventListener('scroll', reposition);
    sizeObserver?.disconnect(); watching = false;
  }
  function show() {
    if (popup.showPopover && !popup.matches(':popover-open')) popup.showPopover();
    position();
    if (watching) return;
    watching = true;
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    window.visualViewport?.addEventListener('resize', reposition);
    window.visualViewport?.addEventListener('scroll', reposition);
    sizeObserver = new ResizeObserver(position); sizeObserver.observe(popup);
  }
  const app = createApp({
    data: () => ({start: '', end: '', anchor: '', month: '', seed: 0}),
    methods: {
      selectStart(value) {
        // Preserve the existing ability to choose an earlier end date.
        if (this.start && !this.end && value < this.start) {
          this.end = this.start; this.start = value; onComplete(this.start, this.end);
        } else { this.start = value; this.end = ''; onDraft(value); }
      },
      selectEnd(value) {
        if (!value) return;
        this.end = value; onComplete(this.start, this.end);
      },
      jump(value) {
        if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value) || +value.slice(0,4) < 100) return;
        this.month = value; this.anchor = `${value}-15`; this.seed++;
      }
    },
    render() {
      return h('div', {class: 'dashboard-lendar', onClick: () => nextTick(() => {
        const calendar = this.$refs.range?.$refs.calendar;
        if (calendar) this.month = `${calendar.currentMonthYear}-${String(calendar.currentMonthMonth + 1).padStart(2,'0')}`;
      })}, [
        h('label', {class: 'dashboard-lendar-jump'}, ['跳转月份', h('input', {type: 'month', min: '0100-01', max: '9998-12',
          'aria-label': '日历显示月份', value: this.month, onChange: event => this.jump(event.target.value)})]),
        h(VRangeSelector, {key: this.seed, ref: 'range', startDate: this.start || undefined,
          endDate: this.end || undefined, defaultDate: this.anchor, singleMonth: true, enableSingleDate: true,
          firstDayOfWeek: 'mon', 'onUpdate:startDate': this.selectStart, 'onUpdate:endDate': this.selectEnd})
      ]);
    }
  });
  view = app.mount(target);
  return {
    close,
    open(start, end, trigger) {
      anchor = trigger || anchor;
      const today = new Date(), year = today.getFullYear(), month = String(today.getMonth() + 1).padStart(2, '0');
      view.start = start || ''; view.end = end || '';
      view.jump(start ? start.slice(0, 7) : `${year}-${month}`);
      nextTick(() => {
        if (popup.hidden) return;
        show();
        target.querySelector('button[aria-pressed="true"], button.vl-calendar-month__day')?.focus({preventScroll: true});
      });
    }
  };
}
