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
  const toggle = async (agent, name) => {
    const chosen = agent.wheels.includes(name) ? agent.wheels.filter(item => item !== name) : [...agent.wheels, name];
    await act('setWheels', { agentId: agent.id, wheels: chosen }, { busy: `wheels-${agent.id}` });
  };
  return h('div', { class: 'card enter' }, h('div', { class: 'card-head' }, icon('zap'), h('h3', { class: 'grow' }, 'The wheels of the agents')),
    h('div', { class: 'card-body stack' }, h('div', { class: 'small muted' }, 'Wheels are scripts the architect wrote from what agents of this project repeated. Give each agent only the wheels its task needs: it gets them as tools.'),
      state.agents.map(agent => h('div', { class: 'row top wrap' }, h('div', { class: 'row', style: { width: '210px' } }, taskIcon(agent.task, 'sm'), h('b', {}, agent.name)),
        h('div', { class: 'pills grow' }, wheels.map(wheel => { const on = agent.wheels.includes(wheel.name); return h('button', { type: 'button', class: ['pill sm', on && 'selected'], title: wheel.summary,
          onClick: () => toggle(agent, wheel.name) }, h('span', { class: 'check' }, on ? icon('check') : null), wheel.name); }))))));
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
    field('architectShare', 'The share of the budget the architect may spend (%)', 'When the mission has a budget. The default is 10%.', { min: '0', max: '100', step: '1' }),
    field('architectDollars', 'What the architect may spend in a mission without a budget ($)', 'The default is $1.', { min: '0', step: '0.1' }),
  ];
}
