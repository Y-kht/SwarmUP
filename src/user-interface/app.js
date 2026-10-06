// SwarmUP: the interface. It draws the state that the program (src/backend/interface/user_interface.py) gives, and sends it what the user does.
// The state comes from /api/poll, which answers as soon as something changed. Every view is drawn again from the state and from ui
// (what only the interface knows: the open window, what is typed...), so the interface never disagrees with the program.
// The page is made of these scripts, loaded in this order by index.html: icons.js, page_tools.js, page_frame.js, step_agents.js, step_folders.js, step_models.js, step_teamwork.js, live_swarm.js, app.js.
// This one has the swarms that were interrupted, the windows that open over the page, and the start.
'use strict';

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

// The settings of the user, kept on this computer (agent-files/settings.json) for every mission.
function settingsModal(modal) {
  const save = async () => {
    const data = await act('saveSettings', { maxAgents: modal.maxAgents }, { busy: 'saveSettings', form: modal });
    if (data) { closeModal(); toast('Your settings are saved.', 'success'); } else render();
  };
  return modalFrame({ iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#5B4CF0', '--task-soft': '#5B4CF026' } }, icon('settings', 'lg')), title: 'Settings',
    subtitle: 'Kept on this computer, for all your missions.',
    body: h('div', { class: 'stack' }, h('div', { class: ['field', modal.errors.maxAgents && 'has-error'] },
      h('label', { class: 'field-label', for: 'max-agents' }, 'The most agents a leader can put in a swarm'),
      h('input', { id: 'max-agents', class: 'input', type: 'number', min: '1', max: String(store.state.maxAgentsLimit), step: '1', style: { maxWidth: '140px' }, 'data-key': 'max-agents',
        value: modal.maxAgents, onInput: event => { modal.maxAgents = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') save(); } }),
      modal.errors.maxAgents ? h('div', { class: 'field-error' }, icon('alert'), modal.errors.maxAgents) :
        h('div', { class: 'field-help' }, icon('info'), `When the leader builds the swarm or adds agents while it runs, it never goes beyond this. The default is 10, and it can be 1 to ${store.state.maxAgentsLimit}. You can always add agents yourself.`))),
    foot: [h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal }), button('Save', { kind: 'primary', iconName: 'check', busy: ui.busy.saveSettings, onClick: save })] });
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
      join: joinModal, removeAgent: removeAgentModal, settings: settingsModal,
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
