// SwarmUP: the interface (see app.js). The state of the page, the small tools that build it, and the calls to the program.
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
// tab is the mission this page shows (several can be open, see missions.js), and pollAbort stops the poll of the tab it leaves.
const store = { catalog: null, state: null, feed: [], feedAfter: 0, version: -1, online: true, jobs: {}, tab: null, pollAbort: null };
const ui = {
  view: 'home', modal: null, stack: [], selected: null, busy: {}, errors: {}, expanded: {}, visited: new Set(), feedFilter: 'all',
  missionDraft: null, folderDrafts: {}, waitsDraft: null, orderChoice: null, mode: null, resume: null, particles: [], seenFeed: 0,
  composer: {}, corrections: {}, messages: {}, renderedOnce: false, animate: true, forms: {}, codex: { loading: false, data: null }, budgetDraft: null, costsOpen: false,
  promptDrafts: {}, ruleDrafts: {}, specialisedOpen: false, swarmFolderDraft: null, ownFolders: null, followUp: null, afterSwitch: false,
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

// An amount of US dollars as people write it. Below a cent, four decimals are kept, so a small cost is never shown as nothing.
function dollars(amount) {
  if (amount === null || amount === undefined) return 'unknown';
  return amount > 0 && amount < 0.01 ? `$${amount.toFixed(4)}` : `$${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// The budget of the mission: what it spent, what the models of the team set aside (the price of 1 million of their tokens), and what is left.
// extra is the price of the model being chosen.
function budgetMeter(costs, extra = 0, label = 'This model') {
  if (!costs || !costs.budget) return null;
  const total = costs.budget, spent = Math.max(0, costs.spent), aside = Math.max(0, costs.setAside);
  const room = Math.max(0, total - spent - aside);
  const percent = value => `${Math.min(100, value / total * 100)}%`;
  const left = costs.left - extra;
  return h('div', {},
    h('div', { class: 'row between small', style: { marginBottom: '7px' } }, h('b', {}, `Budget of the mission · ${dollars(total)}`),
      h('span', { class: left < 0 ? 'danger-text' : 'muted' }, left < 0 ? `${dollars(-left)} over` : `${dollars(left)} left`)),
    h('div', { class: 'meter', role: 'img', 'aria-label': `Budget: ${dollars(spent)} spent, ${dollars(aside)} set aside by the models of the team${extra ? `, ${dollars(extra)} for this model` : ''}, of ${dollars(total)}` },
      h('div', { class: 'spent', style: { width: percent(spent) } }), h('div', { class: 'swarm', style: { width: percent(aside) } }),
      extra ? h('div', { class: extra <= room ? 'this' : 'over', style: { width: percent(Math.min(extra, room) || extra) } }) : null),
    h('div', { class: 'meter-legend' }, h('span', {}, h('i', { style: { background: 'var(--text-2)' } }), `Spent: ${dollars(spent)}`),
      h('span', {}, h('i', { style: { background: 'var(--primary)' } }), `Set aside by the team: ${dollars(aside)}`),
      extra ? h('span', {}, h('i', { style: { background: extra <= room ? 'var(--honey)' : 'var(--danger)' } }), `${label}: ${dollars(extra)}`) : null),
    costs.unknown?.length ? h('div', { class: 'small faint', style: { marginTop: '6px' } }, `The price of ${costs.unknown.join(', ')} is not known: it sets nothing aside.`) : null);
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

function apiHeaders(extra = {}) {
  return { ...extra, 'X-SwarmUP-Token': TOKEN, ...(store.tab ? { 'X-SwarmUP-Tab': store.tab } : {}) };
}

async function api(action, payload = {}) {
  let response;
  try {
    response = await fetch(`/api/${action}`, { method: 'POST', headers: apiHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(payload) });
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

// A poll waits for news of its tab. When the user goes to another tab, the poll is stopped and starts again for the new one.
async function pollLoop() {
  while (true) {
    const controller = store.pollAbort = new AbortController();
    const tab = store.tab;
    try {
      const response = await fetch(`/api/poll?version=${store.version}&feed=${store.feedAfter}`, { headers: apiHeaders(), signal: controller.signal });
      if (!response.ok) throw new Error('poll failed');
      const data = await response.json();
      setOnline(true);
      if (tab !== store.tab) continue;
      addFeed(data.feed);
      applyState(data.state);
    } catch (error) {
      if (controller.signal.aborted) continue;
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
  // The state of another tab (an answer that arrives after the user left it) is not drawn. A tab that was closed elsewhere gives way to the one shown.
  if (store.tab && state.tab !== store.tab && (state.tabs || []).some(tab => tab.tab === store.tab)) return;
  if (store.tab !== state.tab) { store.tab = state.tab; store.feed = []; store.feedAfter = 0; }
  if (store.state && store.state.tab === state.tab && state.version < store.state.version) return;
  if (ui.afterSwitch) { ui.afterSwitch = false; ui.view = state.run ? 'run' : state.mission ? 'mission' : 'home'; }
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
