// SwarmUP: the interface (see app.js). The missions: several are open at the same time, each in a tab of the sidebar, as the chats of a chatbot,
// and the history of the home page keeps all of them, to continue one, to follow it up with a new request, or to delete it with its memory.
'use strict';

// What the state of a mission says, in the tabs and in the history: [label, tone, icon].
const MISSION_STATES = {
  building: ['Being built', '', 'edit'], running: ['Running', 'primary', 'activity'], waiting: ['Waits for you', 'honey', 'bell'],
  finished: ['Round over', 'success', 'checkCircle'], stopped: ['Stopped', '', 'stop'], interrupted: ['Interrupted', 'warning', 'hourglass'], paused: ['Paused', 'warning', 'wifiOff'],
};

// ==============
// The tabs: one mission each. The page draws the tab it shows, and the program tells it when another tab needs the user.
// ==============
function switchTab(tab, view = 'auto') {
  if (store.tab === tab && view === 'auto') return;
  store.tab = tab;
  store.feed = [];
  store.feedAfter = 0;
  store.version = -1;
  try { sessionStorage.setItem('swarmup-tab', tab); } catch (error) { /* kept for this window only */ }
  Object.assign(ui, { modal: null, stack: [], selected: null, missionDraft: null, folderDrafts: {}, waitsDraft: null, orderChoice: null, mode: null, resume: null,
    composer: {}, corrections: {}, messages: {}, budgetDraft: null, followUp: null, afterSwitch: view === 'auto' });
  ui.visited.clear();
  store.pollAbort?.abort();
  if (view !== 'auto') go(view);
  else render();
}

async function newMission() {
  const data = await act('newTab', {}, { busy: 'newTab' });
  if (data) switchTab(data.tab, 'mission');
}

async function closeTab(tab) {
  const data = await act('closeTab', { tab }, { busy: `close-${tab}` });
  if (data && store.tab === tab) switchTab(data.tab);
}

function missionTabs() {
  const state = store.state;
  return [h('div', { class: 'nav-title' }, 'Your missions'),
    h('div', { class: 'mission-tabs' }, state.tabs.map(tab => {
      const [label] = MISSION_STATES[tab.state] || MISSION_STATES.building;
      return h('div', { class: ['mission-tab', tab.tab === state.tab && 'active'] },
        h('button', { class: 'mission-tab-main', title: `${tab.title} (${label}${tab.round > 1 ? `, round ${tab.round}` : ''})`, onClick: () => switchTab(tab.tab) },
          h('span', { class: ['mission-dot', tab.state] }), h('span', { class: 'mission-tab-title' }, shorten(tab.title, 30))),
        state.tabs.length > 1 && !['running', 'waiting'].includes(tab.state) ? h('button', { class: 'mission-tab-close', title: 'Close this tab (the mission stays in the history)',
          'aria-label': 'Close this tab', onClick: () => closeTab(tab.tab) }, icon('x', 'sm')) : null);
    })),
    h('button', { class: 'nav-item', disabled: ui.busy.newTab, onClick: newMission, title: 'Open a new tab and build another swarm. The others go on.' },
      h('span', { class: 'nav-step' }, icon('plus', 'sm')), 'New mission')];
}


// ==============
// The history: every mission, the latest first.
// ==============
function setResume(form) {
  ui.resume = { form, values: Object.fromEntries(form.agents.map(agent => [agent.name, { accounts: [] }])), keys: {}, tokens: {}, errors: {}, error: '' };
  if (form.codex) loadCodex();
}

async function openMission(mission) {
  const data = await act('openMission', { id: mission.id }, { busy: `open-${mission.id}` });
  if (!data) return;
  if (!data.resume) return switchTab(data.tab);
  switchTab(data.tab, 'resume');
  setResume(data.resume);
  render();
}

function historySection() {
  const missions = store.state.history || [];
  if (!missions.length) return null;
  return h('section', { class: 'stack enter-2' },
    h('div', { class: 'section-title' }, h('h2', {}, 'Your missions'), badge(String(missions.length), 'honey'), h('span', { class: 'grow' }),
      button('', { kind: 'ghost', size: 'sm', iconName: 'refresh', title: 'Read the history again', onClick: () => act('refreshUnfinished') })),
    h('p', { class: 'small muted' }, 'Every swarm you ran stays here with its memory, until you delete it. Open one to continue it where it stopped, or to give it a new request: its agents remember what they did.'),
    missions.map(historyCard));
}

function historyCard(mission) {
  const [label, tone, iconName] = MISSION_STATES[mission.state] || MISSION_STATES.stopped;
  const tab = store.state.tabs.find(item => item.missionId === mission.id);
  const elsewhere = mission.state === 'running' && !tab;
  const open = tab ? button('Go to it', { kind: 'primary', iconName: 'arrowRight', onClick: () => switchTab(tab.tab) }) :
    mission.canContinue ? button('Continue', { kind: 'primary', iconName: 'play', disabled: !mission.canResume, busy: ui.busy[`open-${mission.id}`], onClick: () => openMission(mission) }) :
      mission.canFollowUp ? button('Follow up', { kind: 'primary', iconName: 'message', disabled: !mission.canResume, busy: ui.busy[`open-${mission.id}`], onClick: () => openMission(mission) }) : null;
  return h('div', { class: 'card saved-card' }, h('span', { class: 'saved-icon' }, icon(iconName, 'lg')),
    h('div', { class: 'grow stack tight' },
      h('h3', {}, shorten(mission.mission, 140)),
      h('div', { class: 'row wrap small muted' }, badge(label, tone), mission.round > 1 ? badge(`Round ${mission.round}`, 'outline') : null,
        `${plural(mission.agents.length, 'agent')} · ${mission.savedAt}`),
      mission.round > 1 ? h('div', { class: 'small' }, h('span', { class: 'faint' }, 'Latest request: '), shorten(mission.latest, 160)) : null,
      elsewhere ? callout('info', 'info', 'It is running in another session of SwarmUP (a window or the command line). Go there to follow it.') : null,
      !mission.canResume ? callout('warning', 'alert', 'This mission was made by another program, so it cannot be continued here.') : null),
    h('div', { class: 'stack tight' }, open,
      mission.canContinue && !tab ? button('Cancel it', { kind: 'danger-ghost', iconName: 'x', onClick: () => openCancel({ id: mission.id, mission: mission.mission, leader: mission.leader }) }) : null,
      button('Delete', { kind: 'ghost', iconName: 'trash', disabled: elsewhere || (tab && ['running', 'waiting'].includes(tab.state)), title: 'Delete it, with its memory and its temp folder',
        onClick: () => openModal({ type: 'deleteMission', mission }) })));
}

function deleteMissionModal(modal) {
  const mission = modal.mission;
  return confirmModal({ title: 'Delete this mission?', iconName: 'trash', confirm: 'Delete it', danger: true,
    text: `"${shorten(mission.mission, 100)}" leaves the history, with its memory (what the user asked, what every agent did, its notes) and its temp folder. It cannot be followed up anymore. What its agents saved in your folders stays. The long-term memory, which every swarm reads, is not changed.`,
    run: async () => { if (await act('deleteMission', { id: mission.id }, { busy: 'confirm', ok: 'The mission is deleted.' })) closeModal(); } });
}


// ==============
// Following up: when a round is over, the user gives the same swarm a new request, as in a chat.
// ==============
function followUpCard(run) {
  if (!run.canFollowUp) return null;
  const draft = ui.followUp || (ui.followUp = { text: '', mode: 'execute', errors: {}, error: '' });
  const send = async () => {
    const data = await act('followUp', { request: draft.text, mode: draft.mode }, { busy: 'followUp', form: draft });
    if (data) { ui.followUp = null; toast(`Round ${run.round + 1} starts: the leader gives each agent its part.`, 'success'); }
  };
  const modes = [['execute', 'play', 'Do it'], ['plan', 'list', 'Plan first']];
  return h('div', { class: ['card pad followup-card enter', draft.errors?.request && 'has-error'] },
    h('div', { class: 'row' }, icon('message'), h('h3', { class: 'grow' }, `Follow up: round ${run.round + 1}`), run.round > 1 ? badge(`${run.round} rounds so far`, 'outline') : null),
    h('p', { class: 'small muted' }, 'Give the same swarm a new request, as in a chat. The leader sends each part to the agents it concerns, and they remember what they did. The others keep their result.'),
    h('textarea', { class: 'textarea', 'data-key': 'follow-up', value: draft.text, placeholder: 'For example: now make it shorter, and add a summary at the top.',
      onInput: event => { draft.text = event.target.value; }, onKeydown: event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') send(); } }),
    draft.errors?.request ? h('div', { class: 'field-error' }, icon('alert'), draft.errors.request) : null,
    h('div', { class: 'row wrap' }, modes.map(([mode, iconName, label]) => button(label, { kind: draft.mode === mode ? 'soft' : 'ghost', size: 'sm', iconName,
      onClick: () => { draft.mode = mode; render(); } })), h('span', { class: 'grow' }),
    button('Send to the swarm', { kind: 'primary', iconName: 'send', busy: ui.busy.followUp, onClick: send })));
}
