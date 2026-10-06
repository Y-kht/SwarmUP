// SwarmUP: the interface. It draws the state that the program (src/backend/interface/user_interface.py) gives, and sends it what the user does.
// The state comes from /api/poll, which answers as soon as something changed. Every view is drawn again from the state and from ui
// (what only the interface knows: the open window, what is typed...), so the interface never disagrees with the program.
'use strict';

const TOKEN = document.querySelector('meta[name="swarmup-token"]').content;
const STEPS = [
  { view: 'mission', label: 'Mission', icon: 'target' },
  { view: 'agents', label: 'Agents', icon: 'users' },
  { view: 'folders', label: 'Folders', icon: 'folder' },
  { view: 'models', label: 'Models', icon: 'cpu' },
  { view: 'teamwork', label: 'Teamwork', icon: 'workflow' },
  { view: 'launch', label: 'Launch', icon: 'rocket' },
];
const EXAMPLES = [
  'Prepare a short morning briefing on AI news and send it to my phone.',
  'Survey the recent research on battery recycling in 500 words.',
  'Check the proofs of my paper draft, then format it in IEEE style.',
  'Plan my dentist appointment and email my manager that I will be late.',
];
const LONG_TEXT = ['request', 'task', 'topics', 'style'];
const NODE = { width: 206, height: 118, gapX: 76, gapY: 20, pad: 16, user: 120 };
const store = { catalog: null, state: null, feed: [], feedAfter: 0, version: -1, online: true, jobs: {} };
const ui = {
  view: 'home', modal: null, stack: [], selected: null, busy: {}, errors: {}, expanded: {}, visited: new Set(), feedFilter: 'all',
  missionDraft: null, folderDrafts: {}, waitsDraft: null, orderChoice: null, mode: null, resume: null, particles: [], seenFeed: 0,
  composer: {}, corrections: {}, messages: {}, renderedOnce: false, animate: true, forms: {}, codex: { loading: false, data: null },
};
let pointerDown = false, renderPending = false;


// ==============
// Small tools to build the page.
// ==============
function h(tag, props, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') element.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
    else if (key === 'style') for (const [name, style] of Object.entries(value)) name.startsWith('--') ? element.style.setProperty(name, style) : (element.style[name] = style);
    else if (key.startsWith('on')) element.addEventListener(key.slice(2).toLowerCase(), value);
    else if (['value', 'checked', 'disabled', 'selected', 'multiple'].includes(key)) element[key] = value;
    else if (key === 'dataset') Object.assign(element.dataset, value);
    else element.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : String(child));
  }
  return element;
}

function icon(name, size = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `icon ${size}`);
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name] || ICONS.info;
  return svg;
}

function logo() {
  const holder = h('span', { class: 'logo' });
  holder.innerHTML = LOGO;
  return holder;
}

function button(label, options = {}) {
  const { kind = '', size = '', iconName, onClick, disabled, busy, title, type = 'button', after } = options;
  return h('button', { type, class: ['btn', kind, size, !label && 'icon-only'], onClick, disabled: disabled || busy, title: title || null, 'aria-label': !label && title ? title : null },
    busy ? h('span', { class: 'spinner' }) : iconName ? icon(iconName, size === 'sm' ? 'sm' : '') : null, label, after ? icon(after, size === 'sm' ? 'sm' : '') : null);
}

function badge(text, tone = '', iconName = null) {
  return h('span', { class: ['badge', tone] }, iconName ? icon(iconName) : null, text);
}

function callout(tone, iconName, ...body) {
  return h('div', { class: ['callout', tone] }, icon(iconName), h('div', { class: 'callout-body' }, ...body));
}

function taskStyle(task) {
  return TASK_STYLE[task] || { icon: 'sparkles', color: '#8B91AA' };
}

// A colour with some transparency, written #RRGGBBAA, which every web engine understands.
function tint(color, alpha = 0.15) {
  return `${color}${Math.round(alpha * 255).toString(16).padStart(2, '0')}`;
}

function taskIcon(task, size = '') {
  const style = taskStyle(task);
  return h('span', { class: ['task-icon', size], style: { '--task': style.color, '--task-soft': tint(style.color) } }, icon(style.icon, size === 'lg' ? 'lg' : ''));
}

function taskOf(key) {
  if (key === 'leader') return store.catalog.leaderTask;
  return store.catalog.tasks.find(task => task.key === key) || { label: key, role: key, info: '' };
}

function spinner(size = '') {
  return h('span', { class: ['spinner', size] });
}

function loading(text) {
  return h('div', { class: 'loading' }, spinner('lg'), h('div', {}, text));
}

function plural(count, word, many = `${word}s`) {
  return `${count} ${count === 1 ? word : many}`;
}

function firstSentence(text) {
  const found = /^.*?[.!?](\s|$)/.exec(text || '');
  return found ? found[0].trim() : text;
}

function shorten(text, width = 90) {
  const line = String(text || '').replace(/\s+/g, ' ').trim();
  return line.length <= width ? line : `${line.slice(0, width - 1)}…`;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

function parseTime(text) {
  return text ? new Date(text.replace(' ', 'T')) : null;
}

function elapsed(start, end) {
  if (!start) return '';
  const seconds = Math.max(0, Math.round(((end || new Date()) - start) / 1000));
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60, rest = seconds % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}` : `${minutes}:${String(rest).padStart(2, '0')}`;
}

// A text that can be long is shown shortened, with a button to show all of it.
function clamped(text, key, limit = 520, className = 'bubble') {
  const long = String(text || '').length > limit;
  const open = ui.expanded[key];
  return h('div', {},
    h('div', { class: [className, long && !open && 'clamped'] }, pretty(text)),
    long ? h('button', { class: 'more-btn', onClick: () => { ui.expanded[key] = !open; render(); } }, open ? 'Show less' : 'Show more') : null);
}

// What the leader writes as JSON (who waits for whom, which agents a correction concerns) is shown as a list.
function pretty(text) {
  const trimmed = String(text || '').trim().replace(/^```(?:json)?\s*|\s*```$/g, '');
  if (trimmed.startsWith('{')) {
    try {
      const data = JSON.parse(trimmed);
      if (data && typeof data === 'object' && !Array.isArray(data)) {
        const entries = Object.entries(data);
        if (!entries.length) return 'Nobody.';
        const allLists = entries.every(([, value]) => Array.isArray(value));
        return h('dl', { class: 'kv' }, entries.map(([key, value]) => [h('dt', {}, key),
          h('dd', {}, allLists ? (value.length ? `waits for ${value.join(', ')}` : 'waits for nobody') : String(value))]));
      }
    } catch (error) { /* not JSON: shown as it is */ }
  }
  return String(text || '');
}

function extLink(url, label) {
  return h('button', { class: 'link-btn', type: 'button', onClick: () => act('openLink', { url }, { quiet: true }) }, label || url, icon('external', 'sm'));
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    const area = h('textarea', { value: text, style: { position: 'fixed', opacity: '0' } });
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  toast('Copied.', 'success');
}

function codeLine(text) {
  return h('div', { class: 'codeline' }, h('code', {}, text), button('', { size: 'sm', kind: 'ghost', iconName: 'copy', title: 'Copy', onClick: () => copyText(text) }));
}

function toast(text, tone = 'info', action = null) {
  const icons = { success: 'check', danger: 'x', warning: 'alert', info: 'info' };
  const element = h('div', { class: ['toast', tone], role: 'status' }, h('span', { class: 'toast-icon' }, icon(icons[tone] || 'info', 'sm')), h('div', { class: 'grow' }, text),
    action ? h('button', { onClick: () => { action.run(); dismiss(); } }, action.label) : null);
  const dismiss = () => { element.classList.add('leaving'); setTimeout(() => element.remove(), 220); };
  document.getElementById('toasts').append(element);
  setTimeout(dismiss, action ? 9000 : tone === 'danger' ? 8000 : 4800);
}


// ==============
// Talking to the program.
// ==============
class ApiError extends Error {
  constructor(message, errors = {}) {
    super(message);
    this.errors = errors;
  }
}

async function api(action, payload = {}) {
  let response;
  try {
    response = await fetch(`/api/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SwarmUP-Token': TOKEN }, body: JSON.stringify(payload) });
  } catch (error) {
    throw new ApiError('SwarmUP does not answer. Is the program still running?');
  }
  let data;
  try {
    data = await response.json();
  } catch (error) {
    throw new ApiError('SwarmUP gave an answer that cannot be read.');
  }
  if (data.state) applyState(data.state);
  if (!data.ok) throw new ApiError(data.error || 'Something went wrong.', data.errors || {});
  return data;
}

// Runs an action, shows that it is busy, and tells the user what went wrong. It returns the answer, or null if it failed.
// form is an object that receives the errors of the fields (form.errors) and the message (form.error).
async function act(action, payload = {}, options = {}) {
  const key = options.busy || action;
  ui.busy[key] = true;
  render();
  try {
    const data = await api(action, payload);
    if (options.form) { options.form.errors = {}; options.form.error = ''; }
    if (options.ok) toast(options.ok, 'success');
    return data;
  } catch (error) {
    if (options.form) { options.form.errors = error.errors || {}; options.form.error = error.message; }
    if (!options.form || !Object.keys(error.errors || {}).length) toast(error.message, 'danger');
    return null;
  } finally {
    delete ui.busy[key];
    render();
  }
}

async function pollLoop() {
  while (true) {
    try {
      const response = await fetch(`/api/poll?version=${store.version}&feed=${store.feedAfter}`, { headers: { 'X-SwarmUP-Token': TOKEN } });
      if (!response.ok) throw new Error('poll failed');
      const data = await response.json();
      setOnline(true);
      addFeed(data.feed);
      applyState(data.state);
    } catch (error) {
      setOnline(false);
      await sleep(1500);
    }
  }
}

function setOnline(online) {
  if (store.online === online) return;
  store.online = online;
  document.querySelector('.offline')?.remove();
  if (!online) document.body.append(h('div', { class: 'offline banner danger' }, icon('wifiOff'), 'SwarmUP does not answer. Is the program still running?'));
}

function addFeed(items) {
  for (const item of items || []) {
    if (item.id <= store.feedAfter) continue;
    store.feed.push(item);
    store.feedAfter = item.id;
    if (item.kind === 'message' && item.receiver && ui.renderedOnce) ui.particles.push({ from: item.speaker, to: item.receiver });
  }
  store.feed = store.feed.slice(-600);
}

// A new state: the jobs that ended are announced, and the page is drawn again.
function applyState(state) {
  if (store.state && state.version < store.state.version) return;
  const before = store.jobs;
  const signingIn = store.state?.codexLogin?.state === 'waiting';
  store.state = state;
  if (signingIn && state.codexLogin?.state === 'done') {
    toast(`Codex is signed in${state.codexLogin.account?.email ? ` as ${state.codexLogin.account.email}` : ''}.`, 'success');
    loadCodex();
  }
  store.version = state.version;
  store.jobs = state.jobs;
  for (const [name, job] of Object.entries(state.jobs)) {
    if (before[name]?.state === 'running' && job.state !== 'running') onJobEnded(name, job);
  }
  if (ui.view === 'run' && !state.run) ui.view = 'home';
  scheduleRender();
}

function onJobEnded(name, job) {
  if (name === 'order') {
    if (job.state === 'failed') toast(`The leader could not decide: ${job.error}`, 'danger');
    else if (job.result) { toast('The order of the leader is approved.', 'success'); ui.waitsDraft = null; ui.orderChoice = null; }
    else toast('No order was approved, so nothing changed.', 'warning');
    api('clearJob', { name }).catch(() => {});
  }
  if (name === 'leader') {
    if (job.state === 'failed') toast('The leader could not build the swarm. Read why in the step of the mission.', 'danger');
    else if (job.result) { toast(`The leader built a swarm of ${plural(job.result.agents.length, 'agent')}. Check it, then launch it.`, 'success'); go('agents'); }
    else toast('No swarm was built: you rejected the proposal of the leader.', 'warning');
    if (job.state !== 'failed') api('clearJob', { name }).catch(() => {});
  }
  if (name === 'cancel' && job.state === 'failed') toast(job.error, 'danger');
  if (name === 'changes' && job.state === 'failed') toast(job.error, 'danger');
}


// ==============
// Drawing. The page is drawn again when the state changes, keeping the focus, what is selected in a text, and the scroll.
// A redraw waits while a mouse button is down, so a click is never lost.
// ==============
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => {
    renderPending = false;
    if (pointerDown) return;
    render();
  });
}

function saveFocus() {
  const active = document.activeElement;
  const scrolls = {};
  document.querySelectorAll('[data-scroll]').forEach(element => {
    scrolls[element.dataset.scroll] = { top: element.scrollTop, bottom: element.scrollHeight - element.scrollTop - element.clientHeight < 48 };
  });
  return { key: active?.dataset?.key, start: active?.selectionStart, end: active?.selectionEnd, scrolls };
}

function restoreFocus(saved) {
  if (saved.key) {
    const element = document.querySelector(`[data-key="${CSS.escape(saved.key)}"]`);
    if (element) {
      element.focus({ preventScroll: true });
      try { if (saved.start !== null && saved.start !== undefined) element.setSelectionRange(saved.start, saved.end); } catch (error) { /* not a text */ }
    }
  }
  document.querySelectorAll('[data-scroll]').forEach(element => {
    const before = saved.scrolls[element.dataset.scroll];
    if (element.dataset.stick !== undefined && (!before || before.bottom)) element.scrollTop = element.scrollHeight;
    else if (before) element.scrollTop = before.top;
  });
}

function render() {
  if (!store.state || !store.catalog) return;
  const view = VIEWS[ui.view] ? ui.view : 'home';
  const page = VIEWS[view]();
  const saved = saveFocus();
  document.getElementById('app').classList.toggle('no-guide', !page.guide);
  document.getElementById('sidebar').replaceChildren(renderSidebar());
  document.getElementById('header').replaceChildren(...[page.header].flat().filter(Boolean));
  const content = document.getElementById('content');
  content.className = ['content', page.flush && 'flush'].filter(Boolean).join(' ');
  content.replaceChildren(...[page.content].flat().filter(Boolean));
  document.getElementById('footer').replaceChildren(...[page.footer].flat().filter(Boolean));
  document.getElementById('guide').replaceChildren(...(page.guide ? renderGuide(page.guide) : []));
  renderModal();
  restoreFocus(saved);
  fitGraphs();
  drawParticles();
  if (ui.modal?.reveal) { const panel = document.querySelector('.selection-panel'); if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); ui.modal.reveal = false; }
  ui.renderedOnce = true;
  ui.animate = false;
}

function go(view) {
  if (ui.view === view) return;
  ui.view = view;
  ui.animate = true;
  ui.visited.add(view);
  try { sessionStorage.setItem('swarmup-view', view); } catch (error) { /* not available */ }
  render();
  document.getElementById('content').scrollTop = 0;
}

function header(eyebrow, title, subtitle, actions = null) {
  return h('div', { class: 'grow enter' }, eyebrow ? h('div', { class: 'eyebrow' }, eyebrow) : null, h('h1', {}, title), subtitle ? h('p', { class: 'subtitle' }, subtitle) : null);
}

function stepEyebrow(view) {
  const index = STEPS.findIndex(step => step.view === view);
  return `Step ${index + 1} of ${STEPS.length}`;
}

function footer(back, next, note = null) {
  return [back ? button(back.label || 'Back', { kind: 'ghost', iconName: 'arrowLeft', onClick: back.onClick }) : null,
    h('div', { class: 'spacer' }), note ? h('div', { class: ['footer-note', note.error && 'error'] }, icon(note.error ? 'alert' : 'info', 'sm'), note.text) : null,
    next ? button(next.label || 'Continue', { kind: 'primary', size: next.size || '', after: next.iconName ? null : 'arrowRight', iconName: next.iconName, onClick: next.onClick,
      disabled: next.disabled, busy: next.busy }) : null];
}

function renderGuide(cards) {
  return [h('div', { class: 'guide-title' }, h('span', { class: 'bubble' }, icon('sparkles')), 'Guide'),
    ...cards.map(card => h('div', { class: ['guide-card', card.tone || ''] }, card.q ? h('div', { class: 'guide-q' }, card.icon ? icon(card.icon, 'sm') : null, card.q) : null,
      ...(Array.isArray(card.text) ? [h('ul', {}, card.text.map(item => h('li', {}, item)))] : [card.text])))];
}


// ==============
// The sidebar: the steps, the live swarm, the theme and the help.
// ==============
function stepState() {
  const state = store.state;
  const agents = state.agents;
  const models = agents.length > 0 && agents.every(agent => agent.model);
  const team = state.buildMode === 'leader' ? agents.length > 1 : agents.length > 0;
  return {
    mission: { done: !!state.mission && (state.buildMode !== 'leader' || team), enabled: true },
    agents: { done: team, enabled: !!state.mission },
    folders: { done: agents.length > 0 && ui.visited.has('folders'), enabled: agents.length > 0 },
    models: { done: models && team, enabled: agents.length > 0 },
    teamwork: { done: models && ui.visited.has('teamwork'), enabled: models },
    launch: { done: false, enabled: models },
  };
}

function renderSidebar() {
  const state = store.state, run = state.run, running = run?.running;
  const steps = stepState();
  const needsYou = state.questions.length > 0 || (run?.ready || []).length > 0;
  const theme = currentTheme();
  return h('div', { class: 'stack tight', style: { flex: '1', minHeight: '0' } },
    h('div', { class: 'brand' }, logo(), h('div', {}, h('div', { class: 'brand-name' }, 'Swarm', h('span', {}, 'UP')), h('div', { class: 'brand-sub' }, 'Your team of AI agents'))),
    h('button', { class: ['nav-item', ui.view === 'home' && 'active'], onClick: () => go('home') }, h('span', { class: 'nav-step' }, icon('home', 'sm')), 'Home'),
    h('div', { class: 'nav-title' }, 'Build your swarm'),
    STEPS.map((step, index) => {
      const info = steps[step.view];
      const enabled = info.enabled && !running;
      return h('button', { class: ['nav-item', ui.view === step.view && 'active', info.done && ui.view !== step.view && 'done'], disabled: !enabled,
        title: running ? 'The swarm is running: wait until it finishes to change it.' : !enabled ? 'Finish the steps before this one first.' : null, onClick: () => go(step.view) },
        h('span', { class: 'nav-step' }, info.done && ui.view !== step.view ? icon('check') : String(index + 1)), step.label,
        step.view === 'agents' && state.agents.length ? h('span', { class: 'nav-hint' }, String(state.agents.length)) : null);
    }),
    h('div', { class: 'nav-title' }, 'Run'),
    h('button', { class: ['nav-item', ui.view === 'run' && 'active'], disabled: !run, onClick: () => go('run'), title: run ? null : 'Start a swarm to follow it here.' },
      h('span', { class: 'nav-step' }, icon('activity', 'sm')), 'Live swarm', running ? h('span', { class: ['nav-live-dot', needsYou && 'attention'], title: needsYou ? 'Something waits for you' : 'Running' }) : null),
    h('div', { class: 'sidebar-bottom' },
      h('div', { class: 'theme-switch', role: 'group', 'aria-label': 'Theme' },
        [['auto', 'monitor', 'Like the system'], ['light', 'sun', 'Light'], ['dark', 'moon', 'Dark']].map(([value, iconName, label]) =>
          h('button', { class: theme === value && 'active', title: `${label} theme`, 'aria-label': `${label} theme`, onClick: () => setTheme(value) }, icon(iconName, 'sm')))),
      h('button', { class: 'nav-item', onClick: () => openModal({ type: 'help' }) }, h('span', { class: 'nav-step' }, icon('help', 'sm')), 'How SwarmUP works'),
      h('button', { class: 'nav-item', onClick: () => openModal({ type: 'quit' }) }, h('span', { class: 'nav-step' }, icon('power', 'sm')), 'Quit SwarmUP')));
}

function currentTheme() {
  try { return localStorage.getItem('swarmup-theme') || 'auto'; } catch (error) { return 'auto'; }
}

function setTheme(theme) {
  try { localStorage.setItem('swarmup-theme', theme); } catch (error) { /* kept for this window only */ }
  document.documentElement.dataset.theme = theme;
  render();
}


// ==============
// Home: what SwarmUP is, and the swarms that were interrupted.
// ==============
function heroArt() {
  const holder = h('div', { class: 'hero-art' });
  const hexagon = (x, y, r, fill, opacity) => {
    const points = Array.from({ length: 6 }, (_, i) => { const a = Math.PI / 3 * i - Math.PI / 2; return `${x + r * Math.cos(a)},${y + r * Math.sin(a)}`; }).join(' ');
    return `<polygon class="hex" points="${points}" fill="${fill}" fill-opacity="${opacity}"/>`;
  };
  holder.innerHTML = `<svg viewBox="0 0 330 300">${hexagon(200, 150, 58, '#FFC857', 1)}${hexagon(110, 95, 34, '#fff', .9)}${hexagon(110, 205, 34, '#fff', .9)}` +
    `${hexagon(285, 85, 30, '#fff', .55)}${hexagon(285, 215, 30, '#fff', .55)}${hexagon(45, 150, 22, '#fff', .35)}${hexagon(200, 30, 18, '#FFC857', .6)}${hexagon(200, 270, 18, '#FFC857', .6)}` +
    '<g stroke="#fff" stroke-opacity=".55" stroke-width="2.5" stroke-linecap="round"><path d="M140 108 160 124M140 192 160 176M243 112 262 100M243 188 262 200"/></g></svg>';
  return holder;
}

function viewHome() {
  const state = store.state, run = state.run;
  const hasWork = state.agents.length > 0 || state.mission;
  return {
    header: null,
    content: h('div', { class: 'stack loose', style: { paddingTop: '22px' } },
      h('section', { class: 'hero enter' }, heroArt(),
        h('h1', {}, 'Build your own team of AI agents'),
        h('p', {}, 'SwarmUP guides you step by step: you say what you want done, pick the agents and the AI models that think for them, and approve everything they do. No code, and nothing happens without your approval.'),
        h('div', { class: 'row wrap' },
          hasWork && !run?.running ? button('Continue building', { kind: 'primary', size: 'lg', after: 'arrowRight', onClick: () => go(state.mission ? 'agents' : 'mission') }) : null,
          run?.running ? button('Open the live swarm', { kind: 'primary', size: 'lg', iconName: 'activity', onClick: () => go('run') }) :
            button(hasWork ? 'Start a new swarm' : 'Start building', { kind: hasWork ? 'ghost' : 'primary', size: 'lg', iconName: hasWork ? 'plus' : null, after: hasWork ? null : 'arrowRight',
              onClick: () => hasWork ? openModal({ type: 'confirmNew' }) : go('mission') }),
          button('How it works', { kind: 'ghost', size: 'lg', iconName: 'help', onClick: () => openModal({ type: 'help' }) }))),
      state.unfinished.length ? h('section', { class: 'stack enter-2' },
        h('div', { class: 'section-title' }, h('h2', {}, 'Swarms waiting for you'), badge(String(state.unfinished.length), 'honey')),
        state.unfinished.map(savedCard)) : null,
      h('section', { class: 'stack enter-3' }, h('div', { class: 'section-title' }, h('h2', {}, 'How it works')),
        h('div', { class: 'how' }, [
          ['Say the mission', 'One or two sentences about what the swarm must achieve. Every agent reads it.'],
          ['Pick the agents', 'Each agent does one task: an email, a survey, a briefing, code… The first one leads.'],
          ['Give them a brain', 'Choose an AI model for each agent: free on your own GPUs, or paid through an API.'],
          ['Approve everything', 'The agents show you their plan and their work. Nothing is sent or saved before you say yes.'],
        ].map(([title, text], index) => h('div', { class: 'card' }, h('div', { class: 'num' }, String(index + 1)), h('h3', {}, title), h('p', {}, text))))),
      h('section', { class: 'card pad enter-3' }, h('div', { class: 'row top' }, h('span', { class: 'task-icon lg', style: { '--task': '#16A34A', '--task-soft': '#16A34A26' } }, icon('shield', 'lg')),
        h('div', { class: 'stack tight' }, h('h3', {}, 'Your work and your secrets are safe'),
          h('p', { class: 'muted' }, 'Passwords, API keys and tokens are only kept in memory while SwarmUP runs, and never written to a file. The state of a swarm is saved at every change, so if the internet or the computer stops, you continue where you were.'))))),
    footer: null,
    guide: [
      { q: 'New here?', icon: 'sparkles', text: 'Press "Start building". Every step explains what to do, and you can always go back with the steps on the left.', tone: 'tip' },
      { q: 'What is a swarm?', text: 'A small team of AI agents that work for you at the same time. The first agent is the leader: it summarises the work of the others for you.' },
      { q: 'What does it cost?', text: 'Models on your own GPUs are free. Models through an API (OpenAI, Anthropic, Google, DeepSeek) are billed by their company to your account.' },
    ],
  };
}

function savedCard(saved) {
  return h('div', { class: 'card saved-card' }, h('span', { class: 'saved-icon' }, icon('hourglass', 'lg')),
    h('div', { class: 'grow stack tight' },
      h('h3', {}, saved.mission),
      h('div', { class: 'small muted' }, `${saved.reason} on ${saved.savedAt}. Your work is saved. ${saved.mode === 'plan' ? 'It was planning.' : 'It was executing.'}`),
      h('div', { class: 'row wrap', style: { marginTop: '6px' } }, saved.members.map(member => h('span', { class: 'badge outline' }, h('span', { style: { color: taskStyle(member.task).color, display: 'inline-flex' } }, icon(taskStyle(member.task).icon)), member.name,
        h('span', { class: 'faint' }, `· ${member.status}`)))),
      saved.running ? callout('warning', 'info', 'This swarm seems to be working in another window of SwarmUP, so it is left alone. If that window was closed a few seconds ago, try again in half a minute.') : null,
      !saved.canResume ? callout('warning', 'alert', 'This swarm was made by another program, so it cannot be continued here. You can still cancel it.') : null),
    h('div', { class: 'stack tight' },
      button('Continue', { kind: 'primary', iconName: 'play', disabled: saved.running || !saved.canResume, busy: ui.busy[`resume-${saved.id}`], onClick: () => openResume(saved.id) }),
      button('Cancel it', { kind: 'danger-ghost', iconName: 'x', disabled: saved.running, onClick: () => openCancel(saved) }),
      button('', { kind: 'ghost', size: 'sm', iconName: 'refresh', title: 'Check again', onClick: () => act('refreshUnfinished') })));
}


// ==============
// Step 1: the mission, and who builds the swarm: the user agent by agent, or the leader (it proposes the agents, and the user approves).
// ==============
function viewMission() {
  if (ui.missionDraft === null) ui.missionDraft = store.state.mission;
  const state = store.state;
  const error = ui.errors.mission;
  const leaderMode = state.buildMode === 'leader';
  const built = leaderMode && state.agents.length > 1;
  const saveMission = async () => {
    const form = {};
    const data = await act('setMission', { mission: ui.missionDraft }, { form, busy: 'mission' });
    ui.errors.mission = form.errors?.mission || '';
    render();
    return !!data;
  };
  const save = async () => { if (await saveMission()) go('agents'); };
  const build = async () => {
    if (!await saveMission()) return;
    const form = ui.errors.leader = {};
    await act('buildWithLeader', {}, { form, busy: 'buildWithLeader' });
    render();
  };
  const building = state.jobs.leader?.state === 'running';
  const next = !leaderMode || built ? { label: 'Continue to the agents', onClick: save, busy: ui.busy.mission, disabled: building } :
    { label: 'Ask the leader to build the swarm', iconName: 'sparkles', onClick: build, busy: ui.busy.buildWithLeader || building };
  return {
    header: header(stepEyebrow('mission'), 'What should your swarm achieve?', 'Describe the goal in your own words, as you would to a colleague. Every agent reads it to understand what it works for.'),
    content: h('div', { class: 'stack loose', style: { maxWidth: '860px' } },
      h('div', { class: ['card pad enter', error && 'has-error'] },
        h('div', { class: 'field' },
          h('label', { class: 'field-label', for: 'mission' }, 'The mission', h('span', { class: 'req' }, '*')),
          h('textarea', { id: 'mission', class: 'textarea big', 'data-key': 'mission', placeholder: 'For example: prepare a short morning briefing on AI news and send it to my phone.',
            value: ui.missionDraft, onInput: event => { ui.missionDraft = event.target.value; },
            onKeydown: event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') next.onClick(); } }),
          error ? h('div', { class: 'field-error' }, icon('alert'), error) : h('div', { class: 'field-help' }, icon('info'), 'One or two sentences are enough. You can change it later.'))),
      h('div', { class: 'stack enter-2' }, h('h4', {}, 'Need inspiration? Click an example'),
        h('div', { class: 'pills' }, EXAMPLES.map(example => h('button', { class: 'pill', type: 'button', onClick: () => { ui.missionDraft = example; ui.errors.mission = ''; render(); } }, icon('sparkles', 'sm'), example)))),
      h('div', { class: 'stack enter-2' }, h('h2', {}, 'Who builds the swarm?'),
        h('div', { class: 'choice-cards' }, [
          ['manual', 'users', 'I build it myself', 'You add each agent, answer its questions, and choose its folder and its model. Full control, step by step.'],
          ['leader', 'crown', 'The leader builds it', 'You choose the model of the leader and the folder of the mission. It proposes the agents, their tasks and their models, and you approve.'],
        ].map(([mode, iconName, title, text]) => h('button', { type: 'button', class: ['choice-card', state.buildMode === mode && 'selected'], disabled: building || ui.busy.setBuildMode,
          onClick: () => { if (state.buildMode !== mode) act('setBuildMode', { mode }); } }, h('span', { class: 'cc-icon' }, icon(iconName, 'lg')),
          h('div', {}, h('div', { class: 'cc-title' }, title, mode === 'leader' ? badge('New', 'honey') : null), h('div', { class: 'cc-text' }, text)), h('span', { class: 'radio' }))))),
      leaderMode ? leaderSetup(build) : null),
    footer: footer({ label: 'Home', onClick: () => go('home') }, next),
    guide: leaderMode ? [
      { q: 'What the leader does', icon: 'crown', tone: 'tip', text: ['reads your mission and the files of its folder', 'chooses the agents, their tasks, their settings and their models', 'shows you all of it, with its reasons, for your approval'] },
      { q: 'You stay in charge', text: 'Nothing is made before you approve. Ask for changes in your own words: the leader writes its proposal again. Passwords and API keys are asked to you, never to the leader.' },
      { q: 'While the swarm works', text: 'The leader can propose to add an agent, to remove one (to free a GPU, for example) or to change a model. Each proposal comes with its reason, and waits for your approval.' },
    ] : [
      { q: 'Why a mission?', text: 'The agents work better when they know the goal of the whole team, not only their own task.', tone: 'tip', icon: 'target' },
      { q: 'Good missions are', text: ['concrete: what must exist at the end', 'short: one or two sentences', 'in your own words: no special format'] },
      { q: 'Example', text: '"Write a 500-word survey on battery recycling and email it to my supervisor."' },
    ],
  };
}

// The leader that builds the swarm: its model and the folder of the mission, then the button that asks it to build.
function leaderSetup(build) {
  const state = store.state;
  const leader = state.agents.find(agent => agent.builder);
  if (!leader) return null;
  const job = state.jobs.leader;
  const building = job?.state === 'running';
  const others = state.agents.filter(agent => !agent.builder);
  const form = ui.errors.leader || {};
  const draft = ui.folderDrafts[leader.id] ?? leader.folder ?? '';
  const folderError = (ui.errors[`folder-${leader.id}`] || {}).errors?.folder || form.errors?.folder;
  const saveFolder = folder => act('setFolder', { agentId: leader.id, folder }, { busy: `folder-${leader.id}`, form: folderForm(leader) }).then(data => {
    if (data) { ui.folderDrafts[leader.id] = undefined; if (ui.errors.leader?.errors) delete ui.errors.leader.errors.folder; }
    render();
  });
  return h('div', { class: 'card enter-3 leader-setup' },
    h('div', { class: 'card-head' }, taskIcon('leader'), h('div', { class: 'grow' }, h('h3', {}, `${leader.name}, the leader that builds your swarm`),
      h('div', { class: 'small muted' }, 'It works last: it follows the agents, proposes changes to you, and writes the final report.'))),
    h('div', { class: 'card-body stack' },
      h('div', { class: ['field', folderError && 'has-error'] }, h('label', { class: 'field-label' }, 'The folder of the mission', h('span', { class: 'req' }, '*')),
        h('div', { class: 'input-group' },
          h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('folder', 'sm')),
            h('input', { class: 'input with-icon', 'data-key': `folder-${leader.id}`, value: draft, placeholder: 'Write a path or press Browse', disabled: building,
              onInput: event => { ui.folderDrafts[leader.id] = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') saveFolder(event.target.value); },
              onBlur: event => { if (event.target.value !== (leader.folder || '')) saveFolder(event.target.value); } })),
          button('Browse…', { iconName: 'folderOpen', disabled: building, onClick: () => pickPath('folder', draft, path => { ui.folderDrafts[leader.id] = path; saveFolder(path); }) })),
        folderError ? h('div', { class: 'field-error' }, icon('alert'), folderError) :
          h('div', { class: 'field-help' }, icon('info'), 'The leader reads the names of its files to choose the agents. Every agent works in it, and the final report is saved in it.')),
      h('div', { class: ['field', form.errors?.model && 'has-error'] }, h('label', { class: 'field-label' }, 'The model of the leader', h('span', { class: 'req' }, '*')),
        h('div', { class: 'row wrap' }, leader.model ? h('span', { class: 'model-chip' }, icon(modelIcon(leader.model), 'sm'), h('span', {}, modelName(leader.model))) : h('span', { class: 'small faint' }, 'No model yet.'),
          button(leader.model ? 'Change' : 'Choose its model', { kind: leader.model ? '' : 'primary', size: 'sm', iconName: leader.model ? 'refresh' : 'plus', disabled: building, onClick: () => openModelPicker(leader) })),
        form.errors?.model ? h('div', { class: 'field-error' }, icon('alert'), form.errors.model) :
          h('div', { class: 'field-help' }, icon('info'), 'It must plan well and write its proposals in a strict format: a capable model is worth it here.'),
        leader.missing.length ? callout('warning', 'download', h('b', {}, `This model needs ${leader.missing.join(', ')}.`), codeLine(`pip install -U ${leader.missing.join(' ')}`)) : null),
      others.length && !(job?.state === 'done') ? callout('info', 'info', `Your ${plural(others.length, 'agent')} (${others.map(agent => agent.name).join(', ')}) will be replaced by the swarm of the leader when you approve it.`) : null,
      building ? h('div', { class: 'row building' }, spinner(), h('div', { class: 'grow' }, h('b', {}, `${leader.name} is building your swarm…`),
        h('div', { class: 'small muted' }, 'Its proposal opens in a window for your approval. You can ask for changes in your own words.'))) :
        job?.state === 'failed' ? callout('danger', 'alert', h('b', {}, 'The leader could not build the swarm.'), h('div', {}, job.error)) :
          state.agents.length > 1 ? callout('success', 'checkCircle', h('b', {}, `The leader built a swarm of ${plural(others.length, 'agent')}.`), h('div', {}, 'Check it in the next steps: you can still change any agent. Or ask the leader for a new proposal.')) : null,
      form.error && !Object.keys(form.errors || {}).length ? callout('danger', 'alert', form.error) : null,
      state.agents.length > 1 ? h('div', { class: 'row' }, h('div', { class: 'grow' }),
        button('Ask for a new proposal', { kind: 'soft', iconName: 'sparkles', busy: ui.busy.buildWithLeader || building, onClick: build })) : null));
}


// ==============
// Step 2: the agents. Each one has a task, the answers that the task needs, and a name.
// ==============
function viewAgents() {
  const state = store.state;
  const agents = state.agents;
  const leaderMode = state.buildMode === 'leader';
  if (leaderMode && agents.length <= 1) {
    return {
      header: header(stepEyebrow('agents'), 'Who is in your team?', 'The leader builds your team from your mission.'),
      content: h('div', { class: 'card empty enter' }, h('div', { class: 'empty-icon' }, icon('crown', 'xl')), h('h2', {}, 'The leader did not build the swarm yet'),
        h('p', { style: { margin: '8px auto 18px', maxWidth: '460px' } }, 'Give the leader its model and the folder of the mission, then ask it to build the swarm. You approve its proposal before anything is made.'),
        button('Go to the leader', { kind: 'primary', size: 'lg', iconName: 'crown', onClick: () => go('mission') })),
      footer: footer({ onClick: () => go('mission') }, null),
      guide: null,
    };
  }
  return {
    header: header(stepEyebrow('agents'), 'Who is in your team?', leaderMode ? 'The team the leader proposed and you approved. You can still change, add or remove agents: the leader stays first and leads.' :
      'Add an agent for each task. The first agent is the leader: it does its own task, works last, and summarises the work of the others for you.', null),
    content: h('div', { class: 'stack' },
      agents.length === 0 ? h('div', { class: 'card empty enter' }, h('div', { class: 'empty-icon' }, icon('users', 'xl')), h('h2', {}, 'Your team is empty'),
        h('p', { style: { margin: '8px auto 18px', maxWidth: '440px' } }, 'Add your first agent: choose what it must do, then answer a few questions about it. One agent is enough to start.'),
        button('Add your first agent', { kind: 'primary', size: 'lg', iconName: 'plus', onClick: () => openModal({ type: 'tasks' }) })) :
        h('div', { class: 'agent-grid' }, agents.map((agent, index) => agentCard(agent, index, agents.length)),
          h('button', { class: 'add-card enter', type: 'button', onClick: () => openModal({ type: 'tasks' }) }, h('span', { class: 'plus' }, icon('plus', 'lg')), 'Add an agent',
            h('span', { class: 'small faint', style: { fontWeight: '500' } }, 'Email, survey, briefing, code…')))),
    footer: footer({ onClick: () => go('mission') }, { label: 'Continue to the folders', disabled: !agents.length, onClick: () => go('folders') },
      agents.length ? { text: `${plural(agents.length, 'agent')} · ${agents[0].name} leads` } : { text: 'Add at least one agent to continue.' }),
    guide: leaderMode ? [
      { q: 'Built by the leader', icon: 'crown', tone: 'tip', text: 'Each card says why the leader chose the agent. Click a card to change its settings, as if you had added it yourself.' },
      { q: 'Want another team?', text: 'Go back to the mission and ask the leader for a new proposal, in your own words.' },
      { q: 'During the run', text: 'The leader may propose to add or remove agents. Every proposal waits for your approval.' },
    ] : [
      { q: 'The leader', icon: 'crown', text: 'The first agent leads the team. Use the arrows on a card to change who leads. Give the leader a capable model: it writes the summaries you approve.', tone: 'tip' },
      { q: 'One task per agent', text: 'An agent does one thing well. For an email and a survey, add two agents: they work at the same time.' },
      { q: 'You can change your mind', text: 'Click a card to edit it. A removed agent can be brought back right away with Undo.' },
    ],
  };
}

function agentCard(agent, index, total) {
  const move = position => act('moveAgent', { agentId: agent.id, position });
  const stop = handler => event => { event.stopPropagation(); handler(); };
  const builderLeads = store.state.buildMode === 'leader';
  if (agent.builder) {
    return h('div', { class: ['card agent-card leader', ui.animate && 'enter'], role: 'button', tabindex: '0', onClick: () => go('mission'), onKeydown: event => { if (event.key === 'Enter') go('mission'); } },
      h('div', { class: 'row' }, taskIcon('leader'), h('div', { class: 'grow' }, h('div', { class: 'row', style: { gap: '6px' } }, h('h3', {}, agent.name), h('span', { class: 'leader-mark', title: 'Leader' }, icon('crown', 'sm'))),
        h('div', { class: 'small muted' }, taskOf('leader').label)), badge('Leader', 'honey')),
      h('p', { class: 'agent-desc' }, agent.description),
      h('div', { class: 'row wrap', style: { gap: '6px' } }, agent.folder ? badge(shorten(agent.folder.split(/[\\/]/).pop() || agent.folder, 24), '', 'folder') : null,
        agent.model ? badge(shorten(modelName(agent.model), 30), { cli: 'honey', local: 'primary', api: 'info' }[modelKind(agent.model)], modelIcon(agent.model)) : null),
      h('div', { class: 'agent-actions' }, button('Its model and folder', { kind: 'ghost', size: 'sm', iconName: 'edit', onClick: stop(() => go('mission')) })));
  }
  return h('div', { class: ['card agent-card', agent.isLeader && 'leader', ui.animate && 'enter'], role: 'button', tabindex: '0', onClick: () => openAgentForm(agent.task, agent.id),
    onKeydown: event => { if (event.key === 'Enter') openAgentForm(agent.task, agent.id); } },
    h('div', { class: 'row' }, taskIcon(agent.task),
      h('div', { class: 'grow' }, h('div', { class: 'row', style: { gap: '6px' } }, h('h3', {}, agent.name), agent.isLeader ? h('span', { class: 'leader-mark', title: 'Leader' }, icon('crown', 'sm')) : null),
        h('div', { class: 'small muted' }, taskOf(agent.task).label)),
      agent.isLeader ? badge('Leader', 'honey') : badge(`#${index + 1}`, 'outline')),
    h('p', { class: 'agent-desc' }, agent.description),
    agent.why ? h('p', { class: 'agent-why small' }, icon('crown', 'sm'), ` ${agent.why}`) : null,
    h('div', { class: 'row wrap', style: { gap: '6px' } }, agent.folder ? badge(shorten(agent.folder.split(/[\\/]/).pop() || agent.folder, 24), '', 'folder') : null,
      agent.model ? badge(shorten(modelName(agent.model), 30), { cli: 'honey', local: 'primary', api: 'info' }[modelKind(agent.model)], modelIcon(agent.model)) : null),
    h('div', { class: 'agent-actions' },
      button('Edit', { kind: 'ghost', size: 'sm', iconName: 'edit', onClick: stop(() => openAgentForm(agent.task, agent.id)) }),
      !agent.isLeader && !builderLeads ? button('Make leader', { kind: 'ghost', size: 'sm', iconName: 'crown', onClick: stop(() => move(0)) }) : null,
      h('div', { class: 'spacer grow' }),
      button('', { kind: 'ghost', size: 'sm', iconName: 'chevronLeft', title: 'Move earlier', disabled: index === 0 || (builderLeads && index === 1), onClick: stop(() => move(index - 1)) }),
      button('', { kind: 'ghost', size: 'sm', iconName: 'chevronRight', title: 'Move later', disabled: index === total - 1, onClick: stop(() => move(index + 1)) }),
      button('', { kind: 'ghost', size: 'sm', iconName: 'trash', title: `Remove ${agent.name}`, onClick: stop(() => removeAgent(agent)) })));
}

async function removeAgent(agent) {
  const data = await act('removeAgent', { agentId: agent.id });
  if (data) toast(`${agent.name} was removed.`, 'info', { label: 'Undo', run: () => act('undoRemove', {}, { ok: `${agent.name} is back.` }) });
}

function taskPickerModal(modal) {
  return modalFrame({
    size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#5B4CF0', '--task-soft': '#5B4CF026' } }, icon('plus', 'lg')), title: 'What must this agent do?',
    subtitle: 'Choose a task. Next, you answer a few questions about it.',
    body: h('div', { class: 'task-grid' }, store.catalog.tasks.map(task => h('button', { class: 'task-tile', type: 'button', style: { '--task': taskStyle(task.key).color, '--task-shadow': tint(taskStyle(task.key).color, 0.45) },
      onClick: () => { closeModal(); openAgentForm(task.key, null, modal.live); } }, taskIcon(task.key), h('div', {}, h('h3', {}, task.label), h('p', {}, firstSentence(task.info)))))),
    foot: [h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal })],
  });
}

// ---------- The form of an agent ----------
// live: the agent is prepared from the live view, to join the swarm while it runs (then come its model, and the step to join).
async function openAgentForm(task, agentId, live = false) {
  const modal = { type: 'agent', task, agentId, form: null, values: {}, touched: {}, errors: {}, error: '', name: '', reveal: {}, checks: {}, outletGroup: null,
    outletSearch: '', feedUrl: '', publisherSearch: '', publisherResults: null, chats: null, advanced: false, about: !agentId, live };
  openModal(modal);
  const data = await act('taskForm', { task, agentId }, { busy: 'form' });
  if (!data) { closeModal(); return; }
  modal.form = data.form;
  modal.name = data.form.name;
  modal.values = startValues(data.form);
  render();
}

function emptyValue(field) {
  if (field.kind === 'choices') return field.default || [];
  if (field.kind === 'outlets') return [];
  if (field.kind === 'publishers') return field.default || {};
  if (field.kind === 'accounts') return [];
  if (field.kind === 'secret') return '';
  return field.default ?? '';
}

function startValues(form) {
  const values = { ...form.values };
  for (const field of [...form.fields, ...form.advanced]) {
    if (values[field.key] === undefined || values[field.key] === null) values[field.key] = emptyValue(field);
  }
  return values;
}

// The questions depend on the answers (the servers of the email provider, the questions of the messaging app...), so the form is asked again.
async function refreshForm(modal) {
  try {
    const data = await api('taskForm', { task: modal.task, agentId: modal.agentId, values: modal.values });
    for (const field of data.form.fields) {
      const old = modal.form.fields.find(other => other.key === field.key);
      if (!modal.touched[field.key] && old && JSON.stringify(old.default) !== JSON.stringify(field.default) && !['secret', 'choice'].includes(field.kind)) modal.values[field.key] = emptyValue(field);
      if (modal.values[field.key] === undefined) modal.values[field.key] = emptyValue(field);
    }
    modal.form = { ...modal.form, fields: data.form.fields };
  } catch (error) {
    toast(error.message, 'danger');
  }
  render();
}

function setValue(modal, key, value, options = {}) {
  modal.values[key] = value;
  modal.touched[key] = true;
  if (modal.errors[key]) { delete modal.errors[key]; options.redraw = true; }
  if (options.refresh) refreshForm(modal);
  else if (options.redraw) render();
}

// The questions of tasks_library.py are also asked in the command line, so some end with a hint in parentheses: it goes under the question here.
function splitQuestion(ask) {
  const found = /^(.*?)\s*\(([^()]*)\)\s*$/.exec(ask);
  if (!found || !found[1]) return [ask, ''];
  const hint = found[2].trim();
  return [found[1], `${hint.charAt(0).toUpperCase()}${hint.slice(1)}${/[.!?]$/.test(hint) ? '' : '.'}`];
}

function fieldBlock(field, modal, widget) {
  const error = modal.errors[field.key];
  const optional = !field.required && !['choice', 'choices'].includes(field.kind);
  const [label, hint] = field.kind === 'time' ? [splitQuestion(field.ask)[0], ''] : splitQuestion(field.ask);
  const help = [hint, field.help].filter(Boolean).join(' ');
  return h('div', { class: ['field', error && 'has-error'], 'data-field': field.key },
    h('label', { class: 'field-label', for: `f-${field.key}` }, label, field.required ? h('span', { class: 'req', title: 'Needed' }, '*') : optional ? h('span', { class: 'opt' }, 'optional') : null),
    widget,
    error ? h('div', { class: 'field-error' }, icon('alert'), error) : help ? h('div', { class: 'field-help' }, icon('info'), help) : null);
}

function textInput(field, modal, options = {}) {
  const key = field.key;
  const long = LONG_TEXT.includes(key) || (key === 'subject' && modal.task !== 'email');
  const attributes = { id: `f-${key}`, 'data-key': `f-${key}`, value: modal.values[key] ?? '', placeholder: options.placeholder || '', onInput: event => setValue(modal, key, event.target.value) };
  if (long) return h('textarea', { ...attributes, class: 'textarea', rows: 3 });
  return h('input', { ...attributes, class: ['input', options.mono && 'mono'], type: options.type || 'text', min: options.min || null, autocomplete: 'off', spellcheck: options.mono ? 'false' : null });
}

function secretInput(field, modal) {
  const key = field.key, shown = modal.reveal[key];
  return h('div', { class: 'input-wrap' },
    h('input', { id: `f-${key}`, 'data-key': `f-${key}`, class: 'input', type: shown ? 'text' : 'password', autocomplete: 'off', value: modal.values[key] || '',
      placeholder: field.saved ? '•••••••• kept in memory. Leave empty to keep it.' : '', onInput: event => setValue(modal, key, event.target.value) }),
    h('button', { class: 'input-action', type: 'button', title: shown ? 'Hide' : 'Show', onClick: () => { modal.reveal[key] = !shown; render(); } }, icon(shown ? 'eyeOff' : 'eye')));
}

function pathInput(field, modal) {
  const key = field.key;
  const placeholder = { file: 'Choose an existing file', path: 'Choose where the file goes (it can be new)', folder: 'Choose a folder' }[field.kind];
  return h('div', { class: 'input-group' }, h('div', { class: 'input-wrap' }, textInput(field, modal, { placeholder })),
    button('Browse…', { iconName: 'folderOpen', onClick: () => pickPath(field.kind, modal.values[key], path => setValue(modal, key, path, { redraw: true })) }));
}

function timeInput(field, modal) {
  const key = field.key, value = modal.values[key] || '';
  const atTime = value !== '' || modal.touched[`${key}-mode`];
  return h('div', { class: 'stack tight' },
    h('div', { class: 'segmented' },
      h('button', { type: 'button', class: !atTime && 'active', onClick: () => { modal.touched[`${key}-mode`] = false; setValue(modal, key, '', { redraw: true }); } }, icon('zap', 'sm'), 'Right away'),
      h('button', { type: 'button', class: atTime && 'active', onClick: () => { modal.touched[`${key}-mode`] = true; setValue(modal, key, value || '07:30', { redraw: true }); } }, icon('clock', 'sm'), 'At a time of the day')),
    atTime ? h('div', { class: 'row' }, h('input', { id: `f-${key}`, 'data-key': `f-${key}`, class: 'input', type: 'time', style: { maxWidth: '160px' }, value,
      onInput: event => setValue(modal, key, event.target.value), onChange: () => render() }), h('span', { class: 'small muted' }, describeClock(value))) : null);
}

function describeClock(text) {
  const found = /^(\d{1,2}):(\d{2})$/.exec((text || '').trim());
  if (!found) return '';
  const now = new Date(), at = new Date();
  at.setHours(Number(found[1]), Number(found[2]), 0, 0);
  if (at <= now) at.setDate(at.getDate() + 1);
  const minutes = Math.round((at - now) / 60000);
  return `Next time: ${at.toLocaleDateString(undefined, { weekday: 'long' })} at ${found[1].padStart(2, '0')}:${found[2]}, in ${Math.floor(minutes / 60)} h ${minutes % 60} min.`;
}

function choiceInput(field, modal) {
  const value = modal.values[field.key] || field.default;
  return h('div', { class: 'pills', role: 'radiogroup' }, field.options.map(option => h('button', { type: 'button', role: 'radio', 'aria-checked': String(option === value),
    class: ['pill', option === value && 'selected'], onClick: () => setValue(modal, field.key, option, { refresh: true }) },
    field.key === 'messenger' ? icon(option === store.catalog.noMessenger ? 'monitor' : 'phone', 'sm') : null, option)));
}

function choicesInput(field, modal) {
  const values = modal.values[field.key] || [];
  const toggle = option => setValue(modal, field.key, values.includes(option) ? values.filter(other => other !== option) : [...values, option], { redraw: true });
  return h('div', { class: 'pills' }, field.options.map(option => h('button', { type: 'button', class: ['pill', values.includes(option) && 'selected'], onClick: () => toggle(option) },
    h('span', { class: 'check' }, values.includes(option) ? icon('check') : null), option)));
}

function outletsInput(field, modal) {
  const chosen = modal.values[field.key] || [];
  const groups = Object.keys(store.catalog.outlets);
  modal.outletGroup = modal.outletGroup || groups[0];
  const search = modal.outletSearch.trim().toLowerCase();
  const names = search ? groups.flatMap(group => store.catalog.outlets[group]).filter((name, index, all) => name.toLowerCase().includes(search) && all.indexOf(name) === index)
    : store.catalog.outlets[modal.outletGroup];
  const toggle = name => setValue(modal, field.key, chosen.includes(name) ? chosen.filter(other => other !== name) : [...chosen, name], { redraw: true });
  const addFeed = () => {
    const url = modal.feedUrl.trim();
    if (!/^https?:\/\/\S+$/.test(url)) { modal.errors[field.key] = 'The address must start with http:// or https://.'; render(); return; }
    modal.feedUrl = '';
    if (!chosen.includes(url)) setValue(modal, field.key, [...chosen, url], { redraw: true }); else render();
  };
  return h('div', { class: 'stack' },
    h('div', { class: 'row wrap', style: { gap: '6px', minHeight: '30px' } }, chosen.length ? chosen.map(name => h('span', { class: 'badge primary' }, shorten(name, 40),
      h('button', { class: 'link-btn', type: 'button', title: `Remove ${name}`, style: { color: 'inherit' }, onClick: () => toggle(name) }, icon('x', 'sm')))) :
      h('span', { class: 'small faint' }, 'No outlet chosen yet: pick some below.')),
    h('div', { class: 'card', style: { padding: '12px', boxShadow: 'none' } },
      h('div', { class: 'input-wrap', style: { marginBottom: '10px' } }, h('span', { class: 'input-icon' }, icon('search', 'sm')),
        h('input', { class: 'input with-icon', 'data-key': 'outlet-search', placeholder: 'Search an outlet by name', value: modal.outletSearch, onInput: event => { modal.outletSearch = event.target.value; render(); } })),
      search ? null : h('div', { class: 'tabs' }, groups.map(group => h('button', { type: 'button', class: group === modal.outletGroup && 'active', onClick: () => { modal.outletGroup = group; render(); } },
        group, h('span', { class: 'faint small' }, String(store.catalog.outlets[group].filter(name => chosen.includes(name)).length || ''))))),
      h('div', { class: 'pills' }, names.length ? names.map(name => h('button', { type: 'button', class: ['pill sm', chosen.includes(name) && 'selected'], onClick: () => toggle(name) },
        h('span', { class: 'check' }, chosen.includes(name) ? icon('check') : null), name)) : h('span', { class: 'small faint' }, 'No outlet has this in its name.'))),
    h('div', { class: 'input-group' }, h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('link', 'sm')),
      h('input', { class: 'input with-icon', 'data-key': 'feed-url', placeholder: 'Or the address of any news feed or news website (https://…)', value: modal.feedUrl,
        onInput: event => { modal.feedUrl = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') addFeed(); } })),
      button('Add', { iconName: 'plus', onClick: addFeed })));
}

function publishersInput(field, modal) {
  const chosen = modal.values[field.key] || {};
  const toggle = (name, id) => {
    const next = { ...chosen };
    if (next[name]) delete next[name]; else next[name] = id;
    setValue(modal, field.key, next, { redraw: true });
  };
  const search = async () => {
    const data = await act('searchPublishers', { name: modal.publisherSearch }, { busy: 'publishers' });
    modal.publisherResults = data ? data.publishers : null;
    render();
  };
  const known = { ...store.catalog.publishers, ...chosen };
  return h('div', { class: 'stack' },
    h('div', { class: 'pills' }, Object.entries(known).map(([name, id]) => h('button', { type: 'button', class: ['pill sm', chosen[name] && 'selected'], onClick: () => toggle(name, id) },
      h('span', { class: 'check' }, chosen[name] ? icon('check') : null), name))),
    h('div', { class: 'input-group' }, h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('search', 'sm')),
      h('input', { class: 'input with-icon', 'data-key': 'publisher-search', placeholder: 'Find another publisher by its name (asks Crossref)', value: modal.publisherSearch,
        onInput: event => { modal.publisherSearch = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') search(); } })),
      button('Find', { iconName: 'search', busy: ui.busy.publishers, onClick: search })),
    modal.publisherResults ? (modal.publisherResults.length ? h('div', { class: 'browser-list' }, modal.publisherResults.map(publisher => h('button', { type: 'button',
      class: ['browser-item', chosen[publisher.name] && 'selected'], onClick: () => toggle(publisher.name, publisher.id) }, icon(chosen[publisher.name] ? 'checkCircle' : 'plus'),
      h('span', { class: 'grow' }, publisher.name), h('span', { class: 'small faint' }, `${formatNumber(publisher.papers)} works`)))) :
      h('div', { class: 'small muted' }, 'Crossref does not know a publisher with this name.')) : null);
}

function accountsInput(field, modal) {
  const rows = modal.values[field.key] || [];
  const update = (index, part, value) => { rows[index] = { ...rows[index], [part]: value }; setValue(modal, field.key, [...rows]); };
  return h('div', { class: 'stack tight' },
    rows.map((row, index) => h('div', { class: 'repeat-row' },
      h('input', { class: 'input', 'data-key': `acc-host-${index}`, placeholder: 'Website, like ieeexplore.ieee.org', value: row.host || '', onInput: event => update(index, 'host', event.target.value) }),
      h('input', { class: 'input', 'data-key': `acc-user-${index}`, placeholder: 'Username', value: row.user || '', onInput: event => update(index, 'user', event.target.value) }),
      h('input', { class: 'input', 'data-key': `acc-pass-${index}`, type: 'password', placeholder: field.saved ? 'Kept in memory' : 'Password', value: row.password || '',
        onInput: event => update(index, 'password', event.target.value) }),
      button('', { kind: 'ghost', iconName: 'trash', title: 'Remove this account', onClick: () => setValue(modal, field.key, rows.filter((_, other) => other !== index), { redraw: true }) }))),
    h('div', {}, button('Add an account', { kind: 'soft', size: 'sm', iconName: 'plus', onClick: () => setValue(modal, field.key, [...rows, { host: '', user: '', password: '' }], { redraw: true }) })));
}

function fieldWidget(field, modal) {
  switch (field.kind) {
    case 'secret': return secretInput(field, modal);
    case 'email': return textInput(field, modal, { type: 'email', placeholder: 'name@example.com' });
    case 'number': return textInput(field, modal, { type: 'number', min: 1 });
    case 'phone': return textInput(field, modal, { type: 'tel', placeholder: '+4915112345678' });
    case 'file': case 'path': case 'folder': return pathInput(field, modal);
    case 'time': return timeInput(field, modal);
    case 'command': return textInput(field, modal, { mono: true, placeholder: 'Leave empty to run the file with Python' });
    case 'choice': return choiceInput(field, modal);
    case 'choices': return choicesInput(field, modal);
    case 'outlets': return outletsInput(field, modal);
    case 'publishers': return publishersInput(field, modal);
    case 'accounts': return accountsInput(field, modal);
    default: return textInput(field, modal);
  }
}

// The checks that send nothing: the login of the email account, and the messaging app.
function checkResult(result) {
  if (!result) return null;
  return result.problem ? callout('danger', 'xCircle', result.problem) : callout('success', 'checkCircle', result.ok);
}

function emailChecks(modal) {
  const run = async () => {
    const data = await act('checkEmail', { agentId: modal.agentId, values: modal.values }, { busy: 'checkEmail', form: modal });
    modal.checks.email = data ? { problem: data.problem, ok: 'The login works. Nothing was sent.' } : null;
    render();
  };
  return h('div', { class: 'card', style: { padding: '14px', boxShadow: 'none', marginTop: '18px' } }, h('div', { class: 'row' }, icon('shield'),
    h('div', { class: 'grow' }, h('b', {}, 'Test the login'), h('div', { class: 'small muted' }, 'SwarmUP only logs in to check your password. No email is sent.')),
    button('Test now', { kind: 'soft', iconName: 'play', busy: ui.busy.checkEmail, onClick: run })), modal.checks.email ? h('div', { style: { marginTop: '10px' } }, checkResult(modal.checks.email)) : null);
}

function messengerChecks(modal) {
  const app = modal.values.messenger;
  if (!app || app === store.catalog.noMessenger) return null;
  const run = async () => {
    const data = await act('checkMessenger', { agentId: modal.agentId, values: modal.values }, { busy: 'checkMessenger', form: modal });
    modal.checks.messenger = data ? { problem: data.problem, ok: `${app} accepted the information. Nothing was sent.` } : null;
    render();
  };
  const findChats = async () => {
    const data = await act('findChats', { agentId: modal.agentId, values: modal.values }, { busy: 'findChats', form: modal });
    if (!data) return;
    if (data.chats.length === 1) { setValue(modal, 'telegramChat', data.chats[0].id); toast(`Found your chat: ${data.chats[0].name}.`, 'success'); modal.chats = null; }
    else if (!data.chats.length) toast('No message yet. Open your bot in Telegram, press Start, send it any message, then try again.', 'warning');
    else modal.chats = data.chats;
    render();
  };
  return h('div', { class: 'stack', style: { marginTop: '18px' } },
    callout('honey', 'phone', h('b', {}, `How ${app} works`), h('div', {}, store.catalog.messaging[app])),
    app === 'Telegram' ? h('div', { class: 'card', style: { padding: '14px', boxShadow: 'none' } }, h('div', { class: 'row' }, icon('search'),
      h('div', { class: 'grow' }, h('b', {}, 'Find my chat'), h('div', { class: 'small muted' }, 'Send any message to your bot first. SwarmUP then reads who wrote to it.')),
      button('Find my chat', { kind: 'soft', iconName: 'search', busy: ui.busy.findChats, onClick: findChats })),
      modal.chats ? h('div', { class: 'browser-list', style: { marginTop: '10px' } }, modal.chats.map(chat => h('button', { type: 'button', class: 'browser-item',
        onClick: () => { setValue(modal, 'telegramChat', chat.id, { redraw: true }); modal.chats = null; render(); } }, icon('user'), h('span', { class: 'grow' }, chat.name),
        h('span', { class: 'small faint' }, `chat ${chat.id}`)))) : null) : null,
    h('div', { class: 'card', style: { padding: '14px', boxShadow: 'none' } }, h('div', { class: 'row' }, icon('shield'),
      h('div', { class: 'grow' }, h('b', {}, `Test ${app}`), h('div', { class: 'small muted' }, 'Only the information is checked. Nothing is sent.')),
      button('Test now', { kind: 'soft', iconName: 'play', busy: ui.busy.checkMessenger, onClick: run })), modal.checks.messenger ? h('div', { style: { marginTop: '10px' } }, checkResult(modal.checks.messenger)) : null));
}

function agentFormModal(modal) {
  const task = taskOf(modal.task);
  const editing = !!modal.agentId;
  if (!modal.form) return modalFrame({ size: 'wide', iconEl: taskIcon(modal.task, 'lg'), title: task.label, body: loading('Preparing the questions…') });
  const visible = modal.form.fields.filter(field => field.visible);
  const save = async () => {
    const data = await act('saveAgent', { task: modal.task, agentId: modal.agentId, values: modal.values, name: modal.name, live: modal.live }, { busy: 'saveAgent', form: modal });
    if (data) {
      closeModal();
      const agent = store.state.agents.find(item => item.id === data.agentId);
      if (agent?.pending) {
        toast(`${agent.name} is ready. Now choose its model.`, 'success');
        if (!agent.model) openModelPicker(agent, { live: true });
        return;
      }
      toast(editing ? `${modal.name || 'The agent'} is updated.` : `${modal.name || 'The agent'} joined the swarm.`, 'success');
      if (data.warning) toast(data.warning, 'warning');
      return;
    }
    render();
    const first = document.querySelector('.modal .has-error');
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };
  const nameError = modal.errors.name;
  return modalFrame({
    size: 'wide', iconEl: taskIcon(modal.task, 'lg'), title: editing ? `Edit ${modal.name}` : `New agent: ${task.label}`, subtitle: `Role: ${task.role}. Fields with * are needed.`,
    body: h('div', { class: 'stack' },
      h('div', { class: ['disclosure', modal.about && 'open'] }, h('button', { type: 'button', onClick: () => { modal.about = !modal.about; render(); } }, icon('info', 'sm'), 'What this agent does', icon('chevronDown', 'sm chev')),
        modal.about ? h('div', { class: 'disclosure-body muted' }, task.info) : null),
      h('div', { class: ['field', nameError && 'has-error'] }, h('label', { class: 'field-label', for: 'agent-name' }, 'Name of the agent'),
        h('input', { id: 'agent-name', 'data-key': 'agent-name', class: 'input', value: modal.name, placeholder: modal.form.name, onInput: event => { modal.name = event.target.value; } }),
        nameError ? h('div', { class: 'field-error' }, icon('alert'), nameError) : h('div', { class: 'field-help' }, icon('info'), 'You use this name to talk to the agent. Letters, digits, dots, hyphens and underscores.')),
      h('div', { class: 'divider' }),
      h('div', {}, visible.map(field => fieldBlock(field, modal, fieldWidget(field, modal)))),
      modal.task === 'email' ? emailChecks(modal) : null,
      modal.task === 'news' ? messengerChecks(modal) : null,
      h('div', { class: ['disclosure', modal.advanced && 'open'] }, h('button', { type: 'button', onClick: () => { modal.advanced = !modal.advanced; render(); } }, icon('settings', 'sm'), 'Advanced settings',
        icon('chevronDown', 'sm chev')), modal.advanced ? h('div', { class: 'disclosure-body' }, modal.form.advanced.map(field => fieldBlock(field, modal, fieldWidget(field, modal)))) : null)),
    foot: [modal.error ? h('div', { class: 'footer-note error' }, icon('alert', 'sm'), modal.error) : null, h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal }),
      button(editing ? 'Save the changes' : 'Add to the swarm', { kind: 'primary', iconName: 'check', busy: ui.busy.saveAgent, onClick: save })],
  });
}


// ==============
// Choosing a folder or a file: the dialog of the system in the window of SwarmUP, otherwise a browser of folders drawn here.
// ==============
async function pickPath(kind, start, onPick) {
  if (store.state.nativeDialogs) {
    const data = await act('pickPath', { kind, path: start || '' }, { busy: 'pick' });
    if (data && data.path) onPick(data.path);
    return;
  }
  const modal = { type: 'browse', kind, onPick, data: null, selected: null, fileName: '', hidden: false };
  if (kind === 'path' && start) { modal.fileName = start.split(/[\\/]/).pop(); }
  openModal(modal);
  browseTo(modal, start || '');
}

async function browseTo(modal, path) {
  const data = await act('browse', { path, files: modal.kind !== 'folder', hidden: modal.hidden }, { busy: 'browse' });
  if (data) { modal.data = data.browse; modal.selected = null; }
  render();
}

function browseModal(modal) {
  const data = modal.data;
  const titles = { folder: 'Choose a folder', file: 'Choose a file', path: 'Choose where to save the file' };
  const separator = data?.separator || '/';
  const crumbs = [];
  if (data) {
    let path = data.path;
    const parts = [];
    while (path) {
      parts.unshift(path);
      const parent = path.replace(/[\\/][^\\/]*$/, '') || (path.startsWith('/') && path !== '/' ? '/' : '');
      if (parent === path || !parent) break;
      path = parent.endsWith(':') ? `${parent}${separator}` : parent;
    }
    parts.forEach(part => crumbs.push(h('button', { type: 'button', onClick: () => browseTo(modal, part) }, part.split(/[\\/]/).filter(Boolean).pop() || part), icon('chevronRight', 'sm')));
  }
  const join = (folder, name) => folder.endsWith(separator) ? folder + name : folder + separator + name;
  const chosen = modal.kind === 'folder' ? (modal.selected?.folder ? modal.selected.path : data?.path) :
    modal.kind === 'file' ? (modal.selected && !modal.selected.folder ? modal.selected.path : null) : (data && modal.fileName.trim() ? join(data.path, modal.fileName.trim()) : null);
  const pick = () => { if (!chosen) return; const onPick = modal.onPick; closeModal(); onPick(chosen); };
  return modalFrame({
    size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#F4A62A', '--task-soft': '#F4A62A26' } }, icon('folderOpen', 'lg')), title: titles[modal.kind],
    subtitle: modal.kind === 'folder' ? 'Open a folder, then choose it.' : modal.kind === 'file' ? 'Open folders until you see your file, then click it.' : 'Open the folder, then write the name of the file.',
    body: !data ? loading('Reading the folder…') : h('div', { class: 'row top', style: { gap: '16px' } },
      h('div', { class: 'stack tight', style: { width: '170px', flex: 'none' } }, h('h4', {}, 'Places'), data.places.map(place => h('button', { type: 'button', class: 'browser-item',
        style: { borderRadius: '8px', border: '0' }, onClick: () => browseTo(modal, place.path) }, icon(place.name === 'Home' ? 'home' : place.name.startsWith('Drive') ? 'layers' : 'folder', 'sm'), place.name))),
      h('div', { class: 'grow stack' },
        h('div', { class: 'row' }, button('', { kind: 'ghost', size: 'sm', iconName: 'arrowLeft', title: 'Up one folder', disabled: !data.parent, onClick: () => browseTo(modal, data.parent) }),
          h('div', { class: 'crumbs grow' }, crumbs),
          h('label', { class: ['toggle small', modal.hidden && 'on'], onClick: () => { modal.hidden = !modal.hidden; browseTo(modal, data.path); } }, h('span', { class: 'track' }), 'Hidden')),
        data.problem ? callout('warning', 'alert', data.problem) : null,
        h('div', { class: 'browser-list', style: { height: '320px' } }, data.entries.length ? data.entries.map(entry => h('button', { type: 'button',
          class: ['browser-item', entry.folder && 'is-folder', modal.selected?.path === entry.path && 'selected'], title: entry.folder ? 'Double-click to open' : null,
          onClick: () => { modal.selected = entry; if (!entry.folder && modal.kind === 'path') modal.fileName = entry.name; render(); }, onDblclick: () => entry.folder ? browseTo(modal, entry.path) : pick() },
          icon(entry.folder ? 'folder' : 'file'), h('span', { class: 'grow' }, entry.name), entry.folder ? icon('chevronRight', 'sm') : null)) :
          h('div', { class: 'empty small' }, modal.kind === 'folder' ? 'No folder inside.' : 'This folder is empty.')),
        modal.kind === 'path' ? h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Name of the file'),
          h('input', { class: 'input', 'data-key': 'browse-file', value: modal.fileName, placeholder: 'like solution.py', onInput: event => { modal.fileName = event.target.value; render(); } })) : null,
        chosen ? h('div', { class: 'small muted row' }, icon('check', 'sm'), h('span', { class: 'mono' }, chosen)) : null)),
    foot: [h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal }),
      button(modal.kind === 'folder' ? (modal.selected?.folder ? `Choose "${modal.selected.name}"` : 'Choose this folder') : 'Choose', { kind: 'primary', iconName: 'check', disabled: !chosen, onClick: pick })],
  });
}


// ==============
// Step 3: the folder of each agent.
// ==============
function viewFolders() {
  const agents = store.state.agents;
  const save = (agent, folder) => act('setFolder', { agentId: agent.id, folder }, { busy: `folder-${agent.id}`, form: folderForm(agent) }).then(data => {
    if (data) { ui.folderDrafts[agent.id] = undefined; toast(folder ? `${agent.name} works in ${folder}.` : `${agent.name} has no folder.`, 'success'); }
    render();
    return data;
  });
  const saveAll = async () => {
    for (const agent of agents) {
      const draft = ui.folderDrafts[agent.id];
      if (draft !== undefined && draft !== (agent.folder || '')) { if (!await save(agent, draft)) return; }
    }
    go('models');
  };
  return {
    header: header(stepEyebrow('folders'), 'Where does each agent work?', 'Optional. An agent can work inside a folder of your computer: it saves what it makes there, and the files it works on must be in it.'),
    content: h('div', { class: 'agent-rows' }, agents.map(agent => folderRow(agent, save))),
    footer: footer({ onClick: () => go('agents') }, { label: 'Continue to the models', onClick: saveAll }, { text: `${agents.filter(agent => agent.folder).length} of ${agents.length} with a folder` }),
    guide: [
      { q: 'Do I need folders?', text: 'No. Without a folder, an agent shows you its work here, and saves nothing on its own (the formatter and the math checker save next to your file).', tone: 'tip', icon: 'folder' },
      { q: 'What goes in it?', text: ['the email writer: a copy of every email sent', 'the calendar planner: calendar_events.ics', 'the writers: every approved text', 'the coder: its file and its test run'] },
      { q: 'Safety', text: 'An agent only writes inside its own folder, and only after you approve. The original of a document is never changed.' },
    ],
  };
}

function folderForm(agent) {
  ui.errors[`folder-${agent.id}`] = ui.errors[`folder-${agent.id}`] || {};
  return ui.errors[`folder-${agent.id}`];
}

function folderRow(agent, save) {
  const form = folderForm(agent);
  const draft = ui.folderDrafts[agent.id] ?? agent.folder ?? '';
  const changed = draft !== (agent.folder || '');
  const error = form.errors?.folder || (form.error && !form.errors?.folder ? form.error : '');
  return h('div', { class: ['card agent-row', ui.animate && 'enter'] }, taskIcon(agent.task),
    h('div', { class: 'stack' },
      h('div', { class: 'agent-row-head' }, h('span', { class: 'agent-row-name' }, agent.name), agent.isLeader ? badge('Leader', 'honey', 'crown') : null,
        h('span', { class: 'small muted' }, taskOf(agent.task).label), h('span', { class: 'grow' }),
        agent.folder ? badge('Folder set', 'success', 'check') : badge('No folder', '', null)),
      h('div', { class: 'small muted' }, agent.folderNote),
      h('div', { class: ['input-group', error && 'has-error'] },
        h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('folder', 'sm')),
          h('input', { class: 'input with-icon', 'data-key': `folder-${agent.id}`, value: draft, placeholder: 'No folder: write a path or press Browse',
            onInput: event => { ui.folderDrafts[agent.id] = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') save(agent, event.target.value); },
            onBlur: event => { if (event.target.value !== (agent.folder || '')) save(agent, event.target.value); } })),
        button('Browse…', { iconName: 'folderOpen', onClick: () => pickPath('folder', draft || agent.suggestion || '', path => { ui.folderDrafts[agent.id] = path; save(agent, path); }) }),
        agent.folder ? button('', { kind: 'ghost', iconName: 'external', title: 'Open the folder', onClick: () => act('openFolder', { path: agent.folder }) }) : null,
        agent.folder || changed ? button('', { kind: 'ghost', iconName: 'x', title: 'No folder', busy: ui.busy[`folder-${agent.id}`], onClick: () => { ui.folderDrafts[agent.id] = ''; save(agent, ''); } }) : null),
      error ? h('div', { class: 'field-error' }, icon('alert'), error) : null,
      agent.suggestion && agent.suggestion !== agent.folder ? h('div', {}, h('button', { class: 'pill sm', type: 'button', onClick: () => { ui.folderDrafts[agent.id] = agent.suggestion; save(agent, agent.suggestion); } },
        icon('sparkles', 'sm'), `Use the folder of its file: ${shorten(agent.suggestion, 60)}`)) : null));
}


// ==============
// Step 4: the model of each agent.
// ==============
function gpuMeter(gpu, extra = 0, label = 'This model') {
  if (!gpu || !gpu.gpus.length) return null;
  const total = gpu.total || 1;
  const used = Math.max(0, gpu.used);
  const swarm = Math.max(0, gpu.needed);
  const room = Math.max(0, total - used - swarm);
  const fits = extra <= room;
  const percent = value => `${Math.min(100, value / total * 100)}%`;
  return h('div', {},
    h('div', { class: 'row between small', style: { marginBottom: '7px' } }, h('b', {}, `${gpu.gpus.length === 1 ? gpu.gpus[0].name : plural(gpu.gpus.length, 'GPU')} · ${gpu.total} GB of VRAM`),
      h('span', { class: 'muted' }, `${Math.max(0, Math.round((total - used - swarm - extra) * 10) / 10)} GB left`)),
    h('div', { class: 'meter', role: 'img', 'aria-label': `VRAM: ${used} GB used by other programs, ${swarm} GB for the swarm${extra ? `, ${extra} GB for this model` : ''}, of ${total} GB` },
      h('div', { class: 'used', style: { width: percent(used) } }), h('div', { class: 'swarm', style: { width: percent(swarm) } }),
      extra ? h('div', { class: fits ? 'this' : 'over', style: { width: percent(Math.min(extra, room) || extra) } }) : null),
    h('div', { class: 'meter-legend' }, h('span', {}, h('i', { style: { background: 'var(--text-3)' } }), `Other programs: ${used} GB`),
      h('span', {}, h('i', { style: { background: 'var(--primary)' } }), `Your swarm: ${swarm} GB`),
      extra ? h('span', {}, h('i', { style: { background: fits ? 'var(--honey)' : 'var(--danger)' } }), `${label}: ${extra} GB`) : null));
}

function viewModels() {
  const state = store.state;
  const agents = state.agents;
  const missing = agents.filter(agent => !agent.model);
  const providers = [...new Set(agents.filter(agent => agent.model && !agent.model.local && agent.model.provider).map(agent => agent.model.provider))];
  const codex = agents.some(agent => agent.model?.cli === 'codex');
  return {
    header: header(stepEyebrow('models'), 'Give each agent a brain', 'The model is the AI that does the thinking. It runs on your own GPUs (free and private) or on the servers of a company (paid, through an API key).'),
    content: h('div', { class: 'stack loose' },
      state.gpu && state.gpu.gpus.length ? h('div', { class: 'card pad enter' }, h('div', { class: 'row', style: { marginBottom: '12px' } }, icon('gauge'), h('h3', {}, 'Your GPUs'),
        state.gpu.needed && !state.gpu.runnable ? badge(state.gpu.fits ? 'Not free right now' : 'Too much for your GPUs', 'warning', 'alert') : badge('The swarm fits', 'success', 'check')),
        gpuMeter(state.gpu), state.gpu.message ? h('div', { style: { marginTop: '12px' } }, callout('warning', 'alert', state.gpu.message)) : null) :
        callout('neutral', 'cloud', h('b', {}, 'No GPU was found on this computer.'), h('div', {}, 'Local models cannot run here, so choose API models. They need an API key from their company, and every use is billed to your account.')),
      h('div', { class: 'agent-rows' }, agents.map(agent => modelRow(agent))),
      providers.length ? h('div', { class: 'card pad' }, h('div', { class: 'row', style: { marginBottom: '10px' } }, icon('key'), h('h3', {}, 'API keys')),
        h('div', { class: 'stack tight' }, providers.map(provider => { const key = state.keys[provider]; return h('div', { class: 'row small' }, icon('lock', 'sm'), h('b', {}, key.company),
          h('span', { class: 'muted' }, key.source === 'environment' ? `read from ${key.variable}` : key.source === 'typed' ? 'given by you, kept in memory only' : 'missing')); }),
          codex ? h('div', { class: 'row small' }, icon('user', 'sm'), h('b', {}, 'Codex'), h('span', { class: 'muted' }, 'your ChatGPT plan, signed in through Codex')) : null)) :
        codex ? h('div', { class: 'card pad' }, h('div', { class: 'row small' }, icon('user', 'sm'), h('b', {}, 'Codex'), h('span', { class: 'muted' }, 'uses your ChatGPT plan, signed in through Codex'))) : null),
    footer: footer({ onClick: () => go('folders') }, { label: 'Continue to the teamwork', disabled: missing.length > 0, onClick: () => go('teamwork') },
      missing.length ? { text: `Choose a model for ${missing.map(agent => agent.name).join(', ')}.` } : { text: 'Every agent has a model.' }),
    guide: [
      { q: 'Local, API or coding agent?', icon: 'cpu', tone: 'tip', text: ['Local: free and private, but the model must fit in the memory (VRAM) of your GPUs.', 'API: the best models without a GPU. You pay for what you use and need a key.',
        'Coding agent: Claude Code or Codex on this computer. They can also read files and run commands, each time with your approval.'] },
      { q: 'Which one?', text: 'Start with a recommended model: they are picked for the task of the agent, from the smallest to the largest. Bigger models think better but cost more VRAM or money.' },
      { q: 'The leader', icon: 'crown', text: 'Besides its own task, the leader writes the summaries you approve. A capable model is worth it here.' },
    ],
  };
}

// The three kinds of models: on the GPUs, through an API, or a coding agent of this computer (Claude Code, Codex).
function modelKind(model) {
  return model?.cli ? 'cli' : model?.local ? 'local' : 'api';
}

function modelIcon(model) {
  return { cli: 'terminal', local: 'cpu', api: 'cloud' }[modelKind(model)];
}

function modelName(model) {
  if (!model?.cli) return model?.name || '';
  const agent = store.catalog.codingAgents[model.cli];
  return model.name === store.catalog.defaultCliModel ? `${agent.label} (its default model)` : `${agent.label} · ${model.name}`;
}

function modelRow(agent) {
  const model = agent.model;
  const missingCommand = agent.missing.length ? `pip install -U ${agent.missing.join(' ')}` : '';
  return h('div', { class: ['card agent-row', ui.animate && 'enter'] }, taskIcon(agent.task),
    h('div', { class: 'stack' },
      h('div', { class: 'agent-row-head' }, h('span', { class: 'agent-row-name' }, agent.name), agent.isLeader ? badge('Leader', 'honey', 'crown') : null,
        h('span', { class: 'small muted' }, taskOf(agent.task).label), h('span', { class: 'grow' }),
        model ? button('Change', { size: 'sm', iconName: 'refresh', onClick: () => openModelPicker(agent) }) : button('Choose a model', { kind: 'primary', size: 'sm', iconName: 'plus', onClick: () => openModelPicker(agent) })),
      model ? h('div', { class: 'row wrap' }, h('span', { class: 'model-chip' }, icon(modelIcon(model), 'sm'), h('span', {}, modelName(model))),
        model.local ? badge(`${model.vram} GB of VRAM${model.bits < 16 ? `, ${model.bits}-bit` : ''}`, 'primary') :
          model.cli === 'codex' ? badge('Your ChatGPT plan', 'honey', 'user') : model.cli ? badge('Your Anthropic API key', 'honey', 'key') : badge(model.company, 'info'),
        model.gated ? badge('Licence needed', 'warning', 'lock') : null,
        !model.local ? (agent.tested === 'ok' ? badge('Connection works', 'success', 'check') : agent.tested === 'failed' ? badge('Connection failed', 'danger', 'x') :
          button('Test the connection', { kind: 'ghost', size: 'sm', iconName: 'zap', busy: ui.busy[`test-${agent.id}`], onClick: () => testModel(agent) })) : null) :
        h('div', { class: 'small faint' }, 'No model yet.'),
      agent.missing.length ? callout('warning', 'download', h('b', {}, `This model needs ${agent.missing.join(', ')}.`), h('div', {}, 'Install them in a terminal, then check again:'), codeLine(missingCommand),
        h('div', {}, button('Check again', { size: 'sm', iconName: 'refresh', busy: ui.busy.checkPackages, onClick: () => act('checkPackages') }))) : null));
}

async function testModel(agent) {
  const data = await act('testModel', { agentId: agent.id }, { busy: `test-${agent.id}` });
  if (data) toast(data.problem ? `It did not work: ${data.problem}` : `${agent.model.name} answered: ${data.answer}`, data.problem ? 'danger' : 'success');
}

async function openModelPicker(agent, options = {}) {
  const current = agent.model;
  const modal = { type: 'model', agentId: agent.id, agentName: agent.name, task: agent.task, where: null, tab: 'recommended', family: null, provider: null, bits: current?.local ? current.bits : 16,
    catalog: null, selected: current ? { ...current } : null, apiKey: '', hfToken: '', newKey: false, prices: {}, other: { name: '', billions: '', provider: 'claude', found: null }, errors: {}, error: '',
    cli: current?.cli || 'claude-code', live: !!options.live };
  openModal(modal);
  await loadCatalog(modal);
  if (!modal.catalog) return;
  const hasGpu = modal.catalog.gpu.gpus.length > 0;
  modal.where = current ? modelKind(current) : hasGpu ? 'local' : 'api';
  if (modal.where === 'cli' && modal.cli === 'codex') loadCodex();
  render();
}

async function loadCatalog(modal) {
  const data = await act('modelCatalog', { agentId: modal.agentId, bits: modal.bits }, { busy: 'catalog' });
  if (!data) { closeModal(); return; }
  modal.catalog = data.catalog;
  if (modal.selected?.local) {
    const entry = findLocalEntry(modal, modal.selected.name);
    if (entry) modal.selected = { ...entry, local: true };
  }
  render();
}

function findLocalEntry(modal, name) {
  return Object.values(modal.catalog.families).flat().find(entry => entry.name === name) || null;
}

function localOption(modal, entry) {
  const selected = modal.selected?.local && modal.selected.name === entry.name;
  const status = { fits: badge('Fits', 'success', 'check'), busy: badge('Not free now', 'warning', 'alert'), tooBig: badge('Too big', 'danger', 'x') }[entry.status];
  return h('button', { type: 'button', class: ['model-option', selected && 'selected'], disabled: entry.status === 'tooBig', title: entry.message || null,
    onClick: () => { modal.selected = { ...entry, local: true }; modal.errors = {}; modal.reveal = entry.gated || entry.status === 'busy'; render(); } },
    h('span', { class: 'radio' }), h('div', { style: { minWidth: '0' } }, h('div', { class: 'mo-name' }, entry.name),
      h('div', { class: 'mo-sub' }, h('span', {}, `${entry.billions} billion parameters`), entry.downloaded ? h('span', {}, '· downloaded') : null, entry.gated ? h('span', {}, '· licence needed') : null)),
    h('div', { class: 'mo-side' }, badge(`${entry.vram} GB`, 'primary'), status));
}

function apiOption(modal, name, provider) {
  const selected = modal.selected && !modal.selected.local && modal.selected.name === name;
  const price = modal.prices[name];
  const company = store.catalog.providers[provider].company;
  const loadPrice = async event => {
    event.stopPropagation();
    const data = await act('price', { name, provider }, { busy: `price-${name}` });
    if (data) { modal.prices[name] = data.price; render(); }
  };
  return h('div', {},
    h('button', { type: 'button', class: ['model-option', selected && 'selected'], onClick: () => { modal.selected = { name, provider, local: false, company }; modal.errors = {}; modal.reveal = !store.state.keys[provider].source; render(); } },
      h('span', { class: 'radio' }), h('div', { style: { minWidth: '0' } }, h('div', { class: 'mo-name' }, name), h('div', { class: 'mo-sub' }, company)),
      h('div', { class: 'mo-side' }, store.state.keys[provider].source ? badge('Key ready', 'success', 'key') : null,
        h('span', { role: 'button', tabindex: '0', class: 'btn ghost sm', onClick: loadPrice, onKeydown: event => { if (event.key === 'Enter') loadPrice(event); } },
          ui.busy[`price-${name}`] ? spinner() : icon('dollar', 'sm'), 'Price'))),
    price ? priceBox(price) : null);
}

function priceBox(price) {
  if (price.error) return h('div', { class: 'price-box' }, price.error, ' ', extLink(price.page, 'Official prices'));
  return h('div', { class: 'price-box' }, h('div', {}, 'Per 1 million tokens (about 750,000 words): ', h('b', {}, `$${price.input}`), ' to read, ', h('b', {}, `$${price.output}`), ' to write',
    price.cachedInput !== null && price.cachedInput !== undefined ? `, $${price.cachedInput} for cached reading` : '', '.'),
    price.context ? h('div', {}, `It can read up to ${formatNumber(price.context)} tokens at once.`) : null, price.note ? h('div', {}, price.note) : null,
    h('div', { style: { marginTop: '4px' } }, extLink(price.page, 'Official prices')));
}

function modelPickerModal(modal) {
  const catalog = modal.catalog;
  if (!catalog) return modalFrame({ size: 'xl', iconEl: taskIcon(modal.task, 'lg'), title: `Model for ${modal.agentName}`, body: loading('Checking your GPUs and the models…') });
  const hasGpu = catalog.gpu.gpus.length > 0;
  const where = modal.where;
  const families = store.catalog.families;
  const providers = Object.keys(store.catalog.providers);
  let list;
  if (where === 'cli') list = codingAgentPanel(modal);
  else if (where === 'local') {
    if (modal.tab === 'recommended') list = h('div', { class: 'model-list' }, catalog.local.map(entry => localOption(modal, entry)));
    else if (modal.tab === 'all') {
      modal.family = modal.family || families[0];
      list = h('div', {}, h('div', { class: 'pills', style: { marginBottom: '12px' } }, families.map(family => h('button', { type: 'button', class: ['pill sm', family === modal.family && 'selected'],
        onClick: () => { modal.family = family; render(); } }, family))), h('div', { class: 'model-list' }, catalog.families[modal.family].map(entry => localOption(modal, entry))));
    } else list = otherLocal(modal);
  } else {
    if (modal.tab === 'recommended') list = h('div', { class: 'model-list' }, catalog.api.map(entry => apiOption(modal, entry.name, entry.provider)));
    else if (modal.tab === 'all') {
      modal.provider = modal.provider || providers[0];
      list = h('div', {}, h('div', { class: 'pills', style: { marginBottom: '12px' } }, providers.map(provider => h('button', { type: 'button', class: ['pill sm', provider === modal.provider && 'selected'],
        onClick: () => { modal.provider = provider; render(); } }, store.catalog.providers[provider].company))),
        h('div', { class: 'model-list' }, store.catalog.providers[modal.provider].models.map(name => apiOption(modal, name, modal.provider))));
    } else list = otherApi(modal);
  }
  const extra = modal.selected?.local ? modal.selected.vram || 0 : 0;
  const choose = async () => {
    const selected = modal.selected;
    const payload = { agentId: modal.agentId, name: selected.name, local: selected.local, bits: modal.bits, billions: selected.local && selected.custom ? selected.billions : null,
      provider: selected.provider, cli: selected.cli || null, apiKey: modal.apiKey, hfToken: modal.hfToken };
    const data = await act('chooseModel', payload, { busy: 'chooseModel', form: modal });
    if (!data) { render(); return; }
    closeModal();
    toast(`${modal.agentName} now thinks with ${modelName(selected)}.`, 'success');
    if (modal.live) openModal({ type: 'join', agentId: modal.agentId, waits: [], folder: null, errors: {}, error: '' });
    if (data.warning) toast(data.warning, 'warning');
    if (data.download && !data.download.downloaded) toast(`${selected.name} will be downloaded at the first start: about ${data.download.size} GB${data.download.free !== null ? `, and ${data.download.free} GB are free on the disk` : ''}.`, 'info');
    if (!selected.local && selected.cli !== 'codex') toast('Check that the key works with a tiny request (it costs a fraction of a cent).', 'info', { label: 'Test now', run: () => testModel(store.state.agents.find(agent => agent.id === modal.agentId)) });
  };
  return modalFrame({
    size: 'xl', iconEl: taskIcon(modal.task, 'lg'), title: `Model for ${modal.agentName}`, subtitle: `${taskOf(modal.task).label}. The recommended models suit this task.`,
    body: h('div', { class: 'stack loose' },
      catalog.isLeader ? callout('honey', 'crown', h('b', {}, 'This agent is the leader.'), h('div', {}, 'Besides its own task, its model writes the summaries you approve and decides which agents your corrections concern. A capable model is worth it.')) : null,
      h('div', { class: 'where-cards' },
        h('button', { type: 'button', class: ['choice-card', where === 'local' && 'selected'], disabled: !hasGpu, onClick: () => { modal.where = 'local'; modal.tab = 'recommended'; render(); } },
          h('span', { class: 'cc-icon' }, icon('cpu', 'lg')), h('div', {}, h('div', { class: 'cc-title' }, 'On my GPUs', badge('Free', 'success')),
            h('div', { class: 'cc-text' }, hasGpu ? `Private and free. It must fit in your ${catalog.gpu.total} GB of VRAM (${catalog.gpu.free} GB free now).` : 'No supported GPU was found on this computer.')), h('span', { class: 'radio' })),
        h('button', { type: 'button', class: ['choice-card', where === 'api' && 'selected'], onClick: () => { modal.where = 'api'; modal.tab = 'recommended'; render(); } },
          h('span', { class: 'cc-icon', style: { background: 'var(--info-soft)', color: 'var(--info-text)' } }, icon('cloud', 'lg')), h('div', {}, h('div', { class: 'cc-title' }, 'Through an API', badge('Paid', 'info')),
            h('div', { class: 'cc-text' }, 'The models of OpenAI, Anthropic, Google and DeepSeek. Every use is billed, and you need an API key.')), h('span', { class: 'radio' })),
        h('button', { type: 'button', class: ['choice-card', where === 'cli' && 'selected'], onClick: () => { modal.where = 'cli'; if (modal.cli === 'codex') loadCodex(); render(); } },
          h('span', { class: 'cc-icon', style: { background: 'var(--honey-soft)', color: 'var(--honey-text)' } }, icon('terminal', 'lg')), h('div', {}, h('div', { class: 'cc-title' }, 'A coding agent', badge('Claude Code · Codex', 'honey')),
            h('div', { class: 'cc-text' }, 'Claude Code or Codex on this computer. It can also read files and run commands, each time with your approval.')), h('span', { class: 'radio' }))),
      where === 'local' ? h('div', { class: 'card pad', style: { boxShadow: 'none' } }, gpuMeter(catalog.gpu, extra, modal.selected?.local ? shorten(modal.selected.name, 30) : 'This model'),
        h('div', { class: 'row wrap', style: { marginTop: '14px' } }, h('b', { class: 'small' }, 'Memory saver'),
          h('div', { class: 'segmented' }, store.catalog.bits.map(bits => h('button', { type: 'button', class: modal.bits === bits && 'active', onClick: () => { modal.bits = bits; loadCatalog(modal); } },
            bits === 16 ? 'Full quality' : `${bits}-bit (${bits === 8 ? 'half' : 'quarter'} the memory)`))),
          h('span', { class: 'small muted grow' }, modal.bits === 16 ? 'Compress a model to fit a smaller GPU, at a small cost in quality.' : 'Needs the bitsandbytes package.'))) : null,
      where === 'cli' ? list : where ? h('div', {}, h('div', { class: 'tabs' }, [['recommended', 'Recommended', 'sparkles'], ['all', 'All models', 'list'], ['other', where === 'local' ? 'Another model of Hugging Face' : 'Another model', 'edit']]
        .map(([tab, label, iconName]) => h('button', { type: 'button', class: modal.tab === tab && 'active', onClick: () => { modal.tab = tab; render(); } }, icon(iconName, 'sm'), label))), list) : null,
      modal.selected && where === modelKind(modal.selected) ? selectionPanel(modal) : null),
    foot: [modal.error ? h('div', { class: 'small', style: { color: 'var(--danger-text)', maxWidth: '520px' } }, modal.error) : null, h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal }),
      button('Use this model', { kind: 'primary', iconName: 'check', busy: ui.busy.chooseModel, disabled: !modal.selected || where !== modelKind(modal.selected) || modal.selected.status === 'tooBig' ||
        (modal.selected.cli === 'codex' && !ui.codex.data?.account?.signedIn) || (modal.selected.cli === 'claude-code' && modal.catalog.cli['claude-code'].missing.length > 0), onClick: choose })],
  });
}

function otherLocal(modal) {
  const other = modal.other;
  const lookup = async () => {
    const data = await act('lookupModel', { name: other.name }, { busy: 'lookup', form: modal });
    if (!data) { render(); return; }
    other.found = data.model;
    other.billions = data.model.billions || '';
    selectOther(modal);
  };
  return h('div', { class: 'stack' },
    h('div', { class: ['field', modal.errors.name && 'has-error'] }, h('label', { class: 'field-label' }, 'Name on Hugging Face'),
      h('div', { class: 'input-group' }, h('input', { class: 'input mono', 'data-key': 'other-local', placeholder: 'owner/name, like Qwen/Qwen3-8B', value: other.name, spellcheck: 'false',
        onInput: event => { other.name = event.target.value; other.found = null; }, onKeydown: event => { if (event.key === 'Enter') lookup(); } }), button('Look it up', { iconName: 'search', busy: ui.busy.lookup, onClick: lookup })),
      modal.errors.name ? h('div', { class: 'field-error' }, icon('alert'), modal.errors.name) : h('div', { class: 'field-help' }, icon('info'), 'SwarmUP asks Hugging Face for its size, to know how much VRAM it needs.')),
    other.found ? h('div', { class: ['field', modal.errors.billions && 'has-error'] }, h('label', { class: 'field-label' }, 'Billions of parameters'),
      h('input', { class: 'input', type: 'number', min: '0.1', step: '0.1', style: { maxWidth: '200px' }, 'data-key': 'other-billions', value: other.billions,
        onInput: event => { other.billions = event.target.value; selectOther(modal); } }),
      h('div', { class: 'field-help' }, icon('info'), other.found.billions ? `Hugging Face says ${other.found.billions} billion.` : 'Hugging Face does not publish its size: write it (it is often in the name, like 8B).')) : null);
}

function selectOther(modal) {
  const other = modal.other;
  const billions = parseFloat(other.billions);
  if (other.found && billions > 0) {
    const vram = Math.round(billions * modal.bits / 8 * 1.2 * 10) / 10;
    const room = modal.catalog.gpu.total - modal.catalog.gpu.needed;
    modal.selected = { name: other.found.name, local: true, custom: true, billions, vram, gated: other.found.gated, status: vram > room ? 'tooBig' : 'fits', message: '' };
  } else modal.selected = null;
  render();
}

function otherApi(modal) {
  const other = modal.other;
  const update = () => { modal.selected = other.name.trim() ? { name: other.name.trim(), provider: other.provider, local: false, company: store.catalog.providers[other.provider].company } : null; render(); };
  return h('div', { class: 'stack' },
    h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Company'), h('div', { class: 'pills' }, Object.entries(store.catalog.providers).map(([key, provider]) =>
      h('button', { type: 'button', class: ['pill', other.provider === key && 'selected'], onClick: () => { other.provider = key; update(); } }, provider.company)))),
    h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Exact name of the model'),
      h('input', { class: 'input mono', 'data-key': 'other-api', spellcheck: 'false', placeholder: `As ${store.catalog.providers[other.provider].company} writes it in its API`, value: other.name,
        onInput: event => { other.name = event.target.value; }, onChange: update, onBlur: update }),
      h('div', { class: 'field-help' }, icon('info'), 'For the newest models, that are not in the list yet.')));
}

function selectionPanel(modal) {
  const selected = modal.selected;
  if (selected.local) {
    return h('div', { class: 'card pad selection-panel', style: { boxShadow: 'none', background: 'var(--surface-2)' } },
      h('div', { class: 'row', style: { marginBottom: '10px' } }, icon('cpu'), h('h3', { class: 'grow' }, selected.name), badge(`${selected.vram} GB of VRAM`, 'primary')),
      selected.status === 'busy' ? callout('warning', 'alert', selected.message, h('div', {}, 'You can choose it, but the swarm only starts when the memory is free.')) : null,
      selected.status === 'tooBig' ? callout('danger', 'x', selected.message || 'It does not fit in your GPUs. Choose a smaller model, the memory saver, or an API model.') : null,
      selected.gated && !modal.catalog.hfToken ? h('div', { class: 'stack', style: { marginTop: '12px' } },
        callout('honey', 'lock', h('b', {}, 'This model needs a licence.'), h('div', {}, 'Accept its licence on its page first, then give a Hugging Face token (or leave it empty if you logged in with huggingface-cli).'),
          h('div', { class: 'row wrap' }, extLink(`https://huggingface.co/${selected.name}`, 'Page of the model'), extLink('https://huggingface.co/settings/tokens', 'Create a token'))),
        h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Hugging Face token', h('span', { class: 'opt' }, 'optional')),
          h('input', { class: 'input', type: 'password', 'data-key': 'hf-token', value: modal.hfToken, placeholder: 'hf_…', onInput: event => { modal.hfToken = event.target.value; } }))) : null,
      h('div', { class: 'small muted', style: { marginTop: '10px' } }, 'It is downloaded to the Hugging Face cache the first time the swarm starts (the folder of the HF_HOME environment variable).'));
  }
  if (selected.cli === 'codex') {
    const account = ui.codex.data?.account;
    return h('div', { class: 'card pad selection-panel', style: { boxShadow: 'none', background: 'var(--surface-2)' } },
      h('div', { class: 'row', style: { marginBottom: '10px' } }, icon('terminal'), h('h3', { class: 'grow' }, modelName(selected)), badge('Your ChatGPT plan', 'honey', 'user')),
      account?.signedIn ? h('div', { class: 'row small' }, icon('checkCircle', 'sm'), `Codex is signed in${account.email ? ` as ${account.email}` : ''}. Its use counts in the limits of your plan.`) :
        h('div', { class: 'small muted' }, 'Sign in to Codex above to use it.'));
  }
  const key = store.state.keys[selected.provider];
  const askKey = !key.source || modal.newKey;
  return h('div', { class: 'card pad selection-panel', style: { boxShadow: 'none', background: 'var(--surface-2)' } },
    h('div', { class: 'row', style: { marginBottom: '10px' } }, icon(modelIcon(selected)), h('h3', { class: 'grow' }, modelName(selected)), badge(key.company, 'info')),
    selected.cli ? h('div', { class: 'small muted', style: { marginBottom: '10px' } }, 'Claude Code is billed through your Anthropic API key, like the API models. Anthropic does not allow other programs to use a Claude subscription, so your Claude plan cannot be used here.') : null,
    !askKey ? h('div', { class: 'row small' }, icon('checkCircle', 'sm'), h('span', { class: 'grow' }, key.source === 'environment' ? `Using the API key of ${key.variable}.` : 'Using the API key you gave earlier (kept in memory only).'),
      h('button', { class: 'link-btn', type: 'button', onClick: () => { modal.newKey = true; render(); } }, 'Use another key')) :
      h('div', { class: ['field', modal.errors.apiKey && 'has-error'] }, h('label', { class: 'field-label' }, `Your ${key.company} API key`, h('span', { class: 'req' }, '*')),
        h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('key', 'sm')), h('input', { class: 'input with-icon', type: 'password', 'data-key': 'api-key', autocomplete: 'off',
          value: modal.apiKey, placeholder: 'Paste it here', onInput: event => { modal.apiKey = event.target.value; } })),
        modal.errors.apiKey ? h('div', { class: 'field-error' }, icon('alert'), modal.errors.apiKey) : null,
        h('div', { class: 'field-help' }, icon('lock'), h('span', {}, 'Only kept in memory while SwarmUP runs, never saved. ', extLink(key.page, 'Create a key'), ` Next time, set ${key.variable} instead of typing it.`))));
}


// The coding agents: which one, whether it is installed, the sign-in of Codex, and the model it uses.
function codingAgentPanel(modal) {
  const agents = store.catalog.codingAgents;
  const pick = cli => { modal.cli = cli; modal.selected = null; if (cli === 'codex') loadCodex(); render(); };
  const cards = h('div', { class: 'where-cards two' }, [['claude-code', 'Anthropic API key', 'Claude Code by Anthropic. Billed per use through your Anthropic API key.'],
    ['codex', 'ChatGPT plan', 'Codex by OpenAI. Uses your ChatGPT plan: sign in once with your ChatGPT account.']].map(([cli, tag, text]) =>
    h('button', { type: 'button', class: ['choice-card', modal.cli === cli && 'selected'], onClick: () => pick(cli) }, h('span', { class: 'cc-icon', style: { background: 'var(--honey-soft)', color: 'var(--honey-text)' } }, icon('terminal', 'lg')),
      h('div', {}, h('div', { class: 'cc-title' }, agents[cli].label, badge(tag, 'outline')), h('div', { class: 'cc-text' }, text)), h('span', { class: 'radio' }))));
  const option = (cli, name, title, description, isDefault) => {
    const selected = modal.selected?.cli === cli && modal.selected.name === name;
    return h('button', { type: 'button', class: ['model-option', selected && 'selected'], onClick: () => {
      modal.selected = { cli, name, local: false, provider: agents[cli].provider, company: agents[cli].company }; modal.errors = {}; modal.reveal = true; render(); } },
      h('span', { class: 'radio' }), h('div', { style: { minWidth: '0' } }, h('div', { class: 'mo-name' }, title), description ? h('div', { class: 'mo-sub' }, description) : null),
      h('div', { class: 'mo-side' }, isDefault ? badge('Recommended', 'success') : null));
  };
  const permissions = callout('neutral', 'shield', h('b', {}, 'You stay in control.'), h('div', {}, 'It reads the files of its folder freely. Anything else (a command, a change of a file, a web page, a file outside its folder) waits for your approval in the conversation: allow it once, until the next run, or deny it.'));
  if (modal.cli === 'claude-code') {
    const missing = modal.catalog.cli['claude-code'].missing;
    return h('div', { class: 'stack' }, cards,
      missing.length ? callout('warning', 'download', h('b', {}, 'Claude Code needs its Python library.'), h('div', {}, 'It contains Claude Code itself. Install it in a terminal, then check again:'),
        codeLine(`pip install -U ${missing.join(' ')}`), h('div', {}, button('Check again', { size: 'sm', iconName: 'refresh', busy: ui.busy.catalog, onClick: () => loadCatalog(modal) }))) :
        h('div', { class: 'model-list' }, agents['claude-code'].models.map(name => name === store.catalog.defaultCliModel ? option('claude-code', name, 'Let Claude Code choose', 'Its default model.', true) :
          option('claude-code', name, name, 'Anthropic', false))),
      permissions);
  }
  return h('div', { class: 'stack' }, cards, codexAccountPanel(), ui.codex.data?.account?.signedIn ? h('div', { class: 'model-list' },
    option('codex', store.catalog.defaultCliModel, 'Let Codex choose', 'Its default model for your plan.', true),
    ...(ui.codex.data.models || []).map(model => option('codex', model.id, model.name, model.description, false))) : null, permissions);
}

async function loadCodex() {
  if (ui.codex.loading) return;
  ui.codex.loading = true;
  render();
  try {
    ui.codex.data = (await api('codexAccount')).codex;
  } catch (error) {
    ui.codex.data = { problem: error.message, account: null, models: [] };
  }
  ui.codex.loading = false;
  render();
}

// Where Codex stands: installed or not, the right version or not, signed in or not, and the sign-in itself while it happens.
function codexAccountPanel() {
  const data = ui.codex.data;
  if (ui.codex.loading && !data) return loading('Asking Codex who is signed in…');
  if (!data) return null;
  if (data.problem) {
    const command = (data.problem.match(/npm install -g \S+/) || [])[0];
    return callout('warning', 'alert', h('b', {}, 'Codex cannot be used yet.'), h('div', {}, data.problem), command ? codeLine(command) : null,
      h('div', {}, button('Check again', { size: 'sm', iconName: 'refresh', busy: ui.codex.loading, onClick: loadCodex })));
  }
  if (data.account?.signedIn) {
    const who = data.account.type === 'apiKey' ? 'with an OpenAI API key (billed per use)' : `as ${data.account.email || 'your ChatGPT account'}${data.account.plan ? ` · ${data.account.plan} plan` : ''}`;
    return callout('success', 'checkCircle', h('b', {}, `Codex is signed in ${who}.`), h('div', {}, `Codex ${data.version} on this computer.`));
  }
  return codexSignIn();
}

function codexSignIn() {
  const login = store.state.codexLogin;
  const start = method => act('codexSignIn', { method }, { busy: `codex-${method}` });
  const cancel = () => act('codexCancel', {}, { busy: 'codexCancel' });
  if (login?.state === 'waiting' && login.method === 'code') {
    return h('div', { class: 'card pad sign-in' }, h('div', { class: 'row' }, spinner(), h('h3', {}, 'Type this code on the page of OpenAI')),
      h('div', { class: 'device-code' }, login.code, button('', { kind: 'ghost', size: 'sm', iconName: 'copy', title: 'Copy the code', onClick: () => copyText(login.code) })),
      h('ol', { class: 'steps small' }, h('li', {}, 'Open the page: ', extLink(login.url, login.url)), h('li', {}, 'Sign in with your ChatGPT account.'), h('li', {}, 'Type the code above. SwarmUP goes on by itself as soon as Codex is signed in.')),
      h('div', { class: 'small faint' }, 'If OpenAI refuses the code, turn on the sign-in with a code in the security settings of ChatGPT first, or use the browser sign-in.'),
      h('div', { class: 'row' }, button('Cancel', { kind: 'ghost', size: 'sm', busy: ui.busy.codexCancel, onClick: cancel })));
  }
  if (login?.state === 'waiting') {
    return h('div', { class: 'card pad sign-in' }, h('div', { class: 'row' }, spinner(), h('h3', {}, 'Sign in on the page that opened in your browser')),
      h('p', { class: 'small muted' }, 'Sign in with your ChatGPT account and accept. The page then tells Codex on this computer, and SwarmUP goes on by itself.'),
      h('div', { class: 'row wrap' }, extLink(login.url, 'The page did not open? Open it'), button('Use a code instead', { kind: 'ghost', size: 'sm', busy: ui.busy['codex-code'], onClick: () => start('code') }),
        button('Cancel', { kind: 'ghost', size: 'sm', busy: ui.busy.codexCancel, onClick: cancel })));
  }
  return h('div', { class: 'card pad sign-in' }, h('div', { class: 'row' }, icon('user'), h('h3', {}, 'Sign in to Codex with ChatGPT')),
    h('p', { class: 'small muted' }, 'Codex uses your ChatGPT plan. You sign in on the page of OpenAI: SwarmUP never sees your password, and Codex keeps the sign-in like it does for itself.'),
    login?.state === 'failed' ? callout('danger', 'xCircle', login.error) : null,
    h('div', { class: 'row wrap' }, button('Sign in with ChatGPT', { kind: 'primary', iconName: 'external', busy: ui.busy['codex-browser'], onClick: () => start('browser') }),
      button('Use a code instead', { kind: 'ghost', busy: ui.busy['codex-code'], onClick: () => start('code'), title: 'For when the browser cannot come back to this computer' })));
}


// ==============
// Step 5: who waits for whom.
// ==============
function viewTeamwork() {
  const state = store.state;
  const agents = state.agents;
  const workers = agents.slice(1);
  const choice = ui.orderChoice || state.order;
  const job = state.jobs.order;
  const leaderBusy = job?.state === 'running';
  const pick = async order => {
    ui.orderChoice = order;
    ui.errors.order = '';
    if (order === 'together') { ui.waitsDraft = null; await act('setOrder', { order }, { ok: 'Everybody works at the same time.' }); }
    if (order === 'custom') { ui.waitsDraft = Object.fromEntries(workers.map(agent => [agent.name, [...agent.waitsFor]])); render(); }
    if (order === 'leader') render();
  };
  const toggleWait = async (name, other) => {
    const waits = ui.waitsDraft;
    waits[name] = waits[name].includes(other) ? waits[name].filter(item => item !== other) : [...waits[name], other];
    const form = {};
    const data = await act('setOrder', { order: 'custom', waits }, { form, busy: 'order' });
    ui.errors.order = data ? '' : form.error;
    render();
  };
  const options = [
    ['together', 'zap', 'All at the same time', 'Every agent starts right away. The fastest, when the tasks do not depend on each other.'],
    ['leader', 'crown', 'The leader decides', 'The leader reads the tasks and proposes who waits for whom. You approve its proposal.'],
    ['custom', 'workflow', 'I choose', 'You decide which agents wait for others. An agent that waits receives their results.'],
  ];
  return {
    header: header(stepEyebrow('teamwork'), 'How do the agents work together?', 'Agents work at the same time, unless one needs the result of another: then it waits for it and receives what it made. The leader always works last.'),
    content: h('div', { class: 'stack loose' },
      workers.length === 0 ? callout('info', 'info', h('b', {}, 'Your swarm has one agent.'), h('div', {}, 'Nobody waits for anybody. Add agents to make them work together.')) :
        h('div', { class: 'choice-cards enter' }, options.map(([value, iconName, title, text]) => h('button', { type: 'button', class: ['choice-card', choice === value && 'selected'], disabled: leaderBusy,
          onClick: () => pick(value) }, h('span', { class: 'cc-icon' }, icon(iconName, 'lg')), h('div', {}, h('div', { class: 'cc-title' }, title), h('div', { class: 'cc-text' }, text)), h('span', { class: 'radio' })))),
      choice === 'leader' && workers.length ? h('div', { class: 'card pad enter' }, leaderBusy ? h('div', { class: 'row' }, spinner(), h('div', { class: 'grow' }, h('b', {}, `${agents[0].name} is thinking about the order…`),
        h('div', { class: 'small muted' }, 'Its proposal appears in a window for your approval.'))) :
        h('div', { class: 'row' }, h('span', { class: 'task-icon', style: { '--task': '#F4A62A', '--task-soft': '#F4A62A26' } }, icon('crown')), h('div', { class: 'grow' }, h('b', {}, `Ask ${agents[0].name} to propose an order`),
          h('div', { class: 'small muted' }, 'It uses its model, so it can take a moment (and a few tokens for an API model).')),
          button('Ask the leader', { kind: 'primary', iconName: 'sparkles', busy: ui.busy.planOrder, onClick: () => act('planOrder') }))) : null,
      choice === 'custom' && ui.waitsDraft && workers.length ? h('div', { class: 'card enter' }, h('div', { class: 'card-head' }, icon('workflow'), h('h3', { class: 'grow' }, 'Who waits for whom?'),
        ui.busy.order ? spinner() : null),
        h('div', { class: 'card-body stack' }, workers.map(agent => {
          const others = workers.filter(other => other.name !== agent.name);
          return h('div', { class: 'row top wrap' }, h('div', { class: 'row', style: { width: '210px' } }, taskIcon(agent.task, 'sm'), h('b', {}, agent.name), h('span', { class: 'muted small' }, 'waits for')),
            h('div', { class: 'pills grow' }, others.length ? others.map(other => { const on = ui.waitsDraft[agent.name].includes(other.name); return h('button', { type: 'button',
              class: ['pill sm', on && 'selected'], onClick: () => toggleWait(agent.name, other.name) }, h('span', { class: 'check' }, on ? icon('check') : null), other.name); }) :
              h('span', { class: 'small faint' }, 'Nobody else to wait for.')));
        }), ui.errors.order ? callout('danger', 'alert', ui.errors.order) : null)) : null,
      h('div', { class: 'card enter-2' }, h('div', { class: 'card-head' }, icon('activity'), h('h3', { class: 'grow' }, 'The plan of your swarm'), graphLegend()),
        h('div', { class: 'card-body' }, flowGraph(builderNodes(), builderStages(), agents[0]?.name, { builder: true })))),
    footer: footer({ onClick: () => go('models') }, { label: 'Continue to the launch', disabled: leaderBusy || !!ui.errors.order, onClick: () => go('launch') }),
    guide: [
      { q: 'When should an agent wait?', icon: 'workflow', tone: 'tip', text: 'When it needs what another one makes. Example: the email writer waits for the literature reviewer, to send its survey.' },
      { q: 'Waiting in a circle', text: 'Two agents cannot wait for each other: none of them could start. SwarmUP tells you if that happens.' },
      { q: 'The leader', icon: 'crown', text: 'The leader always works last and receives the results of everyone, so it does not appear in the choices.' },
    ],
  };
}

function builderNodes() {
  return store.state.agents.map(agent => ({ name: agent.name, task: agent.task, role: taskOf(agent.task).role, waitsFor: agent.isLeader ? [] : agent.waitsFor, model: agent.model, isLeader: agent.isLeader }));
}

// The groups of agents that start together, from who waits for whom (like Swarm.getStages).
function builderStages() {
  const workers = store.state.agents.slice(1);
  const done = new Set(), stages = [];
  while (done.size < workers.length) {
    const stage = workers.filter(agent => !done.has(agent.name) && agent.waitsFor.every(name => done.has(name))).map(agent => agent.name);
    if (!stage.length) { stages.push(workers.filter(agent => !done.has(agent.name)).map(agent => agent.name)); break; }
    stages.push(stage);
    stage.forEach(name => done.add(name));
  }
  return stages;
}

function graphLegend() {
  return h('div', { class: 'legend' }, h('span', {}, h('i', {}), 'waits for'), h('span', {}, h('i', { class: 'report' }), 'reports to the leader'), h('span', {}, h('i', { class: 'user' }), 'speaks to you'));
}


// ==============
// The map of the swarm: the groups of agents that start together from left to right, then the leader, then you.
// ==============
function nodeStatus(node, mode) {
  if (node.builder) {
    if (node.isLeader) return { label: 'Works last', tone: 'attention', iconName: 'crown' };
    return node.waitsFor.length ? { label: `After ${node.waitsFor.join(', ')}`, tone: 'info', iconName: 'clock' } : { label: 'Starts right away', tone: 'primary', iconName: 'zap' };
  }
  if (node.status === 'failed') return { label: node.review === 'rejected' ? 'Rejected' : 'Did not finish', tone: 'danger', iconName: 'x' };
  if (node.status === 'paused') return { label: 'Paused: no connection', tone: 'warning', iconName: 'wifiOff' };
  if (node.startAt) return { label: `Starts at ${node.startAt.slice(11)}`, tone: 'info', iconName: 'clock' };
  if (node.review === 'ready') return { label: mode === 'plan' ? 'Plan waits for you' : 'Waits for your OK', tone: 'attention', iconName: 'bell' };
  if (node.status === 'done') return { label: mode === 'plan' ? 'Plan approved' : 'Done', tone: 'success', iconName: 'check' };
  if (node.status === 'working') return { label: node.review === 'approved' ? 'Approved, working' : mode === 'plan' ? 'Writing its plan' : 'Working', tone: 'working', live: true };
  if (node.waitingOn?.length) return { label: `Waits for ${node.waitingOn.join(', ')}`, tone: '', iconName: 'hourglass' };
  return { label: 'Waiting to start', tone: '', iconName: 'hourglass' };
}

function statusPill(status) {
  return h('span', { class: ['status', status.tone] }, status.live ? h('span', { class: 'dot live' }) : icon(status.iconName || 'info'), h('span', {}, status.label));
}

function graphSize(columns, vertical) {
  const tallest = Math.max(1, ...columns.map(column => column.length));
  const across = NODE.pad * 2 + tallest * NODE.width + (tallest - 1) * NODE.gapY;
  const along = NODE.pad * 2 + columns.length * (NODE.width + NODE.gapX) + NODE.user;
  return vertical ? across : along;
}

// The room a map has: the width of the page, without the column of the conversation in the live view.
function graphRoom(live) {
  const content = document.getElementById('content');
  return (content?.clientWidth || 900) - (live ? 420 + 60 : 68) - 30;
}

function flowGraph(nodes, stages, leader, options = {}) {
  const byName = Object.fromEntries(nodes.map(node => [node.name, node]));
  const columns = [...stages.filter(stage => stage.length), leader ? [leader] : []].filter(column => column.length);
  const vertical = graphSize(columns, false) > graphRoom(!options.builder) * 1.15 && graphSize(columns, true) < graphSize(columns, false);
  const tallest = Math.max(1, ...columns.map(column => column.length));
  const positions = {};
  let width, height, user;
  if (vertical) {
    const rowGap = 54;
    width = graphSize(columns, true);
    columns.forEach((row, index) => {
      const rowWidth = row.length * NODE.width + (row.length - 1) * NODE.gapY;
      row.forEach((name, place) => { positions[name] = { x: (width - rowWidth) / 2 + place * (NODE.width + NODE.gapY), y: NODE.pad + index * (NODE.height + rowGap) }; });
    });
    user = { x: (width - NODE.user) / 2, y: NODE.pad + columns.length * (NODE.height + rowGap) };
    height = user.y + 100 + NODE.pad;
  } else {
    height = NODE.pad * 2 + tallest * NODE.height + (tallest - 1) * NODE.gapY;
    columns.forEach((column, index) => {
      const columnHeight = column.length * NODE.height + (column.length - 1) * NODE.gapY;
      column.forEach((name, row) => { positions[name] = { x: NODE.pad + index * (NODE.width + NODE.gapX), y: (height - columnHeight) / 2 + row * (NODE.height + NODE.gapY) }; });
    });
    user = { x: NODE.pad + columns.length * (NODE.width + NODE.gapX), y: height / 2 - 50 };
    width = user.x + NODE.user + NODE.pad;
  }
  const exit = name => vertical ? [positions[name].x + NODE.width / 2, positions[name].y + NODE.height] : [positions[name].x + NODE.width, positions[name].y + NODE.height / 2];
  const entry = name => vertical ? [positions[name].x + NODE.width / 2, positions[name].y] : [positions[name].x, positions[name].y + NODE.height / 2];
  const waitedBy = new Set(nodes.flatMap(node => node.isLeader ? [] : node.waitsFor));
  let paths = '';
  for (const node of nodes) {
    if (node.isLeader || !positions[node.name]) continue;
    for (const other of node.waitsFor) {
      if (!positions[other]) continue;
      const flowing = !options.builder && byName[other]?.status === 'done' && node.status === 'waiting';
      paths += `<path class="edge waits${flowing ? ' flowing' : ''}" d="${curve(...exit(other), ...entry(node.name))}"/>`;
    }
    if (!waitedBy.has(node.name) && leader && positions[leader]) paths += `<path class="edge report" d="${curve(...exit(node.name), ...entry(leader))}"/>`;
  }
  if (leader && positions[leader]) paths += `<path class="edge user" d="${curve(...exit(leader), ...(vertical ? [user.x + NODE.user / 2, user.y] : [user.x, user.y + 50]))}"/>`;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'edges');
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.innerHTML = paths;
  const graph = h('div', { class: 'graph', style: { width: `${width}px`, height: `${height}px` }, dataset: { graph: options.builder ? 'builder' : 'live' } }, svg,
    nodes.filter(node => positions[node.name]).map(node => {
      const position = positions[node.name];
      const status = nodeStatus({ ...node, builder: options.builder }, options.mode);
      return h('button', { type: 'button', class: ['node', node.isLeader && 'leader', status.tone === 'attention' && !options.builder && 'attention', status.tone === 'danger' && 'failed',
        status.tone === 'success' && !options.builder && 'done', options.selected === node.name && 'selected'], style: { left: `${position.x}px`, top: `${position.y}px`, height: `${NODE.height}px` },
        dataset: { name: node.name }, title: options.builder ? 'Click to edit this agent' : 'Click to look at this agent',
        onClick: () => options.onSelect ? options.onSelect(node.name) : openAgentForm(node.task, store.state.agents.find(agent => agent.name === node.name)?.id) },
        h('div', { class: 'row', style: { gap: '9px' } }, taskIcon(node.task, 'sm'), h('div', { style: { minWidth: '0' } }, h('div', { class: 'node-name' }, node.name, node.isLeader ? h('span', { class: 'leader-mark' }, icon('crown', 'sm')) : null),
          h('div', { class: 'node-role' }, node.role))),
        h('div', { class: 'node-model' }, node.model ? [icon(modelIcon(node.model)), shorten(modelName(node.model), 30)] : [icon('alert'), 'No model yet']),
        statusPill(status));
    }),
    h('div', { class: 'node user-node', style: { left: `${user.x}px`, top: `${user.y}px` } }, h('span', { class: 'user-avatar' }, icon('user')), h('b', {}, 'You'), h('span', { class: 'small muted' }, 'approve everything')));
  return h('div', { class: 'graph-scroll' }, graph);
}

// A line between two points, curved along the way the map is read.
function curve(x1, y1, x2, y2) {
  if (Math.abs(y2 - y1) > Math.abs(x2 - x1)) {
    const dy = (y2 - y1) / 2;
    return `M${x1},${y1} C${x1},${y1 + dy} ${x2},${y2 - dy} ${x2},${y2}`;
  }
  const dx = Math.max(36, (x2 - x1) / 2);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

// A map wider than its card is shrunk to fit, so the whole swarm is always visible.
function fitGraphs() {
  document.querySelectorAll('.graph-scroll').forEach(holder => {
    const graph = holder.querySelector('.graph');
    const width = parseFloat(graph.style.width), height = parseFloat(graph.style.height);
    const scale = Math.max(0.55, Math.min(1, (holder.clientWidth - 8) / width));
    graph.style.transform = scale < 1 ? `scale(${scale})` : '';
    graph.style.transformOrigin = 'top left';
    graph.style.margin = scale < 1 ? '0' : '0 auto';
    holder.style.height = `${Math.ceil(height * scale) + 22}px`;
  });
}

// A dot runs along the line between two agents when one sends a message to the other.
function drawParticles() {
  const particles = ui.particles;
  ui.particles = [];
  const graph = document.querySelector('[data-graph="live"]');
  if (!graph || !particles.length) return;
  const svg = graph.querySelector('svg.edges');
  const nodes = {};
  graph.querySelectorAll('.node[data-name]').forEach(node => { nodes[node.dataset.name] = node; });
  for (const { from, to } of particles.slice(-6)) {
    const a = nodes[from], b = nodes[to];
    if (!a || !b) continue;
    const center = node => [node.offsetLeft + node.offsetWidth / 2, node.offsetTop + node.offsetHeight / 2];
    const [ax, ay] = center(a), [bx, by] = center(b);
    const path = Math.abs(by - ay) > Math.abs(bx - ax)
      ? curve(ax, ay + Math.sign(by - ay) * a.offsetHeight / 2, bx, by - Math.sign(by - ay) * b.offsetHeight / 2)
      : curve(ax + Math.sign(bx - ax) * a.offsetWidth / 2, ay, bx - Math.sign(bx - ax) * b.offsetWidth / 2, by);
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('r', '5');
    dot.setAttribute('class', 'particle');
    dot.innerHTML = `<animateMotion dur="1.1s" repeatCount="1" fill="freeze" path="${path}"/>`;
    svg.append(dot);
    setTimeout(() => dot.remove(), 1200);
  }
}


// ==============
// Step 6: the launch.
// ==============
function viewLaunch() {
  const state = store.state;
  const agents = state.agents;
  const mode = ui.mode || state.mode;
  const missingModels = agents.filter(agent => !agent.model);
  const missingPackages = agents.filter(agent => agent.missing.length);
  const gpu = state.gpu;
  const checks = [
    { ok: !!state.mission, text: state.mission ? `Mission: ${shorten(state.mission, 90)}` : 'Write the mission', fix: () => go('mission') },
    state.buildMode === 'leader' && agents.length <= 1 ? { ok: false, text: 'The leader did not build the swarm yet', fix: () => go('mission') } :
      { ok: agents.length > 0, text: `${plural(agents.length, 'agent')}, led by ${agents[0]?.name || 'nobody'}${state.buildMode === 'leader' ? ', which built the swarm and can propose changes while it runs' : ''}`, fix: () => go('agents') },
    { ok: missingModels.length === 0, text: missingModels.length ? `No model for ${missingModels.map(agent => agent.name).join(', ')}` : 'Every agent has a model', fix: () => go('models') },
    missingPackages.length ? { warn: true, text: `Packages to install for ${missingPackages.map(agent => agent.name).join(', ')}: ${[...new Set(missingPackages.flatMap(agent => agent.missing))].join(', ')}`, fix: () => go('models') } : null,
    gpu && gpu.needed ? { ok: gpu.runnable, warn: !gpu.runnable, text: gpu.runnable ? `The local models need ${gpu.needed} GB of VRAM, and ${gpu.free} GB are free` : gpu.message, fix: () => go('models') } : null,
  ].filter(Boolean);
  const start = async () => {
    const data = await act('start', { mode }, { busy: 'start' });
    if (data) { ui.selected = null; store.feed = []; go('run'); }
  };
  const setMode = value => { ui.mode = value; act('setMode', { mode: value }, { quiet: true }); };
  return {
    header: header(stepEyebrow('launch'), 'Ready to launch', 'Look at your swarm one last time, choose how it starts, and launch it. You follow it live, and nothing is done before you approve it.'),
    content: h('div', { class: 'stack loose' },
      h('div', { class: 'card enter' }, h('div', { class: 'card-head' }, icon('activity'), h('h3', { class: 'grow' }, 'Your swarm'), graphLegend()),
        h('div', { class: 'card-body' }, flowGraph(builderNodes(), builderStages(), agents[0]?.name, { builder: true }))),
      h('div', { class: 'stack enter-2' }, h('h2', {}, 'How must it start?'),
        h('div', { class: 'choice-cards' },
          h('button', { type: 'button', class: ['choice-card', mode === 'plan' && 'selected'], onClick: () => setMode('plan') }, h('span', { class: 'cc-icon' }, icon('list', 'lg')),
            h('div', {}, h('div', { class: 'cc-title' }, 'Plan first', badge('Recommended', 'success')), h('div', { class: 'cc-text' }, 'Every agent writes its plan, the leader summarises them, and you approve. Then you execute the approved plans in one click.')), h('span', { class: 'radio' })),
          h('button', { type: 'button', class: ['choice-card', mode === 'execute' && 'selected'], onClick: () => setMode('execute') }, h('span', { class: 'cc-icon' }, icon('play', 'lg')),
            h('div', {}, h('div', { class: 'cc-title' }, 'Execute right away'), h('div', { class: 'cc-text' }, 'Every agent drafts its work right away, and you approve each result before it acts (sends, books, saves…).')), h('span', { class: 'radio' })))),
      h('div', { class: 'card pad enter-3' }, h('div', { class: 'row', style: { marginBottom: '12px' } }, icon('checkCircle'), h('h3', {}, 'Checklist')),
        h('div', { class: 'stack tight' }, checks.map(check => h('div', { class: 'row small' }, h('span', { style: { color: check.ok ? 'var(--success)' : check.warn ? 'var(--warning)' : 'var(--danger)' } },
          icon(check.ok ? 'checkCircle' : check.warn ? 'alert' : 'xCircle')), h('span', { class: 'grow' }, check.text), !check.ok ? h('button', { class: 'link-btn', type: 'button', onClick: check.fix }, 'Fix') : null)))),
      callout('success', 'shield', h('b', {}, 'Nothing is done without you.'), h('div', {}, 'Emails are only sent, events booked and files written after you approve the exact result. You can message any agent while it works, and stop the swarm at any time.'))),
    footer: footer({ onClick: () => go('teamwork') }, { label: 'Start the swarm', iconName: 'rocket', size: 'lg', disabled: missingModels.length > 0 || (state.buildMode === 'leader' && agents.length <= 1),
      busy: ui.busy.start, onClick: start }),
    guide: [
      { q: 'Plan or execute?', icon: 'list', tone: 'tip', text: 'Plan first is the safest: you see what every agent intends to do before anything happens, and correct it in your own words.' },
      { q: 'While it runs', text: ['the map shows who works and who waits', 'a golden light means an agent waits for you', 'the leader asks you questions on the right'] },
      { q: 'If something stops', text: 'If the internet or the computer stops, your swarm is saved. Start SwarmUP again to continue where it was.' },
    ],
  };
}


// ==============
// The live swarm: the map, the agent you look at, and the conversation with the leader.
// ==============
function runTitle(run) {
  const titles = {
    running: run.interruption ? ['Paused: the connection was lost', 'warning', 'wifiOff'] : (run.ready.length || store.state.questions.length) ? ['Waits for you', 'attention', 'bell'] : ['Running', 'primary', 'activity'],
    succeeded: [run.mode === 'plan' ? 'Plans approved' : 'Finished', 'success', 'checkCircle'], unfinished: ['Ended without a result', 'warning', 'alert'],
    stopped: ['Stopped', 'warning', 'stop'], error: ['Could not start', 'danger', 'xCircle'],
  };
  return titles[run.state] || titles.running;
}

function viewRun() {
  const state = store.state, run = state.run;
  if (!run) return viewHome();
  const [label, tone, iconName] = runTitle(run);
  const agents = run.agents;
  const selectedName = agents.some(agent => agent.name === ui.selected) ? ui.selected : run.ready[0] || run.leader;
  const selected = agents.find(agent => agent.name === selectedName);
  const nodes = agents.map(agent => ({ ...agent, role: agent.role, waitsFor: agent.isLeader ? [] : agent.waitsFor }));
  return {
    header: h('div', { class: 'grow' }, h('div', { class: 'eyebrow' }, run.resumed ? 'Continued swarm' : run.mode === 'plan' ? 'Plan mode' : 'Execute mode'),
      h('div', { class: 'row wrap' }, h('h1', { style: { fontSize: '22px' } }, shorten(run.mission, 110)), statusPill({ label, tone, iconName, live: run.running && tone === 'primary' }),
        h('span', { class: 'badge outline timer', dataset: { start: run.startedAt || '', end: run.finishedAt || '' } }, icon('clock'), elapsed(parseTime(run.startedAt), parseTime(run.finishedAt))))),
    flush: true,
    content: h('div', { class: 'run' },
      h('div', { class: 'run-left', 'data-scroll': 'run-left' },
        h('div', { class: 'run-bar' }, h('span', { class: 'grow' }),
          button('Add an agent', { size: 'sm', iconName: 'plus', disabled: !run.canJoin, onClick: () => openModal({ type: 'tasks', live: true }),
            title: run.canJoin ? 'A new agent joins the swarm now, and every agent is told' : 'The leader started its final work: an agent can join when the run is over' }),
          run.running ? button('Stop the swarm', { kind: 'danger-ghost', size: 'sm', iconName: 'stop', onClick: () => openModal({ type: 'stop' }) }) : null,
          !run.running ? button('Edit the swarm', { size: 'sm', iconName: 'edit', onClick: () => go('launch') }) : null,
          !run.running ? button('New swarm', { size: 'sm', iconName: 'plus', onClick: () => openModal({ type: 'confirmNew' }) }) : null),
        run.interruption ? h('div', { class: 'banner warning' }, h('span', { class: 'banner-icon' }, icon('wifiOff', 'lg')),
          h('div', { class: 'grow' }, h('h3', {}, 'The swarm lost its connection and is paused'), h('div', { class: 'small' }, 'Everything done so far is saved. Answer the leader on the right: try again, or cancel.'),
            h('ul', { class: 'small', style: { margin: '6px 0 0', paddingLeft: '18px' } }, Object.entries(run.interruption).map(([name, reason]) => h('li', {}, h('b', {}, name), `: ${reason}`))))) : null,
        !run.running ? resultsCard(run) : null,
        joiningCard(),
        h('div', { class: 'card' }, h('div', { class: 'card-head' }, icon('activity'), h('h3', { class: 'grow' }, 'The swarm'), graphLegend()),
          h('div', { class: 'card-body', style: { padding: '8px 12px' } }, flowGraph(nodes, run.stages, run.leader, { mode: run.mode, selected: selectedName, onSelect: name => { ui.selected = name; render(); } }))),
        selected ? agentDetail(run, selected) : null),
      h('div', { class: 'run-right' }, conversation(run))),
    footer: null,
    guide: null,
  };
}

// The agents prepared in the live view that did not join yet: their model, then the step to join, or drop them.
function joiningCard() {
  const pending = store.state.agents.filter(agent => agent.pending);
  if (!pending.length) return null;
  return h('div', { class: 'card pad joining' }, h('div', { class: 'row', style: { marginBottom: '10px' } }, icon('plus'), h('h3', { class: 'grow' }, 'Waiting to join the swarm')),
    pending.map(agent => h('div', { class: 'row wrap', style: { padding: '6px 0' } }, taskIcon(agent.task, 'sm'), h('b', {}, agent.name), h('span', { class: 'small muted grow' },
      agent.model ? `${taskOf(agent.task).label} · ${modelName(agent.model)}` : `${taskOf(agent.task).label} · no model yet`),
      agent.model ? button('Add it now', { kind: 'primary', size: 'sm', iconName: 'plus', onClick: () => openModal({ type: 'join', agentId: agent.id, waits: [], folder: null, errors: {}, error: '' }) }) :
        button('Choose its model', { kind: 'soft', size: 'sm', iconName: 'cpu', onClick: () => openModelPicker(agent, { live: true }) }),
      button('Drop it', { kind: 'ghost', size: 'sm', iconName: 'trash', busy: ui.busy[`drop-${agent.id}`], onClick: () => act('removeAgent', { agentId: agent.id }, { busy: `drop-${agent.id}` }) }))));
}

// The last step to add an agent to the swarm: who it waits for (execute mode), its folder, and when it starts.
function joinModal(modal) {
  const run = store.state.run;
  const agent = store.state.agents.find(item => item.id === modal.agentId);
  if (!agent) return modalFrame({ title: 'This agent is gone', body: h('p', {}, 'It was dropped meanwhile.'), foot: [h('div', { class: 'spacer' }), button('Close', { onClick: closeModal })] });
  const plan = run?.mode === 'plan';
  const others = (run?.agents || []).filter(item => !item.isLeader);
  const folder = modal.folder ?? agent.folder ?? '';
  const toggle = name => { modal.waits = modal.waits.includes(name) ? modal.waits.filter(other => other !== name) : [...modal.waits, name]; render(); };
  const join = async () => {
    const data = await act('joinLive', { agentId: agent.id, waitsFor: plan ? [] : modal.waits, folder }, { busy: 'joinLive', form: modal });
    if (!data) { render(); return; }
    closeModal();
    ui.selected = agent.name;
    toast(`${agent.name} joined the swarm. Every agent was told.`, 'success');
  };
  const when = !run?.running ? 'It works the next time the swarm runs.' : plan ? 'It writes its plan now, and you review it like the others.' :
    'It starts as soon as the agents it waits for are done, receives their results, and reports to the leader.';
  return modalFrame({
    size: 'wide', iconEl: taskIcon(agent.task, 'lg'), title: `Add ${agent.name} to the swarm`, subtitle: `${taskOf(agent.task).label} · ${modelName(agent.model)}`,
    body: h('div', { class: 'stack loose' },
      callout('info', 'info', h('b', {}, when), h('div', {}, 'Every agent of the swarm is told that it joined. Nobody waits for it, so the work already planned goes on as it is.')),
      plan ? null : h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Must it wait for other agents?', h('span', { class: 'opt' }, 'optional')),
        others.length ? h('div', { class: 'pills' }, others.map(other => h('button', { type: 'button', class: ['pill sm', modal.waits.includes(other.name) && 'selected'], onClick: () => toggle(other.name) },
          h('span', { class: 'check' }, modal.waits.includes(other.name) ? icon('check') : null), other.name, h('span', { class: 'faint' }, ` · ${nodeStatus(other, run.mode).label}`)))) :
          h('div', { class: 'small faint' }, 'There is no other agent to wait for.'),
        h('div', { class: 'field-help' }, icon('info'), 'It receives the result of every agent it waits for. One that already finished gives it at once.')),
      h('div', { class: ['field', modal.errors.folder && 'has-error'] }, h('label', { class: 'field-label' }, 'Its folder', h('span', { class: 'opt' }, 'optional')),
        h('div', { class: 'input-group' }, h('input', { class: 'input', 'data-key': 'join-folder', value: folder, placeholder: 'No folder', onInput: event => { modal.folder = event.target.value; } }),
          button('Browse…', { iconName: 'folderOpen', onClick: () => pickPath('folder', folder, path => { modal.folder = path; render(); }) })),
        modal.errors.folder ? h('div', { class: 'field-error' }, icon('alert'), modal.errors.folder) : h('div', { class: 'field-help' }, icon('info'), agent.folderNote))),
    foot: [modal.error ? h('div', { class: 'footer-note error' }, icon('alert', 'sm'), modal.error) : null, h('div', { class: 'spacer' }), button('Not now', { kind: 'ghost', onClick: closeModal }),
      button('Add to the swarm', { kind: 'primary', iconName: 'plus', busy: ui.busy.joinLive, onClick: join })],
  });
}

// Removing an agent: what happens to its result, to the agents that wait for it, to its files and to its memory, before the user confirms.
function removeAgentModal(modal) {
  const run = store.state.run;
  const agent = run?.agents.find(item => item.name === modal.agent);
  if (!agent) return modalFrame({ title: 'This agent already left', body: h('p', {}, 'It is not in the swarm anymore.'), foot: [h('div', { class: 'spacer' }), button('Close', { onClick: closeModal })] });
  const done = agent.status === 'done';
  const waiting = run.agents.filter(other => (other.waitsFor || []).includes(agent.name)).map(other => other.name);
  const facts = [
    done ? 'Its result stays: what it made is kept, and the leader already has it.' : run.running ? 'It stops at its next step. What it left half done in your files is put back.' : 'It will not work in the next run.',
    waiting.length ? `${waiting.join(', ')} ${done ? 'still receive its result' : 'go on without it'}.` : null,
    agent.model?.local ? `Its model frees about ${agent.model.vram} GB of memory on your GPUs as soon as its last step ended.` : 'Its model is let go as soon as its last step ended.',
    'Every agent of the swarm is told that it left, so none of them counts on it anymore.',
  ].filter(Boolean);
  const remove = async () => {
    if (await act('removeLive', { agent: agent.name, reason: modal.reason }, { busy: 'removeLive' })) {
      closeModal();
      ui.selected = null;
      toast(`${agent.name} left the swarm.`, 'success');
    }
  };
  return modalFrame({
    size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#E5484D', '--task-soft': '#E5484D26' } }, icon('trash', 'lg')), title: `Remove ${agent.name} from the swarm?`,
    subtitle: `${agent.role} · ${nodeStatus(agent, run.mode).label}`,
    body: h('div', { class: 'stack' }, h('ul', { class: 'steps' }, facts.map(fact => h('li', {}, fact))),
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Why?', h('span', { class: 'opt' }, 'optional, the leader and the agents read it')),
        h('input', { class: 'input', 'data-key': 'remove-reason', value: modal.reason || '', placeholder: done ? 'For example: it is done, free its memory' : 'For example: it is not needed anymore',
          onInput: event => { modal.reason = event.target.value; } }))),
    foot: [h('div', { class: 'spacer' }), button('Keep it', { kind: 'ghost', onClick: closeModal }), button('Remove it', { kind: 'danger', iconName: 'trash', busy: ui.busy.removeLive, onClick: remove })],
  });
}

function usageOf(run) {
  return run.agents.filter(agent => agent.usage && agent.usage.calls);
}

function resultsCard(run) {
  const banners = {
    succeeded: run.mode === 'plan' ? ['success', 'checkCircle', 'Every plan is approved', 'The agents know what to do. Execute the plans now: every agent drafts its work by following its plan, and you approve each result.']
      : ['success', 'checkCircle', 'The swarm finished its work', 'Every agent is done. Their results are below and in their folders.'],
    unfinished: ['warning', 'alert', 'The swarm ended without a result', 'Some agents did not finish, for example because a draft was rejected or a model failed. Look at each agent below, change what is needed, and run it again.'],
    stopped: ['warning', 'stop', 'You stopped the swarm', 'What was already done stays done. The agents that did not finish were cut in the middle of their task.'],
    error: ['danger', 'xCircle', 'The swarm could not start', run.error],
  };
  const failed = run.agents.filter(agent => agent.status === 'failed').map(agent => agent.name);
  if (run.state === 'succeeded' && failed.length) banners.succeeded = ['warning', 'alert', `The swarm finished, but ${failed.join(', ')} did not`,
    `The leader finished with what it had. Look at ${failed.length === 1 ? 'this agent' : 'these agents'} below to see why, change what is needed, and run it again.`];
  const [tone, iconName, title, text] = banners[run.state] || banners.unfinished;
  const usage = usageOf(run);
  return h('div', { class: 'card enter' },
    h('div', { class: `banner ${tone}`, style: { borderRadius: 'var(--radius) var(--radius) 0 0', border: '0' } }, h('span', { class: 'banner-icon' }, icon(iconName, 'lg')),
      h('div', { class: 'grow' }, h('h3', {}, title), h('div', { class: 'small' }, text)),
      run.state === 'succeeded' && run.mode === 'plan' && !failed.length ? button('Execute the plans', { kind: 'success', iconName: 'play', busy: ui.busy.execute, onClick: () => { store.feed = []; act('execute'); } }) : null,
      run.state === 'error' ? button('Change the models', { iconName: 'cpu', onClick: () => go('models') }) : null,
      run.state !== 'error' ? button('Run again', { iconName: 'refresh', busy: ui.busy.start, onClick: () => { store.feed = []; act('start', { mode: run.mode }); } }) : button('Try again', { iconName: 'refresh', busy: ui.busy.start, onClick: () => act('start', { mode: run.mode }) })),
    run.state !== 'error' ? h('div', { class: 'card-body' },
      run.mode === 'plan' && run.agents.length > 1 && run.summary ? h('div', { style: { marginBottom: '14px' } }, h('h4', { style: { marginBottom: '8px' } }, 'The summary of the leader'), clamped(run.summary, 'summary', 900, 'draft-box')) : null,
      run.agents.map(agent => h('div', { class: 'result-row' }, taskIcon(agent.task, 'sm'),
        h('div', { style: { minWidth: '0' } }, h('div', { class: 'row', style: { gap: '6px' } }, h('b', {}, agent.name), agent.isLeader ? h('span', { class: 'leader-mark' }, icon('crown', 'sm')) : null),
          agent.status === 'done' ? clamped(run.mode === 'plan' ? agent.plan || agent.result : agent.result, `result-${agent.name}`, 360, 'small muted pre') :
            h('div', { class: 'small', style: { color: 'var(--danger-text)' } }, agent.error)),
        statusPill(nodeStatus(agent, run.mode)))),
      (run.removed || []).length ? h('div', { style: { marginTop: '14px' } }, h('h4', { style: { marginBottom: '6px' } }, 'Removed during the run'),
        run.removed.map(item => h('div', { class: 'row small', style: { padding: '3px 0' } }, icon('trash', 'sm'), h('b', {}, item.name), h('span', { class: 'muted' }, `${item.role}, ${item.status} when it left · ${item.reason || 'no reason given'}`)))) : null,
      usage.length ? h('div', { style: { marginTop: '14px' } }, h('h4', { style: { marginBottom: '6px' } }, 'Tokens used'), h('table', { class: 'usage-table' },
        h('tr', {}, h('th', {}, 'Agent'), h('th', {}, 'Model'), h('th', {}, 'Calls'), h('th', {}, 'Read'), h('th', {}, 'Written')),
        usage.map(agent => h('tr', {}, h('td', {}, agent.name), h('td', {}, modelName(agent.model)), h('td', {}, formatNumber(agent.usage.calls)), h('td', {}, formatNumber(agent.usage.input)),
          h('td', {}, formatNumber(agent.usage.output))))), h('div', { class: 'small faint', style: { marginTop: '6px' } }, 'API models are billed for the tokens they read and write.')) : null) : null);
}

function agentDetail(run, agent) {
  const status = nodeStatus(agent, run.mode);
  const what = run.mode === 'plan' ? 'plan' : 'draft';
  const spec = store.state.agents.find(item => item.name === agent.name);
  const correction = ui.corrections[agent.name] || '';
  const message = ui.messages[agent.name] || '';
  const ready = agent.review === 'ready';
  const finished = ['done', 'failed'].includes(agent.status);
  const sendCorrection = async () => {
    if (!correction.trim()) return;
    if (await act('correct', { agent: agent.name, text: correction }, { busy: `correct-${agent.name}`, ok: `${agent.name} rewrites its ${what}.` })) ui.corrections[agent.name] = '';
  };
  const sendMessage = async () => {
    if (!message.trim()) return;
    if (await act('message', { agent: agent.name, text: message }, { busy: `message-${agent.name}`, ok: `${agent.name} reads it with its next step.` })) ui.messages[agent.name] = '';
  };
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, taskIcon(agent.task, 'lg'),
      h('div', { class: 'grow', style: { minWidth: '0' } }, h('div', { class: 'row', style: { gap: '7px' } }, h('h2', {}, agent.name), agent.isLeader ? badge('Leader', 'honey', 'crown') : null,
        agent.removable ? button('', { kind: 'ghost', size: 'sm', iconName: 'trash', title: `Remove ${agent.name} from the swarm`, onClick: () => openModal({ type: 'removeAgent', agent: agent.name, reason: '' }) }) : null),
        h('div', { class: 'small muted' }, agent.role, agent.model ? ` · ${modelName(agent.model)}` : '')), statusPill(status)),
    h('div', { class: 'card-body' },
      h('div', { class: 'detail-section' }, h('h4', {}, 'Its task'), h('p', { class: 'muted' }, agent.description),
        agent.waitsFor.length && !agent.isLeader ? h('p', { class: 'small', style: { marginTop: '6px' } }, icon('clock', 'sm'), ` Waits for ${agent.waitsFor.join(', ')} and receives their results.`) : null),
      ready ? h('div', { class: 'detail-section' }, h('div', { class: 'row', style: { marginBottom: '8px' } }, h('h4', { class: 'grow', style: { margin: '0' } }, `Its ${what} waits for you`), badge(`Draft ${agent.revision}`, 'outline')),
        h('div', { class: 'draft-box ready' }, agent.draft),
        agent.problem ? h('div', { style: { marginTop: '10px' } }, callout('warning', 'alert', h('b', {}, 'The automatic checks found a problem.'), h('div', {}, agent.problem))) :
          h('div', { class: 'small', style: { marginTop: '8px', color: 'var(--success-text)' } }, icon('checkCircle', 'sm'), ' The automatic checks passed.'),
        h('div', { class: 'row wrap', style: { marginTop: '12px' } },
          button(`Approve`, { kind: 'success', iconName: 'check', busy: ui.busy[`approve-${agent.name}`], onClick: () => act('approve', { agent: agent.name, revision: agent.revision }, { busy: `approve-${agent.name}`, ok: `You approved ${agent.name}.` }) }),
          button('Reject', { kind: 'danger-ghost', iconName: 'x', busy: ui.busy[`reject-${agent.name}`], onClick: () => act('reject', { agent: agent.name, revision: agent.revision }, { busy: `reject-${agent.name}` }) })),
        h('div', { class: 'field', style: { marginTop: '14px' } }, h('label', { class: 'field-label' }, 'Or ask for changes, in your own words'),
          h('textarea', { class: 'textarea', 'data-key': `correct-${agent.name}`, rows: 2, value: correction, placeholder: 'For example: make it shorter and more formal.',
            onInput: event => { ui.corrections[agent.name] = event.target.value; }, onKeydown: event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') sendCorrection(); } }),
          h('div', {}, button('Ask for changes', { kind: 'soft', iconName: 'edit', busy: ui.busy[`correct-${agent.name}`], onClick: sendCorrection })))) : null,
      !ready && agent.draft && !finished ? h('div', { class: 'detail-section' }, h('h4', {}, agent.review === 'approved' ? `Its approved ${what}` : `Its latest ${what}`), clamped(agent.draft, `draft-${agent.name}`, 700, 'draft-box')) : null,
      agent.plan && run.mode === 'execute' ? h('div', { class: 'detail-section' }, h('h4', {}, 'The plan it follows'), clamped(agent.plan, `plan-${agent.name}`, 500, 'draft-box')) : null,
      agent.status === 'done' && agent.result ? h('div', { class: 'detail-section' }, h('div', { class: 'row', style: { marginBottom: '8px' } }, h('h4', { class: 'grow', style: { margin: '0' } }, run.mode === 'plan' ? 'Its approved plan' : 'Its result'),
        button('', { kind: 'ghost', size: 'sm', iconName: 'copy', title: 'Copy', onClick: () => copyText(String(agent.result)) }),
        spec?.folder ? button('Open its folder', { kind: 'ghost', size: 'sm', iconName: 'folderOpen', onClick: () => act('openFolder', { path: spec.folder }) }) : null),
        clamped(String(agent.result), `res-${agent.name}`, 900, 'draft-box')) : null,
      agent.error ? h('div', { class: 'detail-section' }, callout('danger', 'xCircle', h('b', {}, 'It did not finish.'), h('div', {}, agent.error))) : null,
      agent.actions.length ? h('div', { class: 'detail-section' }, h('h4', {}, 'What it changed on your computer or online'),
        h('ul', { class: 'action-list' }, agent.actions.map(action => h('li', {}, icon('check', 'sm'), h('span', {}, h('span', { class: 'faint' }, action.time.slice(11)), ' ', action.text))))) : null,
      agent.startAt ? h('div', { class: 'detail-section' }, h('div', { class: 'row' }, icon('clock'), h('div', { class: 'grow' }, h('b', {}, `It starts at ${agent.startAt.slice(11)}.`),
        h('div', { class: 'small muted' }, 'The other agents do not wait for it.')), button('Start it now', { kind: 'primary', size: 'sm', iconName: 'play', onClick: () => act('startNow', { agent: agent.name }, { ok: `${agent.name} starts now.` }) }))) : null,
      run.running && !finished ? h('div', { class: 'detail-section' }, h('h4', {}, `Send a message to ${agent.name}`),
        h('div', { class: 'composer-row row', style: { alignItems: 'flex-end' } }, h('textarea', { class: 'textarea', rows: 1, 'data-key': `message-${agent.name}`, style: { minHeight: '44px' }, value: message,
          placeholder: ready ? `It is taken as a correction of its ${what}.` : 'It reads it with its next step.', onInput: event => { ui.messages[agent.name] = event.target.value; },
          onKeydown: event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') sendMessage(); } }),
          button('', { kind: 'primary', iconName: 'send', title: 'Send (Ctrl+Enter)', busy: ui.busy[`message-${agent.name}`], onClick: sendMessage }))) : null));
}

function speakerStyle(name, run) {
  const agent = run?.agents.find(item => item.name === name) || store.state.agents.find(item => item.name === name);
  return { task: agent?.task, leader: run ? run.leader === name : agent?.isLeader };
}

function feedItem(item, run) {
  const fresh = item.id > ui.seenFeed;
  if (item.kind === 'event') {
    const icons = { success: 'checkCircle', danger: 'xCircle', warning: 'alert', attention: 'bell', start: 'rocket', info: 'info' };
    return h('div', { class: ['feed-event', item.tone, fresh && 'msg new'] }, icon(icons[item.tone] || 'info'), h('span', {}, item.text), h('span', { class: 'faint' }, item.time.slice(0, 5)));
  }
  if (item.kind === 'answer' || item.speaker === store.catalog.user) {
    return h('div', { class: ['msg mine', fresh && 'new'] }, h('span', { class: 'avatar', style: { background: 'var(--honey)', color: '#fff' } }, icon('user')),
      h('div', { class: 'msg-body', style: { display: 'flex', flexDirection: 'column', alignItems: 'flex-end' } },
        h('div', { class: 'msg-meta' }, h('b', {}, item.receiver ? `You → ${item.receiver}` : 'You'), item.time), h('div', { class: 'bubble' }, item.text)));
  }
  const style = speakerStyle(item.speaker, run);
  const color = taskStyle(style.task).color;
  return h('div', { class: ['msg', fresh && 'new'] },
    h('span', { class: 'avatar', style: { background: tint(color, 0.16), color } }, icon(style.leader ? 'crown' : taskStyle(style.task).icon)),
    h('div', { class: 'msg-body' }, h('div', { class: 'msg-meta' }, h('b', {}, item.kind === 'message' ? `${item.speaker} → ${item.receiver}` : item.speaker || 'SwarmUP'),
      style.leader && item.kind !== 'message' ? h('span', {}, 'leader') : null, item.time),
      clamped(item.text, `feed-${item.id}`, item.kind === 'message' ? 260 : 900, ['bubble', item.kind === 'message' && 'between', item.tone === 'warning' && 'warning'].filter(Boolean).join(' '))));
}

function conversation(run) {
  const state = store.state;
  const forMe = ui.feedFilter === 'me';
  const items = store.feed.filter(item => !forMe || item.kind === 'note' || item.kind === 'answer' || (item.kind === 'event' && item.tone !== 'info'));
  const rendered = items.map(item => feedItem(item, run));
  ui.seenFeed = store.feedAfter;
  return [
    h('div', { class: 'convo-head' }, icon('message'), h('h3', { class: 'grow' }, 'Conversation'),
      h('div', { class: 'segmented' }, h('button', { type: 'button', class: !forMe && 'active', onClick: () => { ui.feedFilter = 'all'; render(); } }, 'Everything'),
        h('button', { type: 'button', class: forMe && 'active', onClick: () => { ui.feedFilter = 'me'; render(); } }, 'For me'))),
    h('div', { class: 'feed', 'data-scroll': 'feed', 'data-stick': '' }, rendered.length ? rendered : h('div', { class: 'empty small' }, run.running ? 'The agents are getting ready…' : 'Nothing was said yet.')),
    h('div', { class: 'composer' }, state.questions.length ? state.questions.map(question => questionCard(question, run, false)) :
      h('div', { class: 'small muted row' }, icon('info', 'sm'), run.running ? 'Nothing waits for you right now. Click an agent on the map to look at it or to message it.' : 'The swarm is not running.')),
  ];
}

// A coding agent asks for a permission: what it wants to do, exactly, where and why, with allow once, allow until the next run, or deny.
function permissionCard(question, run, inModal) {
  const request = question.payload || {};
  const why = ui.composer[question.id] || '';
  const send = async decision => {
    const data = await act('answer', { id: question.id, answer: { decision, message: decision === 'deny' ? why : '' } }, { busy: `answer-${question.id}` });
    if (data) delete ui.composer[question.id];
  };
  const style = speakerStyle(question.speaker, run);
  return h('div', { class: 'question-card permission' },
    h('div', { class: 'q-head' }, icon('shield', 'sm'), `${question.speaker} asks for your permission`, h('span', { class: 'grow' }), h('span', { class: 'faint small' }, question.time.slice(0, 5))),
    h('div', { class: 'q-text' }, `It wants to ${request.action}:`),
    h('pre', { class: 'permission-detail' }, request.detail || ''),
    h('div', { class: 'small muted' }, icon('folder', 'sm'), ` In ${request.folder || 'its folder'}`, request.reason ? h('span', {}, ` · ${request.reason}`) : null),
    inModal && question.context.length ? h('div', { class: 'q-context' }, question.context.map(text => h('div', {}, pretty(text)))) : null,
    h('div', { class: 'row wrap' }, question.replies.map(reply => button(reply.label, { kind: reply.style === 'primary' ? 'primary' : reply.style === 'danger' ? 'danger-ghost' : 'ghost',
      size: 'sm', iconName: reply.value === 'once' ? 'check' : reply.value === 'run' ? 'checkCircle' : 'x', busy: ui.busy[`answer-${question.id}`], onClick: () => send(reply.value) }))),
    h('input', { class: 'input', 'data-key': `q-${question.id}`, value: why, placeholder: 'If you deny it, you can tell it why or what to do instead (optional)',
      onInput: event => { ui.composer[question.id] = event.target.value; }, onKeydown: event => { if (event.key === 'Enter' && why.trim()) send('deny'); } }));
}

// A coding agent asks its own questions: options to choose (one, or several), or words of the user.
function formCard(question, run) {
  const questions = question.payload || [];
  const form = ui.forms[question.id] = ui.forms[question.id] || Object.fromEntries(questions.map(item => [item.id, { picked: [], other: '' }]));
  const toggle = (item, label) => {
    const state = form[item.id];
    state.picked = item.multiple ? (state.picked.includes(label) ? state.picked.filter(other => other !== label) : [...state.picked, label]) : [label];
    render();
  };
  const send = async () => {
    const answers = Object.fromEntries(questions.map(item => [item.id, form[item.id].other.trim() ? [...(item.multiple ? form[item.id].picked : []), form[item.id].other.trim()] : form[item.id].picked]));
    const data = await act('answer', { id: question.id, answer: answers }, { busy: `answer-${question.id}` });
    if (data) delete ui.forms[question.id];
  };
  return h('div', { class: 'question-card' },
    h('div', { class: 'q-head' }, icon('help', 'sm'), `${question.speaker} asks you`, h('span', { class: 'grow' }), h('span', { class: 'faint small' }, question.time.slice(0, 5))),
    questions.map(item => h('div', { class: 'stack tight' }, item.header ? h('h4', {}, item.header) : null, h('div', { class: 'q-text' }, item.question),
      item.options?.length ? h('div', { class: 'pills' }, item.options.map(option => h('button', { type: 'button', class: ['pill sm', form[item.id].picked.includes(option.label) && 'selected'],
        title: option.description || null, onClick: () => toggle(item, option.label) }, h('span', { class: 'check' }, form[item.id].picked.includes(option.label) ? icon('check') : null), option.label))) : null,
      h('input', { class: 'input', type: item.secret ? 'password' : 'text', autocomplete: 'off', 'data-key': `f-${question.id}-${item.id}`, value: form[item.id].other,
        placeholder: item.options?.length ? 'Or your own answer' : 'Your answer', onInput: event => { form[item.id].other = event.target.value; } }))),
    h('div', {}, button('Send the answers', { kind: 'primary', size: 'sm', iconName: 'send', busy: ui.busy[`answer-${question.id}`], onClick: send })));
}

// An agent the leader proposes: its task, its model, who it waits for, why, its settings, and what the user will be asked for.
function proposedAgent(agent, number = null) {
  return h('div', { class: 'proposed-agent' }, taskIcon(agent.task),
    h('div', { class: 'grow stack tight', style: { minWidth: '0' } },
      h('div', { class: 'row wrap', style: { gap: '6px' } }, number ? h('span', { class: 'faint small' }, `${number}.`) : null, h('b', {}, agent.name), h('span', { class: 'small muted' }, agent.label),
        badge(agent.model, { cli: 'honey', local: 'primary', api: 'info' }[agent.modelKind], { cli: 'terminal', local: 'cpu', api: 'cloud' }[agent.modelKind])),
      h('div', { class: 'small' }, agent.description),
      h('div', { class: 'small muted' }, icon(agent.waitsFor.length ? 'clock' : 'zap', 'sm'), agent.waitsFor.length ? ` Waits for ${agent.waitsFor.join(', ')} and receives their results` : ' Starts right away'),
      agent.why ? h('div', { class: 'small why' }, icon('crown', 'sm'), h('span', {}, agent.why)) : null,
      agent.settings.length ? h('dl', { class: 'kv small' }, agent.settings.map(item => [h('dt', {}, item.label), h('dd', {}, item.value)])) : null,
      agent.needs.length ? h('div', { class: 'small needs' }, icon('key', 'sm'), h('span', {}, `You will be asked for: ${agent.needs.join('; ')}.`)) : null));
}

// The leader proposes the swarm, or a change of the swarm while it runs. The user approves, rejects, or says what to change (the leader writes it again).
function proposalCard(question, run, inModal) {
  const proposal = question.payload || {};
  const draft = ui.composer[question.id] || '';
  const busy = ui.busy[`answer-${question.id}`];
  const send = async (decision, message = '') => {
    const data = await act('answer', { id: question.id, answer: { decision, message } }, { busy: `answer-${question.id}` });
    if (data) delete ui.composer[question.id];
  };
  const titles = { build: 'proposes a swarm for your mission', add: 'proposes to add an agent', remove: 'proposes to remove an agent', model: 'proposes to change a model' };
  const details = proposal.details || {};
  let body;
  if (proposal.action === 'build') {
    body = h('div', { class: 'stack tight' }, (proposal.agents || []).map((agent, index) => proposedAgent(agent, index + 1)));
  } else if (proposal.action === 'add') {
    body = proposedAgent(details);
  } else if (proposal.action === 'remove') {
    const facts = [`${details.role}, ${details.status === 'done' ? 'done: its result stays, and the agents that need it keep it' : `${details.status}: it stops at its next step, and what it left half done is put back`}.`,
      details.model ? `Its model (${details.model}) frees its memory.` : null,
      details.waitedBy?.length && details.status !== 'done' ? `${details.waitedBy.join(', ')} wait for it, and go on without it.` : null, 'Every agent of the swarm is told.'].filter(Boolean);
    body = h('div', { class: 'stack tight' }, h('div', { class: 'row' }, icon('trash', 'sm'), h('b', {}, `Remove ${proposal.agent}`)), h('ul', { class: 'steps small' }, facts.map(fact => h('li', {}, fact))));
  } else {
    body = h('div', { class: 'stack tight' }, h('div', { class: 'row wrap' }, icon('cpu', 'sm'), h('b', {}, proposal.agent), h('span', { class: 'muted small' }, details.from || 'no model'), icon('arrowRight', 'sm'), h('b', { class: 'small' }, details.to)),
      h('div', { class: 'small muted' }, 'It has not started yet, so it starts with the new model.'));
  }
  return h('div', { class: 'question-card proposal' },
    h('div', { class: 'q-head' }, icon('crown', 'sm'), `${question.speaker} ${titles[proposal.action] || 'proposes a change'}`, h('span', { class: 'grow' }), h('span', { class: 'faint small' }, question.time.slice(0, 5))),
    proposal.why ? h('div', { class: 'why-box' }, h('span', { class: 'faint small' }, proposal.action === 'build' ? 'The leader says' : 'Why'), h('div', {}, proposal.why)) : null,
    h('div', { class: 'proposal-body', 'data-scroll': inModal ? `proposal-${question.id}` : null }, body),
    h('div', { class: 'row wrap' }, button(proposal.action === 'build' ? 'Approve this swarm' : 'Approve', { kind: 'primary', size: 'sm', iconName: 'check', busy, onClick: () => send('approve') }),
      button('Reject', { kind: 'danger-ghost', size: 'sm', iconName: 'x', busy, onClick: () => send('reject') })),
    h('div', { class: 'composer-row row', style: { alignItems: 'flex-end' } },
      h('textarea', { class: 'textarea', rows: 1, 'data-key': `q-${question.id}`, value: draft, placeholder: proposal.action === 'build' ? 'Or tell the leader what to change, in your own words…' : 'Or reject it and tell the leader why…',
        onInput: event => { ui.composer[question.id] = event.target.value; }, onKeydown: event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && draft.trim()) send('reject', draft.trim()); } }),
      button(proposal.action === 'build' ? 'Ask for changes' : 'Send', { kind: 'soft', size: 'sm', iconName: 'send', busy, disabled: !draft.trim(), onClick: () => send('reject', draft.trim()) })));
}

// A question of an agent or of the leader, with buttons for the usual answers and a box for the others.
function questionCard(question, run, inModal) {
  if (question.kind === 'permission') return permissionCard(question, run, inModal);
  if (question.kind === 'form') return formCard(question, run);
  if (question.kind === 'proposal') return proposalCard(question, run, inModal);
  const draft = ui.composer[question.id] || '';
  const send = async value => {
    const data = await act('answer', { id: question.id, answer: value }, { busy: `answer-${question.id}` });
    if (data) delete ui.composer[question.id];
  };
  const takesText = ['review', 'approval', 'text', 'secret'].includes(question.kind);
  const placeholder = { review: 'Or write what you want changed…', approval: 'Or write what you want changed…', secret: 'Hidden while you type', text: 'Your answer…' }[question.kind];
  const ready = question.kind === 'review' && run ? run.ready : [];
  const style = speakerStyle(question.speaker, run);
  return h('div', { class: 'question-card' },
    h('div', { class: 'q-head' }, icon(style.leader ? 'crown' : 'bell', 'sm'), `${question.speaker || 'SwarmUP'} asks you`, h('span', { class: 'grow' }), h('span', { class: 'faint small' }, question.time.slice(0, 5))),
    inModal && question.context.length ? h('div', { class: 'q-context' }, question.context.map(text => h('div', { style: { marginBottom: '6px' } }, pretty(text)))) : null,
    h('div', { class: 'q-text' }, question.text),
    ready.length && !inModal ? h('div', { class: 'row wrap small' }, h('span', { class: 'muted' }, 'Or decide one by one:'), ready.map(name => h('button', { class: 'pill sm', type: 'button',
      onClick: () => { ui.selected = name; render(); } }, icon('eye', 'sm'), name))) : null,
    question.replies.length ? h('div', { class: 'row wrap' }, question.replies.map(reply => button(reply.label, { kind: reply.style === 'primary' ? 'primary' : reply.style === 'danger' ? 'danger-ghost' : 'ghost',
      size: 'sm', iconName: reply.style === 'primary' ? 'check' : reply.style === 'danger' ? 'x' : null, busy: ui.busy[`answer-${question.id}`], onClick: () => send(reply.value) }))) : null,
    takesText ? h('div', { class: 'composer-row row', style: { alignItems: 'flex-end' } },
      question.secret ? h('input', { class: 'input', type: 'password', 'data-key': `q-${question.id}`, value: draft, placeholder, autocomplete: 'off', onInput: event => { ui.composer[question.id] = event.target.value; },
        onKeydown: event => { if (event.key === 'Enter') send(event.target.value); } }) :
        h('textarea', { class: 'textarea', rows: 1, 'data-key': `q-${question.id}`, value: draft, placeholder, onInput: event => { ui.composer[question.id] = event.target.value; },
          onKeydown: event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey || question.kind === 'text')) { event.preventDefault(); send(event.target.value); } } }),
      button('', { kind: 'primary', iconName: 'send', title: 'Send (Ctrl+Enter)', busy: ui.busy[`answer-${question.id}`], onClick: () => send(ui.composer[question.id] || '') })) : null);
}


// ==============
// The swarms that were interrupted: continue (the secrets are given again) or cancel (after the summary of the leader).
// ==============
async function openResume(id) {
  const data = await act('resumeForm', { id }, { busy: `resume-${id}` });
  if (!data) return;
  const form = data.resume;
  ui.resume = { form, values: Object.fromEntries(form.agents.map(agent => [agent.name, { accounts: [] }])), keys: {}, tokens: {}, errors: {}, error: '' };
  if (form.codex) loadCodex();
  go('resume');
}

function viewResume() {
  const resume = ui.resume;
  if (!resume) return viewHome();
  const form = resume.form;
  const fieldError = key => resume.errors[key] ? h('div', { class: 'field-error' }, icon('alert'), resume.errors[key]) : null;
  const submit = async () => {
    const data = await act('resume', { id: form.id, secrets: resume.values, keys: resume.keys, tokens: resume.tokens }, { busy: 'resume', form: resume });
    if (data) { ui.resume = null; ui.selected = null; store.feed = []; go('run'); } else render();
  };
  return {
    header: header('Continue a swarm', shorten(form.mission, 120), 'Passwords, keys and tokens are never saved, so give them again. What is already done is not done again, and nothing approved is sent twice.'),
    content: h('div', { class: 'stack loose', style: { maxWidth: '900px' } },
      Object.keys(form.providers).length ? h('div', { class: 'card pad enter' }, h('div', { class: 'row', style: { marginBottom: '12px' } }, icon('key'), h('h3', {}, 'API keys')),
        Object.entries(form.providers).map(([provider, key]) => h('div', { class: ['field', resume.errors[`key.${provider}`] && 'has-error'] }, h('label', { class: 'field-label' }, `${key.company} API key`,
          key.source ? h('span', { class: 'opt' }, key.source === 'environment' ? `found in ${key.variable}` : 'given earlier') : h('span', { class: 'req' }, '*')),
          h('input', { class: 'input', type: 'password', 'data-key': `rk-${provider}`, autocomplete: 'off', value: resume.keys[provider] || '', placeholder: key.source ? 'Leave empty to use it' : 'Paste it here',
            onInput: event => { resume.keys[provider] = event.target.value; } }), fieldError(`key.${provider}`), !key.source ? h('div', { class: 'field-help' }, icon('lock'), extLink(key.page, 'Create a key')) : null))) : null,
      form.codex ? h('div', { class: 'card pad enter' }, h('div', { class: 'row', style: { marginBottom: '12px' } }, icon('terminal'), h('h3', {}, 'Codex')),
        ui.codex.data || ui.codex.loading ? codexAccountPanel() : h('div', {}, button('Check the sign-in of Codex', { iconName: 'refresh', onClick: loadCodex })),
        resume.errors.codex ? fieldError('codex') : null) : null,
      form.gated.length ? h('div', { class: 'card pad enter' }, h('div', { class: 'row', style: { marginBottom: '12px' } }, icon('lock'), h('h3', {}, 'Hugging Face tokens'), h('span', { class: 'opt small faint' }, 'optional')),
        form.gated.map(name => h('div', { class: 'field' }, h('label', { class: 'field-label' }, name), h('input', { class: 'input', type: 'password', 'data-key': `rt-${name}`, value: resume.tokens[name] || '',
          placeholder: 'hf_… (empty if you logged in with huggingface-cli)', onInput: event => { resume.tokens[name] = event.target.value; } })))) : null,
      form.agents.map(agent => h('div', { class: 'card agent-row enter-2' }, taskIcon(agent.task),
        h('div', { class: 'stack' }, h('div', { class: 'agent-row-head' }, h('span', { class: 'agent-row-name' }, agent.name), h('span', { class: 'small muted' }, agent.role), h('span', { class: 'grow' }),
          badge(agent.status, agent.status === 'done' ? 'success' : agent.status === 'failed' ? 'danger' : 'warning')),
          agent.model ? h('div', { class: 'small muted row' }, icon(modelIcon(agent.model), 'sm'), modelName(agent.model)) : null,
          agent.missing.length ? callout('warning', 'download', `Install first: pip install -U ${agent.missing.join(' ')}`) : null,
          agent.fields.length ? agent.fields.map(field => h('div', { class: ['field', resume.errors[`${agent.name}.${field.key}`] && 'has-error'] },
            h('label', { class: 'field-label' }, field.ask, field.required ? h('span', { class: 'req' }, '*') : h('span', { class: 'opt' }, agent.finished ? 'not needed: it already finished' : 'optional')),
            h('input', { class: 'input', type: 'password', autocomplete: 'off', 'data-key': `rs-${agent.name}-${field.key}`, value: resume.values[agent.name][field.key] || '',
              onInput: event => { resume.values[agent.name][field.key] = event.target.value; } }), fieldError(`${agent.name}.${field.key}`), field.help ? h('div', { class: 'field-help' }, icon('info'), field.help) : null)) :
            !agent.accounts ? h('div', { class: 'small faint' }, agent.finished ? 'It already finished: nothing is needed.' : 'Nothing to give again.') : null,
          agent.accounts ? h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Accounts on publisher websites', h('span', { class: 'opt' }, 'optional')),
            accountsInput({ key: 'accounts' }, { values: resume.values[agent.name], touched: {}, errors: {} })) : null))),
      resume.error && !Object.keys(resume.errors).length ? callout('danger', 'alert', resume.error) : null),
    footer: footer({ label: 'Not now', onClick: () => { ui.resume = null; go('home'); } }, { label: 'Continue the swarm', iconName: 'play', busy: ui.busy.resume, onClick: submit }),
    guide: [
      { q: 'Why again?', icon: 'lock', tone: 'tip', text: 'SwarmUP never writes a password, a key or a token to a file. They are only kept in memory while it runs, so they are asked again after a stop.' },
      { q: 'Nothing twice', text: 'An email or a message you approved is never sent twice. If the program stopped while sending one, you will be asked if it must be sent again.' },
    ],
  };
}

async function openCancel(saved) {
  const modal = { type: 'cancel', saved, form: null, key: '', stage: 'loading' };
  openModal(modal);
  try {
    modal.form = (await api('resumeForm', { id: saved.id })).resume;
  } catch (error) {
    modal.form = null;
  }
  modal.stage = 'ask';
  render();
}

function cancelModal(modal) {
  const state = store.state;
  const job = state.jobs.cancel;
  const cancelling = state.cancelling && state.cancelling.id === modal.saved.id ? state.cancelling : null;
  const leader = modal.form?.agents.find(agent => agent.name === modal.saved.leader);
  const provider = leader?.model && !leader.model.local ? leader.model.provider : null;
  const needsKey = provider && !state.keys[provider].source;
  const summarize = plain => act('prepareCancel', { id: modal.saved.id, plain, keys: provider && modal.key ? { [provider]: modal.key } : {} }, { busy: 'prepareCancel' });
  let body, foot;
  if (job?.state === 'running') {
    body = loading(`${modal.saved.leader} is listing what the swarm already did…`);
    foot = [h('div', { class: 'spacer' })];
  } else if (cancelling && job?.state === 'done') {
    body = h('div', { class: 'stack' }, h('h4', {}, `${modal.saved.leader} summarises what the swarm did`), h('div', { class: 'draft-box' }, cancelling.summary),
      callout('warning', 'alert', 'If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task. What they changed in your files is put back.'));
    foot = [h('div', { class: 'spacer' }), button('Continue it instead', { kind: 'ghost', iconName: 'play', onClick: () => { closeModal(); api('clearJob', { name: 'cancel' }); openResume(modal.saved.id); } }),
      button('Stop it for good', { kind: 'danger', iconName: 'stop', busy: ui.busy.abandon, onClick: async () => {
        if (await act('abandon', { id: modal.saved.id }, { ok: 'The swarm is stopped.' })) closeModal();
      } })];
  } else {
    body = h('div', { class: 'stack' }, h('p', {}, `Before you cancel "${shorten(modal.saved.mission, 80)}", its leader summarises what was already changed (emails sent, events booked, files written) and what would be cut in the middle.`),
      modal.stage === 'loading' ? loading('Reading the saved swarm…') : null,
      needsKey ? h('div', { class: 'field' }, h('label', { class: 'field-label' }, `${state.keys[provider].company} API key of the leader`, h('span', { class: 'opt' }, 'optional')),
        h('input', { class: 'input', type: 'password', 'data-key': 'cancel-key', value: modal.key, placeholder: 'Leave empty for a plain list of the facts', onInput: event => { modal.key = event.target.value; } }),
        h('div', { class: 'field-help' }, icon('info'), `The leader writes the summary with ${leader.model.name}, which needs its key again.`)) : null,
      job?.state === 'failed' ? callout('danger', 'alert', job.error) : null);
    foot = [h('div', { class: 'spacer' }), button('Keep it', { kind: 'ghost', onClick: closeModal }),
      button('Show the plain facts', { kind: 'ghost', disabled: modal.stage === 'loading', onClick: () => summarize(true) }),
      button('Summarise and decide', { kind: 'primary', iconName: 'sparkles', disabled: modal.stage === 'loading' || (needsKey && !modal.key.trim()), busy: ui.busy.prepareCancel, onClick: () => summarize(false) })];
  }
  return modalFrame({ size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#E5484D', '--task-soft': '#E5484D26' } }, icon('stop', 'lg')), title: 'Cancel this swarm?', body, foot });
}


// ==============
// The windows that open over the page.
// ==============
function openModal(modal) {
  if (ui.modal) ui.stack.push(ui.modal);
  ui.modal = modal;
  render();
}

function closeModal() {
  const closing = ui.modal;
  ui.modal = ui.stack.pop() || null;
  if (closing?.type === 'stop' && store.state.jobs.changes) api('clearJob', { name: 'changes' }).catch(() => {});
  render();
}

function modalFrame({ size = '', iconEl = null, title, subtitle = '', body, foot = null, closable = true }) {
  return h('div', { class: ['modal', size], role: 'dialog', 'aria-modal': 'true', 'aria-label': title, onClick: event => event.stopPropagation() },
    h('div', { class: 'modal-head' }, iconEl, h('div', { class: 'grow' }, h('h2', {}, title), subtitle ? h('p', {}, subtitle) : null),
      closable ? button('', { kind: 'ghost', iconName: 'x', title: 'Close', onClick: closeModal }) : null),
    h('div', { class: 'modal-body', 'data-scroll': `modal-${title}` }, body),
    foot ? h('div', { class: 'modal-foot' }, foot) : null);
}

function helpModal() {
  const terms = [
    ['users', 'Swarm', 'A small team of AI agents that work for you. They work at the same time, unless one needs the result of another.'],
    ['crown', 'Leader', 'The first agent. Besides its own task, it works last, summarises the work of the others, and passes your corrections to the agents they concern.'],
    ['sparkles', 'Built by the leader', 'Instead of building the swarm yourself, let the leader propose it: the agents, their tasks and their models. It can also propose changes while the swarm runs. You approve every proposal.'],
    ['cpu', 'Model', 'The AI that does the thinking of an agent. Every agent has its own.'],
    ['gauge', 'VRAM', 'The memory of your graphics cards (GPUs). A local model must fit in it. The bigger the model, the more it needs.'],
    ['cloud', 'API', 'A model that runs on the servers of a company. You need an API key from that company, and you pay for what you use.'],
    ['list', 'Plan mode', 'Every agent writes its plan, the leader summarises them, and you approve. Nothing else happens.'],
    ['play', 'Execute mode', 'Every agent drafts its work and shows it to you. It only acts (sends, books, saves) after you approve.'],
    ['folder', 'Folder', 'Where an agent saves what it makes. Optional.'],
    ['shield', 'Safety', 'Passwords and keys stay in memory only. The state of a swarm is saved at every change, so nothing is lost if the computer stops.'],
  ];
  return modalFrame({ size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#5B4CF0', '--task-soft': '#5B4CF026' } }, icon('help', 'lg')), title: 'How SwarmUP works',
    subtitle: 'The words you meet in SwarmUP, in plain language.',
    body: h('dl', { class: 'glossary' }, terms.map(([iconName, term, text]) => [h('dt', {}, icon(iconName, 'sm'), term), h('dd', {}, text)])),
    foot: [h('div', { class: 'small muted' }, 'Tip: hover a button to see what it does.'), h('div', { class: 'spacer' }), button('Got it', { kind: 'primary', onClick: closeModal })] });
}

function stopModal() {
  const job = store.state.jobs.changes;
  if (!job) {
    return modalFrame({ size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#E5484D', '--task-soft': '#E5484D26' } }, icon('stop', 'lg')), title: 'Stop the swarm?',
      subtitle: 'First, the leader tells you what was already done.', body: h('p', {}, 'The leader lists what the swarm already changed (emails sent, files written…) and what would be cut in the middle. Then you decide.'),
      foot: [h('div', { class: 'spacer' }), button('Keep going', { kind: 'ghost', onClick: closeModal }), button('Show me first', { kind: 'primary', busy: ui.busy.prepareStop, onClick: () => act('prepareStop') })] });
  }
  return modalFrame({ size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#E5484D', '--task-soft': '#E5484D26' } }, icon('stop', 'lg')), title: 'Stop the swarm?',
    body: job.state === 'running' ? loading('The leader is listing what the swarm already changed…') : job.state === 'failed' ? callout('danger', 'alert', job.error) :
      h('div', { class: 'stack' }, h('div', { class: 'draft-box' }, job.result), callout('warning', 'alert', 'If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task.')),
    foot: [h('div', { class: 'spacer' }), button('Keep going', { kind: 'ghost', onClick: closeModal }),
      button('Stop here', { kind: 'danger', iconName: 'stop', disabled: job.state === 'running', busy: ui.busy.stop, onClick: async () => { if (await act('stop', {}, { ok: 'The swarm is stopping.' })) closeModal(); } })] });
}

function confirmModal({ title, text, confirm, danger = false, run, iconName = 'alert' }) {
  return modalFrame({ iconEl: h('span', { class: 'task-icon lg', style: { '--task': danger ? '#E5484D' : '#5B4CF0' } }, icon(iconName, 'lg')), title, body: h('p', { class: 'muted' }, text),
    foot: [h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal }), button(confirm, { kind: danger ? 'danger' : 'primary', busy: ui.busy.confirm, onClick: run })] });
}

function questionModal(question) {
  const proposal = question.kind === 'proposal';
  return modalFrame({ size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#F4A62A', '--task-soft': '#F4A62A26' } }, icon(proposal ? 'crown' : 'bell', 'lg')),
    title: proposal ? `${question.speaker} has a proposal for you` : `${question.speaker || 'SwarmUP'} needs your answer`, closable: false,
    body: questionCard(question, store.state.run, true) });
}

function renderModal() {
  const layer = document.getElementById('modal');
  const questions = store.state.questions;
  let content = null;
  if (questions.length && ui.view !== 'run') content = questionModal(questions[0]);
  else if (ui.modal) {
    const modal = ui.modal;
    const builders = {
      tasks: taskPickerModal, agent: agentFormModal, browse: browseModal, model: modelPickerModal, help: helpModal, stop: stopModal, cancel: cancelModal,
      join: joinModal, removeAgent: removeAgentModal,
      quit: () => confirmModal({ title: 'Quit SwarmUP?', iconName: 'power', confirm: 'Quit', danger: true, text: store.state.run?.running ? 'The swarm is running. It is saved now, and you can continue it at the next start.' : 'Your team is kept only while SwarmUP runs: what you built here is lost when you quit, except a swarm that was interrupted.',
        run: async () => { const data = await act('quit', {}, { busy: 'confirm' }); if (data) document.body.replaceChildren(h('div', { class: 'splash' }, h('div', { class: 'splash-logo' }, logo()), h('div', { class: 'splash-text' }, data.message))); } }),
      confirmNew: () => confirmModal({ title: 'Start a new swarm?', iconName: 'plus', confirm: 'Start a new swarm', danger: true, text: 'The mission, the agents and their models are cleared. The API keys you gave stay in memory.',
        run: async () => { if (await act('newSwarm', {}, { busy: 'confirm' })) { closeModal(); ui.stack = []; ui.modal = null; Object.assign(ui, { missionDraft: null, folderDrafts: {}, waitsDraft: null, orderChoice: null, mode: null, selected: null }); ui.visited.clear(); store.feed = []; go('mission'); } } }),
    };
    content = builders[modal.type] ? builders[modal.type](modal) : null;
  }
  layer.classList.toggle('open', !!content);
  layer.replaceChildren(...(content ? [content] : []));
  layer.onclick = () => { if (!questions.length || ui.view === 'run') { if (ui.modal && ['help', 'tasks'].includes(ui.modal.type)) closeModal(); } };
}

const VIEWS = { home: viewHome, mission: viewMission, agents: viewAgents, folders: viewFolders, models: viewModels, teamwork: viewTeamwork, launch: viewLaunch, run: viewRun, resume: viewResume };


// ==============
// Start.
// ==============
function tick() {
  document.querySelectorAll('.timer').forEach(element => {
    const start = parseTime(element.dataset.start), end = parseTime(element.dataset.end);
    const text = elapsed(start, end);
    const last = element.lastChild;
    if (last && last.nodeType === Node.TEXT_NODE && last.textContent !== text) last.textContent = text;
  });
}

async function start() {
  document.documentElement.dataset.theme = currentTheme();
  document.querySelector('.splash-logo').innerHTML = LOGO;
  addEventListener('pointerdown', () => { pointerDown = true; }, true);
  addEventListener('pointerup', () => { pointerDown = false; setTimeout(scheduleRender, 0); }, true);
  addEventListener('keydown', event => {
    if (event.key === 'Escape' && ui.modal && !(store.state?.questions.length && ui.view !== 'run')) closeModal();
  });
  setInterval(tick, 1000);
  while (!store.catalog) {
    try {
      const response = await fetch('/api/catalog', { headers: { 'X-SwarmUP-Token': TOKEN } });
      if (!response.ok) throw new Error('catalog failed');
      store.catalog = await response.json();
    } catch (error) {
      document.querySelector('.splash-text').textContent = 'Waiting for SwarmUP…';
      await sleep(1000);
    }
  }
  pollLoop();
  while (!store.state) await sleep(30);
  let view = null;
  try { view = sessionStorage.getItem('swarmup-view'); } catch (error) { /* not available */ }
  ui.view = store.state.run?.running ? 'run' : (view && VIEWS[view] && view !== 'resume' ? view : 'home');
  if (ui.view === 'run' && !store.state.run) ui.view = 'home';
  ui.visited.add(ui.view);
  render();
  document.getElementById('splash').classList.add('gone');
  setTimeout(() => document.getElementById('splash').remove(), 400);
}

start();
