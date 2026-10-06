// SwarmUP: the interface (see app.js). The step of the models, with the picker of a model, the budget, and the sign-in of Codex.
'use strict';

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
      h('div', { class: 'card pad enter' }, h('div', { class: 'row', style: { marginBottom: '12px' } }, icon('dollar'), h('h3', { class: 'grow' }, 'The cost of the mission'),
        state.costs.spent ? badge(`${dollars(state.costs.spent)} spent so far`, 'info') : null),
        state.costs.budget ? budgetMeter(state.costs) : h('div', { class: 'row small' }, h('span', { class: 'muted grow' }, 'No budget: every model can be chosen, and the cost is still counted while the swarm runs.'),
          h('button', { class: 'link-btn', type: 'button', onClick: () => go('mission') }, 'Set a budget'))),
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

function priceEntry(modal, name, provider) {
  return (modal.catalog.prices[provider] || []).find(entry => entry.name === name) || { status: 'unknown', price: null };
}

function priceBadge(entry) {
  if (entry.price === null || entry.price === undefined) return badge('Price unknown', 'outline');
  return badge(`${dollars(entry.price)} / 1M tokens`, entry.status === 'overBudget' ? 'danger' : 'outline', entry.status === 'overBudget' ? 'alert' : null);
}

// From when the prices are. Without internet they are those of the last connection, and the user is told so.
function priceDate() {
  const prices = store.state.prices;
  if (!prices?.fetchedAt) return null;
  return prices.offline ? callout('warning', 'wifiOff', h('b', {}, 'No internet connection.'), h('div', {}, `The prices are those of the last connection, ${prices.fetchedAt}. They may have changed since.`)) :
    h('div', { class: 'small faint row' }, icon('clock', 'sm'), `Prices of ${prices.fetchedAt}, renewed every hour.`);
}

// What the first list of models leaves out, and where to find it.
function hiddenNote(count, what) {
  return count ? h('div', { class: 'small faint row', style: { marginTop: '8px' } }, icon('info', 'sm'), `${plural(count, 'recommended model')} ${count === 1 ? 'is' : 'are'} not shown: ${what}. ${count === 1 ? 'It is' : 'They are'} in All models.`) : null;
}

function apiOption(modal, name, provider) {
  const selected = modal.selected && !modal.selected.local && modal.selected.name === name;
  const price = modal.prices[name];
  const entry = priceEntry(modal, name, provider);
  const company = store.catalog.providers[provider].company;
  const loadPrice = async event => {
    event.stopPropagation();
    const data = await act('price', { name, provider }, { busy: `price-${name}` });
    if (data) { modal.prices[name] = data.price; render(); }
  };
  return h('div', {},
    h('button', { type: 'button', class: ['model-option', selected && 'selected'], onClick: () => { modal.selected = { name, provider, local: false, company }; modal.errors = {}; modal.reveal = !store.state.keys[provider].source; render(); } },
      h('span', { class: 'radio' }), h('div', { style: { minWidth: '0' } }, h('div', { class: 'mo-name' }, name), h('div', { class: 'mo-sub' }, company)),
      h('div', { class: 'mo-side' }, priceBadge(entry), store.state.keys[provider].source ? badge('Key ready', 'success', 'key') : null,
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
    if (modal.tab === 'recommended') list = h('div', {}, h('div', { class: 'model-list' }, catalog.local.map(entry => localOption(modal, entry))),
      !catalog.local.length ? h('div', { class: 'small muted' }, 'No recommended model fits in the VRAM that is left.') : null,
      hiddenNote(catalog.hiddenLocal, `they need more VRAM than the ${Math.max(0, Math.round((catalog.gpu.total - catalog.gpu.needed) * 10) / 10)} GB the other agents leave`));
    else if (modal.tab === 'all') {
      modal.family = modal.family || families[0];
      list = h('div', {}, h('div', { class: 'pills', style: { marginBottom: '12px' } }, families.map(family => h('button', { type: 'button', class: ['pill sm', family === modal.family && 'selected'],
        onClick: () => { modal.family = family; render(); } }, family))), h('div', { class: 'model-list' }, catalog.families[modal.family].map(entry => localOption(modal, entry))));
    } else list = otherLocal(modal);
  } else {
    if (modal.tab === 'recommended') list = h('div', {}, h('div', { class: 'model-list' }, catalog.api.map(entry => apiOption(modal, entry.name, entry.provider))),
      !catalog.api.length ? h('div', { class: 'small muted' }, 'No recommended model fits in what is left of the budget.') : null,
      hiddenNote(catalog.hiddenApi, `the price of 1 million of their tokens is more than the ${dollars(Math.max(0, catalog.budget.left))} left of the budget`));
    else if (modal.tab === 'all') {
      modal.provider = modal.provider || providers[0];
      list = h('div', {}, h('div', { class: 'pills', style: { marginBottom: '12px' } }, providers.map(provider => h('button', { type: 'button', class: ['pill sm', provider === modal.provider && 'selected'],
        onClick: () => { modal.provider = provider; render(); } }, store.catalog.providers[provider].company))),
        h('div', { class: 'model-list' }, store.catalog.providers[modal.provider].models.map(name => apiOption(modal, name, modal.provider))));
    } else list = otherApi(modal);
  }
  const extra = modal.selected?.local ? modal.selected.vram || 0 : 0;
  const selectedPrice = modal.selected && !modal.selected.local ? (modal.selected.cli === 'codex' ? 0 : modal.selected.cli ?
    ((catalog.cli['claude-code'].prices || []).find(entry => entry.name === modal.selected.name)?.price || 0) : priceEntry(modal, modal.selected.name, modal.selected.provider).price || 0) : 0;
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
      where !== 'local' && where && catalog.budget.budget ? h('div', { class: 'card pad', style: { boxShadow: 'none' } }, budgetMeter(catalog.budget, selectedPrice,
        modal.selected && !modal.selected.local ? shorten(modelName(modal.selected), 30) : 'This model')) : null,
      where === 'api' ? priceDate() : null,
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
        h('div', {}, h('div', { class: 'model-list' }, agents['claude-code'].models.filter(name => name === store.catalog.defaultCliModel ||
          (modal.catalog.cli['claude-code'].prices || []).find(entry => entry.name === name)?.status !== 'overBudget').map(name => {
          if (name === store.catalog.defaultCliModel) return option('claude-code', name, 'Let Claude Code choose', 'Its default model. Its price is only known once it ran.', true);
          const entry = (modal.catalog.cli['claude-code'].prices || []).find(item => item.name === name) || {};
          return option('claude-code', name, name, entry.price !== null && entry.price !== undefined ? `Anthropic · ${dollars(entry.price)} per 1M tokens` : 'Anthropic · price unknown', false);
        })),
          (modal.catalog.cli['claude-code'].prices || []).some(entry => entry.status === 'overBudget') ? h('div', { class: 'small faint row', style: { marginTop: '8px' } }, icon('info', 'sm'),
            `${(modal.catalog.cli['claude-code'].prices || []).filter(entry => entry.status === 'overBudget').length} Anthropic models are not shown: they cost more than what is left of the budget.`) : null),
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
