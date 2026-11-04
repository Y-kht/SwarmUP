// SwarmUP: the interface (see app.js). The architect of the swarm: its model in the step of the models, what it does while the swarm runs,
// the wheels of each agent in the step of the teamwork, and its settings.
'use strict';

// The model of the architect: the same as an agent of the swarm (the leader by default), or a model of its own.
function architectRow(architect) {
  if (!architect) return null;
  const own = architect.model;
  const choose = () => openModelPicker({ id: architect.id, name: 'Architect', task: 'architect', model: own });
  return h('div', { class: ['card agent-row', ui.animate && 'enter'] }, taskIcon('architect'),
    h('div', { class: 'stack' },
      h('div', { class: 'agent-row-head' }, h('span', { class: 'agent-row-name' }, 'Architect'), badge('In the background', 'outline', 'layers'),
        h('span', { class: 'small muted' }, 'Turns what the agents repeat into wheels'), h('span', { class: 'grow' }),
        button(own ? 'Change' : 'A model of its own', { size: 'sm', iconName: own ? 'refresh' : 'plus', onClick: choose })),
      own ? h('div', { class: 'row wrap' }, h('span', { class: 'model-chip' }, icon(modelIcon(own), 'sm'), h('span', {}, modelName(own))),
        architect.options.length ? button('Use the model of an agent instead', { kind: 'ghost', size: 'sm', onClick: () => act('setArchitect', { same: architect.options[0] }) }) : null) :
        architect.options.length ? h('div', { class: 'pills' }, architect.options.map(name => h('button', { type: 'button', class: ['pill sm', architect.same === name && 'selected'],
          onClick: () => act('setArchitect', { same: name }) }, h('span', { class: 'check' }, architect.same === name ? icon('check') : null), `Same as ${name}`))) :
          h('div', { class: 'small faint' }, 'No agent has a model it can use yet: choose one for it, or it only counts what the agents repeat.'),
      h('div', { class: 'small muted' }, 'It reads the work of the agents and writes scripts, so a capable model is worth it. It spends at most its share of the budget, and never asks you anything.')));
}

// What the architect does while the swarm runs: its passes, the wheels it made, its last report.
function architectCard(run) {
  const architect = run.architect;
  if (!architect) return null;
  const working = architect.state === 'working';
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, taskIcon('architect', 'sm'), h('h3', { class: 'grow' }, 'The architect'),
    working ? statusPill({ label: 'Working', tone: 'primary', iconName: 'sparkles', live: true }) : badge(architect.available ? 'Waiting for patterns' : 'Counting only', 'outline'),
    architect.spent ? badge(dollars(architect.spent), 'outline', 'dollar') : null),
    h('div', { class: 'card-body stack tight' },
      h('div', { class: 'small muted' }, architect.available ? `${architect.model} · ${plural(architect.passes, 'pass', 'passes')}` :
        'No model can run the architect in this swarm: the patterns of the agents are still counted for the next swarms.'),
      architect.wheels.length ? h('div', { class: 'stack tight' }, architect.wheels.slice(-5).reverse().map(wheel => h('div', { class: 'row small top' }, icon(wheel.action === 'retired' ? 'trash' : 'zap', 'sm'),
        h('div', {}, h('b', {}, `${wheel.name} v${wheel.version}`), ` ${wheel.action}: ${wheel.summary}`)))) : null,
      architect.failure ? callout('warning', 'alert', architect.failure) : null,
      architect.report ? h('details', { class: 'small' }, h('summary', {}, `Its last report (${architect.report.time})`), h('div', { style: { whiteSpace: 'pre-wrap' } }, architect.report.text)) : null));
}

// The wheels of each agent, in the step of the teamwork: only when the project has wheels.
function wheelsCard() {
  const state = store.state;
  const wheels = state.architect?.wheels || [];
  if (!wheels.length || !state.agents.length) return null;
  return h('div', { class: 'card enter' }, h('div', { class: 'card-head' }, icon('zap'), h('h3', { class: 'grow' }, 'The wheels of the agents')),
    h('div', { class: 'card-body stack' }, h('div', { class: 'small muted' }, 'Wheels are scripts the architect wrote from what agents of this project repeated. Give each agent only the wheels its task needs: it gets them as tools.'),
      state.agents.map(agent => h('div', { class: 'row top wrap' }, h('div', { class: 'row', style: { width: '210px' } }, taskIcon(agent.task, 'sm'), h('b', {}, agent.name)),
        wheelPills(agent.id, agent.wheels, wheels)))));
}

// The settings of the tools and of the architect, in the modal of the settings.
function architectSettings(modal) {
  const field = (key, label, help, attributes) => h('div', { class: ['field', modal.errors[key] && 'has-error'] },
    h('label', { class: 'field-label', for: `setting-${key}` }, label),
    h('input', { id: `setting-${key}`, class: 'input', type: 'number', style: { maxWidth: '140px' }, 'data-key': `setting-${key}`, value: modal[key], ...attributes,
      onInput: event => { modal[key] = event.target.value; } }),
    modal.errors[key] ? h('div', { class: 'field-error' }, icon('alert'), modal.errors[key]) : h('div', { class: 'field-help' }, icon('info'), help));
  return [
    field('maxTools', 'The most tools one agent is given', `An agent with more tools gets the first ones, and you are told. The default is 1000 (no real limit); it can be 10 to ${store.state.maxToolsLimit}.`, { min: '10', step: '1' }),
    field('maxSteps', 'The most steps of one request of an agent', `At the last step the agent must answer, even if its work is not finished. The default is 60; it can be 5 to ${store.state.maxStepsLimit}.`, { min: '5', step: '1' }),
    field('architectShare', 'The share of the budget the architect may spend (%)', 'When the mission has a budget. The default is 10%.', { min: '0', max: '100', step: '1' }),
    field('architectDollars', 'What the architect may spend in a mission without a budget ($)', 'The default is $1.', { min: '0', step: '0.1' }),
  ];
}

// A wheel to give or take back, as a pill. wheels are the wheels of the project, given the names the agent has.
function wheelPills(agentId, given, wheels) {
  const toggle = name => act('setWheels', { agentId, wheels: given.includes(name) ? given.filter(item => item !== name) : [...given, name] }, { busy: `wheels-${agentId}` });
  return h('div', { class: 'pills grow' }, wheels.map(wheel => { const on = given.includes(wheel.name); return h('button', { type: 'button', class: ['pill sm', on && 'selected'], title: wheel.summary,
    onClick: () => toggle(wheel.name) }, h('span', { class: 'check' }, on ? icon('check') : null), wheel.name); }));
}

// The wheels of an agent while the swarm runs: the leader gives them, and the user can give them or take them back too. They are its tools from its next step.
function agentWheels(run, agent) {
  const spec = store.state.agents.find(item => item.name === agent.name);
  const wheels = store.state.architect?.wheels || [];
  const given = agent.wheels || [];
  const working = run.running && !['done', 'failed'].includes(agent.status);
  if (!given.length && !(working && wheels.length)) return null;
  return h('div', { class: 'detail-section' }, h('h4', {}, 'Its wheels'),
    working && spec ? wheelPills(spec.id, given, wheels) : h('p', { class: 'small muted' }, given.join(', ')),
    working ? h('div', { class: 'small faint', style: { marginTop: '6px' } }, 'Scripts the architect wrote from what agents repeated. It gets them as tools from its next step.') : null);
}


// ==============
// What the architect learned, project by project: the wheels with their approvals, the patterns the agents repeat, what the architect reads, a
// search of the journals, and the notes of the architects (as swarmup_cli.py patterns and wheels in the command line).
// ==============
const LEARNING_TABS = [['wheels', 'Wheels', 'zap'], ['patterns', 'Patterns', 'layers'], ['brief', 'What the architect reads', 'book'], ['search', 'Search the journals', 'search'], ['notes', 'Notes', 'edit']];

function learningPage() {
  return ui.learning || (ui.learning = { tab: 'wheels', project: null, data: null, loading: false, error: '', kind: '', status: '', patterns: null, brief: null, query: '', found: null, noteQuery: '', notes: null });
}

// The projects and the wheels of the chosen project, then what the open tab shows.
async function loadLearning(project) {
  const page = learningPage();
  page.loading = true;
  try {
    const data = await api('learning', { project: project === undefined ? page.project : project });
    Object.assign(page, { data, project: data.project, error: '', patterns: null, brief: null, found: null });
  } catch (error) {
    page.error = error.message;
  }
  page.loading = false;
  render();
  if (page.project) await loadLearningTab();
}

async function loadLearningTab() {
  const page = learningPage();
  try {
    if (page.tab === 'patterns') page.patterns = (await api('patterns', { project: page.project, kind: page.kind, status: page.status })).patterns;
    if (page.tab === 'brief') page.brief = (await api('patternBrief', { project: page.project })).text;
    if (page.tab === 'notes') page.notes = (await api('architectNotes', { query: page.noteQuery })).notes;
    page.error = '';
  } catch (error) {
    page.error = error.message;
  }
  render();
}

async function searchLearning() {
  const page = learningPage();
  if (!page.query.trim()) return;
  const data = await act('searchPatterns', { project: page.project, text: page.query }, { busy: 'searchPatterns' });
  if (data) page.found = data.text;
  render();
}

async function mineLearning(rebuild) {
  const page = learningPage();
  const data = await act('minePatterns', { project: page.project, rebuild }, { busy: rebuild ? 'mineAll' : 'mineNew' });
  if (data) { toast(data.text, 'success'); await loadLearning(page.project); }
}

function dataBox(text) {
  return h('pre', { class: 'permission-detail learning-text' }, text);
}

async function openWheel(name) {
  const data = await act('wheelDetail', { name }, { busy: `wheel-${name}` });
  if (data) openModal({ type: 'wheel', wheel: data.wheel });
}

async function openPattern(id) {
  const data = await act('patternDetail', { project: learningPage().project, id }, { busy: `pattern-${id}` });
  if (data) openModal({ type: 'pattern', id, text: data.text });
}

function wheelRow(wheel) {
  return h('div', { class: 'card pad stack tight' },
    h('div', { class: 'row wrap' }, icon('zap', 'sm'), h('b', {}, wheel.name), badge(`v${wheel.version}`, 'outline'), badge(wheel.kind === 'script' ? 'A script' : 'Steps with tools', 'outline'),
      badge(wheel.scope === 'global' ? 'Every project' : 'This project', 'outline'), wheel.kind !== 'script' ? badge('Each of its calls asks like a call of the agent', 'outline', 'shield') :
        wheel.approved ? badge('Allowed always', 'success', 'checkCircle') : badge('Asks before it runs', 'honey', 'shield'),
      h('span', { class: 'grow' }), button('Its code', { size: 'sm', iconName: 'terminal', busy: ui.busy[`wheel-${wheel.name}`], onClick: () => openWheel(wheel.name) }),
      button('Retire', { kind: 'danger-ghost', size: 'sm', iconName: 'trash', onClick: () => openModal({ type: 'retireWheel', name: wheel.name, why: '' }) })),
    h('div', { class: 'small' }, wheel.summary),
    h('div', { class: 'small faint' }, [wheel.usage, wheel.author && `Written with ${wheel.author}`, wheel.updated].filter(Boolean).join(' · ')));
}

// A wheel is code a model wrote: it is allowed always only from the window that shows its code.
function wheelModal(modal) {
  const wheel = modal.wheel;
  const change = async (action, ok) => {
    const data = await act(action, { name: wheel.name }, { busy: action, ok });
    if (data) { closeModal(); await loadLearning(); }
  };
  return modalFrame({ size: 'wide', iconEl: taskIcon('architect'), title: `${wheel.name} v${wheel.version}`, subtitle: wheel.summary,
    body: h('div', { class: 'stack' }, wheel.changes ? callout('warning', 'alert', 'It changes files.') : null, wheel.network ? callout('warning', 'alert', 'It reaches the internet.') : null,
      wheel.covers.length ? h('div', { class: 'small muted' }, `It covers ${plural(wheel.covers.length, 'pattern')} of what the agents repeated.`) : null, dataBox(wheel.text)),
    foot: [h('div', { class: 'spacer' }), button('Close', { kind: 'ghost', onClick: closeModal }),
      wheel.kind !== 'script' ? null : wheel.approved ? button('Ask again before it runs', { iconName: 'shield', busy: ui.busy.revokeWheel, onClick: () => change('revokeWheel', `${wheel.name} asks again before it runs.`) }) :
        button('Always allow this version', { kind: 'primary', iconName: 'checkCircle', busy: ui.busy.approveWheel, onClick: () => change('approveWheel', `This version of ${wheel.name} runs without asking.`) })] });
}

function retireWheelModal(modal) {
  const retire = async () => {
    const data = await act('retireWheel', { name: modal.name, why: modal.why }, { busy: 'retireWheel', ok: `${modal.name} is retired.` });
    if (data) { closeModal(); await loadLearning(); }
  };
  return modalFrame({ iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#E5484D', '--task-soft': '#E5484D26' } }, icon('trash', 'lg')), title: `Retire ${modal.name}?`,
    subtitle: 'No agent gets it anymore, and the architect will not write it again unless the agents need it.',
    body: h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Why', h('span', { class: 'opt' }, 'optional')),
      h('input', { class: 'input', 'data-key': 'retire-why', value: modal.why, placeholder: 'For example: it does not fit this project.', onInput: event => { modal.why = event.target.value; } })),
    foot: [h('div', { class: 'spacer' }), button('Keep it', { kind: 'ghost', onClick: closeModal }), button('Retire it', { kind: 'danger', iconName: 'trash', busy: ui.busy.retireWheel, onClick: retire })] });
}

function patternModal(modal) {
  return modalFrame({ size: 'wide', iconEl: taskIcon('architect'), title: `Pattern #${modal.id}`, subtitle: 'Where it was seen, with examples from the journals of the agents.', body: dataBox(modal.text),
    foot: [h('div', { class: 'spacer' }), button('Close', { kind: 'primary', onClick: closeModal })] });
}

function patternsTab(page) {
  const select = (value, options, label, onChange) => h('select', { class: 'input', style: { maxWidth: '200px' }, 'aria-label': label, value, onChange: event => { onChange(event.target.value); loadLearningTab(); } },
    h('option', { value: '' }, label), options.map(option => h('option', { value: option, selected: option === value }, option)));
  return h('div', { class: 'stack' }, h('div', { class: 'row wrap' }, select(page.kind, page.data.kinds, 'Every kind', value => { page.kind = value; }), select(page.status, page.data.statuses, 'Every status', value => { page.status = value; })),
    page.patterns === null ? loading('Reading the patterns…') : !page.patterns.length ? h('p', { class: 'muted' }, 'No pattern matches.') :
      h('div', { style: { overflowX: 'auto' } }, h('table', { class: 'usage-table calls-table' }, h('tr', {}, ['', 'Kind', 'Times', 'Requests', 'Missions', 'Status', 'What'].map(title => h('th', {}, title))),
        page.patterns.map(row => h('tr', { class: 'clickable', title: 'See it with examples', onClick: () => openPattern(row.id) }, h('td', { class: 'faint' }, `#${row.id}`), h('td', {}, row.kind), h('td', {}, String(row.count)),
          h('td', {}, String(row.requests)), h('td', {}, String(row.missions)), h('td', {}, badge(row.wheel ? `wheel ${row.wheel}` : row.status, row.status === 'repeating' ? 'honey' : row.status === 'covered' ? 'success' : 'outline')),
          h('td', { class: 'small' }, shorten(row.label, 130), row.hints.length ? h('div', { class: 'faint' }, row.hints.join(', ')) : null))))));
}

function learningTab(page) {
  if (page.tab === 'wheels') {
    return page.data.wheels.length ? h('div', { class: 'stack' }, page.data.wheels.map(wheelRow)) : h('p', { class: 'muted' }, 'No wheel yet: the architect writes them from what the agents repeat.');
  }
  if (page.tab === 'patterns') return patternsTab(page);
  if (page.tab === 'brief') return page.brief === null ? loading('Reading the brief…') : dataBox(page.brief);
  if (page.tab === 'search') {
    return h('div', { class: 'stack' }, h('div', { class: 'row' }, h('input', { class: 'input grow', 'data-key': 'learning-query', value: page.query, placeholder: 'A word or a phrase the agents wrote, called or got',
      onInput: event => { page.query = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') searchLearning(); } }),
      button('Search', { kind: 'soft', iconName: 'search', busy: ui.busy.searchPatterns, onClick: searchLearning })), page.found === null ? null : dataBox(page.found));
  }
  return h('div', { class: 'stack' }, h('div', { class: 'row' }, h('input', { class: 'input grow', 'data-key': 'learning-notes', value: page.noteQuery, placeholder: 'Only the notes about these words',
    onInput: event => { page.noteQuery = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') loadLearningTab(); } }), button('Read', { kind: 'soft', iconName: 'search', onClick: loadLearningTab })),
    page.notes === null ? loading('Reading the notes…') : page.notes.length ? page.notes.map(note => h('div', { class: 'card pad small' }, note.text)) : h('p', { class: 'muted' }, 'No note of an architect yet.'));
}

function viewLearning() {
  const page = learningPage();
  if (!page.data && !page.loading && !page.error) loadLearning();
  const data = page.data;
  const counts = data?.counts;
  const content = page.error ? callout('danger', 'alert', page.error) : !data ? loading('Reading what the architect learned…') : !data.projects.length ?
    h('div', { class: 'card pad' }, h('p', { class: 'muted' }, 'Nothing yet: the architect learns from the swarms that run. Every swarm counts what its agents repeat.')) :
    h('div', { class: 'stack loose' },
      h('div', { class: 'row wrap' }, data.projects.length > 1 ? h('select', { class: 'input', style: { maxWidth: '360px' }, 'aria-label': 'The project', onChange: event => loadLearning(event.target.value) },
        data.projects.map(project => h('option', { value: project.id, selected: project.id === page.project }, `${project.name} (${plural(project.missions, 'mission')})`))) :
        h('b', {}, data.projects[0].name),
        counts ? h('span', { class: 'small muted' }, `${plural(counts.patterns, 'pattern')} from ${plural(counts.missions, 'mission')}`) : null, h('span', { class: 'grow' }),
        button('Read the journals now', { size: 'sm', iconName: 'refresh', busy: ui.busy.mineNew, onClick: () => mineLearning(false) }),
        button('Read everything again', { kind: 'ghost', size: 'sm', busy: ui.busy.mineAll, title: 'What the architect dismissed or noted stays.', onClick: () => mineLearning(true) })),
      h('div', { class: 'pills' }, LEARNING_TABS.map(([tab, label, iconName]) => h('button', { type: 'button', class: ['pill', page.tab === tab && 'selected'], onClick: () => { page.tab = tab; render(); loadLearningTab(); } }, icon(iconName, 'sm'), label))),
      learningTab(page));
  return {
    header: header('The architect', 'Patterns and wheels', 'What your agents repeat, and the scripts the architect wrote from it, so the next agents do in one call what took them many steps.'),
    content,
    guide: [
      { q: 'What is a wheel?', icon: 'zap', text: 'A script the architect wrote from what agents did again and again. An agent gets it as a tool, only when its leader or you give it.' },
      { q: 'You stay in control', icon: 'shield', tone: 'tip', text: 'A wheel is code that a model wrote. The first time an agent runs a new version, you see its code. You can read it here too, allow this version always, ask again, or retire it.' },
      { q: 'What is a pattern?', icon: 'layers', text: 'Something the agents of a project repeat: a step, a sequence, a mistake and its fix, reasoning written again. Only what repeats, and costs, can become a wheel.' },
    ],
  };
}
