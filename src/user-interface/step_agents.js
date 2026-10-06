// SwarmUP: the interface (see app.js). The step of the agents, with the form of the task of each agent.
'use strict';

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
