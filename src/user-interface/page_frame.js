// SwarmUP: the interface (see app.js). Drawing the page, the sidebar, the home page, and the first step: the mission.
'use strict';

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
      h('button', { class: 'nav-item', onClick: () => openModal({ type: 'settings', maxAgents: String(state.settings.maxAgents), errors: {}, error: '' }) }, h('span', { class: 'nav-step' }, icon('settings', 'sm')), 'Settings'),
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
  if (ui.budgetDraft === null) ui.budgetDraft = state.costs.budget ? String(state.costs.budget) : '';
  const saveMission = async () => {
    const form = {};
    const data = await act('setMission', { mission: ui.missionDraft }, { form, busy: 'mission' });
    ui.errors.mission = form.errors?.mission || '';
    if (data && ui.budgetDraft.trim() !== (state.costs.budget ? String(state.costs.budget) : '')) {
      const budgetForm = {};
      const saved = await act('setBudget', { budget: ui.budgetDraft }, { form: budgetForm, busy: 'mission' });
      ui.errors.budget = budgetForm.errors?.budget || '';
      render();
      return !!saved;
    }
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
      h('div', { class: ['card pad enter', ui.errors.budget && 'has-error'] },
        h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'budget' }, 'Budget of the mission', h('span', { class: 'opt' }, 'optional, in US dollars')),
          h('div', { class: 'input-wrap', style: { maxWidth: '260px' } }, h('span', { class: 'input-icon' }, icon('dollar', 'sm')),
            h('input', { id: 'budget', class: 'input with-icon', 'data-key': 'budget', inputmode: 'decimal', placeholder: 'No budget', value: ui.budgetDraft, onInput: event => { ui.budgetDraft = event.target.value; } })),
          ui.errors.budget ? h('div', { class: 'field-error' }, icon('alert'), ui.errors.budget) :
            h('div', { class: 'field-help' }, icon('info'), 'For the API models, the leader included. Each one you choose sets aside the price of 1 million of its tokens, and the lists only show the models that fit in what is left. Models on your GPUs and Codex cost nothing from it. What the mission spends is counted live.'))),
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
