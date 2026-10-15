// SwarmUP: the interface (see app.js). What the window shows of the MCP layer: every call of a tool in the mission (the gateway records them),
// what an agent writes while it writes, and the MCP servers of other programs that the user adds, inspects, approves and removes.
'use strict';

const OUTCOMES = { success: ['done', 'success'], error: ['error', 'danger'], refused: ['refused', 'warning'], cancelled: ['stopped', 'warning'], timeout: ['too long', 'danger'] };

// ==============
// The calls of the mission.
// ==============
async function openCalls(agent = '') {
  ui.modal = { type: 'calls', agent, server: '', calls: null, counts: {}, error: '' };
  render();
  await loadCalls();
}

async function loadCalls() {
  const modal = ui.modal;
  if (!modal || modal.type !== 'calls') return;
  try {
    const data = await api('calls', { agent: modal.agent, server: modal.server });
    Object.assign(modal, { calls: data.calls, counts: data.counts, error: '' });
  } catch (error) {
    modal.error = error.message;
  }
  render();
}

function callRow(call) {
  const [label, tone] = OUTCOMES[call.outcome] || [call.outcome, ''];
  const shown = Object.entries(call.arguments || {}).map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`).join(', ');
  return h('tr', {}, h('td', { class: 'faint' }, call.time.slice(11)), h('td', {}, call.agent), h('td', {}, h('code', {}, `${call.server}__${call.tool}`)),
    h('td', { class: 'small', title: shown }, shorten(shown, 70)), h('td', {}, `${call.seconds.toFixed(1)} s`),
    h('td', { title: call.note || '' }, badge(label, tone)));
}

function callsModal(modal) {
  const agents = Object.keys(modal.counts || {});
  const servers = [...new Set((modal.calls || []).map(call => call.server))];
  const select = (value, options, label, onChange) => h('select', { class: 'input', style: { maxWidth: '200px' }, 'aria-label': label, value, onChange: event => { onChange(event.target.value); loadCalls(); } },
    h('option', { value: '' }, label), options.map(option => h('option', { value: option, selected: option === value }, option)));
  const totals = Object.entries(modal.counts || {}).map(([name, count]) => h('li', {}, h('b', {}, name), ` ${plural(count.calls, 'call')}, ${count.errors} errors, ${count.refused} refused, ${count.cancelled} stopped, ${count.timeout} too long, ${count.seconds.toFixed(1)} s`));
  return modalFrame({ size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#5B4CF0', '--task-soft': '#5B4CF026' } }, icon('activity', 'lg')), title: 'The calls of the mission',
    subtitle: 'Every tool an agent used, through the gateway of SwarmUP: when, with what, how long it took and how it ended. No secret is ever shown.',
    body: h('div', { class: 'stack' },
      h('div', { class: 'row wrap' }, select(modal.agent, agents, 'Every agent', value => { modal.agent = value; }), select(modal.server, servers, 'Every server', value => { modal.server = value; }),
        h('span', { class: 'grow' }), button('', { kind: 'ghost', size: 'sm', iconName: 'refresh', title: 'Read them again', onClick: loadCalls })),
      modal.error ? callout('danger', 'alert', modal.error) : modal.calls === null ? loading('Reading the calls…') :
        modal.calls.length ? h('div', { style: { overflowX: 'auto' } }, h('table', { class: 'usage-table calls-table' },
          h('tr', {}, ['Time', 'Agent', 'Tool', 'With', 'Took', 'Ended'].map(title => h('th', {}, title))), modal.calls.slice().reverse().map(callRow))) :
          h('p', { class: 'muted' }, 'No call yet.'),
      totals.length ? h('ul', { class: 'small', style: { margin: '0', paddingLeft: '18px' } }, totals) : null),
    foot: [h('div', { class: 'spacer' }), button('Close', { kind: 'primary', onClick: closeModal })] });
}

// What an agent writes now, while it writes it, and what it did with its tools.
function liveSection(agent) {
  const calls = agent.calls;
  return [agent.live ? h('div', { class: 'detail-section' }, h('h4', {}, h('span', { class: 'live-dot' }), ' It writes now'), h('div', { class: 'draft-box live-text' }, agent.live)) : null,
    calls ? h('div', { class: 'detail-section row' }, h('div', { class: 'grow small muted' }, `${plural(calls.calls, 'tool call')}` + (calls.errors ? `, ${calls.errors} with an error` : '') +
      (calls.refused ? `, ${calls.refused} refused` : '')), button('Its calls', { kind: 'ghost', size: 'sm', iconName: 'list', onClick: () => openCalls(agent.name) })) : null];
}

// ==============
// The MCP servers of other programs, in the settings.
// ==============
async function loadServers(modal) {
  try {
    modal.servers = (await api('mcpServers')).servers;
  } catch (error) {
    modal.serverError = error.message;
  }
  render();
}

function inspectionView(name, tools) {
  return h('div', { class: 'stack tight' }, tools.map(tool => h('div', { class: ['card pad', tool.findings.length && 'has-error'] },
    h('div', {}, h('code', {}, `${name}__${tool.name}`)), h('div', { class: 'small muted' }, shorten(tool.description, 300)),
    tool.findings.map(finding => h('div', { class: 'field-error' }, icon('alert'), finding)))));
}

function mcpSection(modal) {
  if (modal.servers === undefined) {
    modal.servers = null;
    loadServers(modal);
  }
  const draft = modal.mcpDraft || (modal.mcpDraft = { name: '', transport: 'stdio', command: '', args: '', url: '', agents: 'all', errors: {} });
  const inspect = async (name, data) => { modal.inspection = { name, tools: data.inspection }; if (data.servers) modal.servers = data.servers; render(); };
  const add = async () => { const data = await act('mcpAdd', draft, { busy: 'mcpAdd', form: draft }); if (data) { modal.mcpDraft = null; await inspect(draft.name, data); } };
  const approve = async () => { const data = await act('mcpApprove', { name: modal.inspection.name, hashes: modal.inspection.tools.map(tool => tool.hash) }, { busy: 'mcpApprove', ok: `${modal.inspection.name} is approved.` });
    if (data) { modal.servers = data.servers; modal.inspection = null; render(); } };
  const field = (key, label, placeholder) => h('div', { class: ['field', draft.errors?.[key] && 'has-error'] }, h('label', { class: 'field-label' }, label),
    h('input', { class: 'input', 'data-key': `mcp-${key}`, value: draft[key], placeholder, onInput: event => { draft[key] = event.target.value; } }),
    draft.errors?.[key] ? h('div', { class: 'field-error' }, icon('alert'), draft.errors[key]) : null);
  return h('div', { class: 'stack' }, h('h3', {}, 'MCP servers'),
    h('p', { class: 'small muted' }, 'Other programs can give tools to your agents with MCP (GitHub, a database...). Nothing is given before you approve a server, and a tool that changes later is hidden until you approve it again.'),
    modal.serverError ? callout('danger', 'alert', modal.serverError) : modal.servers === null ? loading('Reading the servers…') :
      modal.servers.map(server => h('div', { class: 'row card pad' }, h('div', { class: 'grow' }, h('b', {}, server.name), ' ', badge(server.approved ? `approved, ${plural(server.tools, 'tool')}` : 'not approved', server.approved ? 'success' : 'warning'),
        h('div', { class: 'small faint' }, `${server.transport}: ${shorten(server.where, 80)} · for ${server.agents === 'all' ? 'every agent' : server.agents.join(', ')}`)),
        button(server.approved ? 'Inspect' : 'Inspect and approve', { size: 'sm', busy: ui.busy[`inspect-${server.name}`], onClick: async () => { const data = await act('mcpInspect', { name: server.name }, { busy: `inspect-${server.name}` }); if (data) inspect(server.name, data); } }),
        button('', { kind: 'ghost', size: 'sm', iconName: 'trash', title: `Remove ${server.name}`, onClick: async () => { const data = await act('mcpRemove', { name: server.name }); if (data) { modal.servers = data.servers; render(); } } }))),
    modal.inspection ? h('div', { class: 'stack' }, h('h4', {}, `The tools of ${modal.inspection.name}`), inspectionView(modal.inspection.name, modal.inspection.tools),
      modal.inspection.tools.some(tool => tool.findings.length) ? callout('warning', 'alert', 'Some tools say things a tool should not say. Approve only a server you trust.') : null,
      h('div', { class: 'row' }, h('span', { class: 'grow' }), button('Not now', { kind: 'ghost', onClick: () => { modal.inspection = null; render(); } }),
        button('Approve these tools', { kind: 'primary', iconName: 'check', busy: ui.busy.mcpApprove, onClick: approve }))) : null,
    h('div', { class: 'disclosure open' }, h('div', { class: 'stack tight' }, h('h4', {}, 'Add a server'),
      h('div', { class: 'row wrap' }, ['stdio', 'http'].map(kind => button(kind === 'stdio' ? 'A program of this computer' : 'A server on the network', { kind: draft.transport === kind ? 'soft' : 'ghost', size: 'sm',
        onClick: () => { draft.transport = kind; render(); } }))),
      field('name', 'Its name', 'github'),
      draft.transport === 'stdio' ? [field('command', 'The command that starts it', 'npx'), field('args', 'Its arguments', '-y @modelcontextprotocol/server-github')] : field('url', 'Its address', 'https://example.com/mcp'),
      field('agents', 'The agents that get it', 'all, or Writer, Coder'),
      h('div', {}, button('Add and inspect it', { kind: 'soft', iconName: 'plus', busy: ui.busy.mcpAdd, onClick: add })))));
}
