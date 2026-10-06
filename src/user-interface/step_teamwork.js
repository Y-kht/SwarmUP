// SwarmUP: the interface (see app.js). The step of the teamwork, the map of the swarm, and the launch.
'use strict';

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
    state.costs.budget ? { ok: state.costs.left >= 0, warn: state.costs.left < 0, fix: () => go('models'), text: state.costs.left >= 0 ?
      `Budget of ${dollars(state.costs.budget)}: ${dollars(state.costs.left)} left once each API model set aside the price of 1 million of its tokens` :
      `The API models set aside ${dollars(-state.costs.left)} more than the budget of ${dollars(state.costs.budget)}: they may cost more than you planned` } : null,
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
