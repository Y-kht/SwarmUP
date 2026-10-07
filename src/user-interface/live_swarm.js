// SwarmUP: the interface (see app.js). The live swarm: its map, the agent you look at, the cost of the mission, and the conversation with the leader.
'use strict';

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
        h('div', { class: 'run-bar' }, costBadge(), h('span', { class: 'grow' }),
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
        costCard(),
        h('div', { class: 'card' }, h('div', { class: 'card-head' }, icon('activity'), h('h3', { class: 'grow' }, 'The swarm'), graphLegend()),
          h('div', { class: 'card-body', style: { padding: '8px 12px' } }, flowGraph(nodes, run.stages, run.leader, { mode: run.mode, selected: selectedName, onSelect: name => { ui.selected = name; render(); } }))),
        selected ? agentDetail(run, selected) : null),
      h('div', { class: 'run-right' }, conversation(run))),
    footer: null,
    guide: null,
  };
}

// What the mission spent, as it grows: every call of a model is counted, from the building of the swarm to now.
function costBadge() {
  const costs = store.state.costs;
  const over = costs.budget && costs.spent > costs.budget;
  return h('button', { type: 'button', class: ['badge', over ? 'danger' : 'outline', 'cost-badge'], title: 'The cost of the mission so far', onClick: () => { ui.costsOpen = true; render(); } },
    icon('dollar'), `${dollars(costs.spent)}${costs.budget ? ` of ${dollars(costs.budget)}` : ' spent'}${costs.unpriced ? ' +' : ''}`);
}

function costCard() {
  const costs = store.state.costs;
  if (!costs.agents.length && !costs.budget) return null;
  const share = costs.budget ? costs.spent / costs.budget : null;
  const tone = share === null ? 'info' : share >= 1 ? 'danger' : share >= 0.8 ? 'warning' : 'success';
  const kinds = { free: 'on your GPUs, free', plan: 'your ChatGPT plan', unknown: 'price unknown', pending: 'price loading' };
  return h('div', { class: 'card cost-card' }, h('div', { class: 'card-head' }, icon('dollar'), h('h3', { class: 'grow' }, 'The cost of the mission'),
    h('span', { class: `cost-total ${tone}` }, dollars(costs.spent)), button(ui.costsOpen ? 'Hide' : 'Each agent', { kind: 'ghost', size: 'sm', iconName: ui.costsOpen ? 'chevronUp' : 'chevronDown',
      onClick: () => { ui.costsOpen = !ui.costsOpen; render(); } })),
    h('div', { class: 'card-body stack tight' },
      costs.budget ? budgetMeter(costs) : h('div', { class: 'small muted' }, 'No budget was set: every call is still counted.'),
      costs.unpriced ? callout('warning', 'alert', `${formatNumber(costs.unpriced)} tokens were used by models whose price is not known${costs.pending ? ' yet' : ''}: they are not in the total.`) : null,
      ui.costsOpen ? h('table', { class: 'usage-table' }, h('tr', {}, h('th', {}, 'Agent'), h('th', {}, 'Models'), h('th', {}, 'Calls'), h('th', {}, 'Read'), h('th', {}, 'Written'), h('th', {}, 'Cost')),
        costs.agents.map(row => h('tr', {}, h('td', {}, row.agent), h('td', {}, row.models.join(', ')), h('td', {}, formatNumber(row.calls)), h('td', {}, formatNumber(row.input)),
          h('td', {}, formatNumber(row.output)), h('td', {}, row.kinds.every(kind => kind === 'free' || kind === 'plan') ? kinds[row.kinds[0]] : dollars(row.cost),
            row.unpriced ? h('span', { class: 'faint' }, ` + ${formatNumber(row.unpriced)} tokens at an unknown price`) : null)))) : null,
      ui.costsOpen ? priceDate() : null,
      ui.costsOpen ? h('div', { class: 'small faint' }, 'Computed from the tokens of every call and the published prices: the cache, the long prompts and the hours of DeepSeek are priced like the providers bill them. Claude Code gives its own cost. Your provider\'s bill is the reference.') : null));
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
        h('tr', {}, h('th', {}, 'Agent'), h('th', {}, 'Model'), h('th', {}, 'Calls'), h('th', {}, 'Read'), h('th', {}, 'Written'), h('th', {}, 'Cost of the mission')),
        usage.map(agent => { const row = store.state.costs.agents.find(item => item.agent === agent.name);
          return h('tr', {}, h('td', {}, agent.name), h('td', {}, modelName(agent.model)), h('td', {}, formatNumber(agent.usage.calls)), h('td', {}, formatNumber(agent.usage.input)),
            h('td', {}, formatNumber(agent.usage.output)), h('td', {}, row ? dollars(row.cost) : '')); })),
        h('div', { class: 'small faint', style: { marginTop: '6px' } }, `The mission spent ${dollars(store.state.costs.spent)} in all, its building and the agents that left included.`)) : null) : null);
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
      agent.activity?.length ? h('div', { class: 'detail-section' }, h('h4', {}, finished ? 'What it did with its tools' : 'What it is doing'),
        h('ul', { class: 'action-list' }, agent.activity.slice(-8).reverse().map(item => h('li', {}, icon('activity', 'sm'), h('span', {}, h('span', { class: 'faint' }, item.time), ' ', item.text))))) : null,
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
        badge(agent.model, { cli: 'honey', local: 'primary', api: 'info' }[agent.modelKind], { cli: 'terminal', local: 'cpu', api: 'cloud' }[agent.modelKind]),
        agent.price ? badge(agent.price, 'outline', 'dollar') : null),
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
