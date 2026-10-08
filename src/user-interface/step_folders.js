// SwarmUP: the interface (see app.js). Choosing a folder or a file, and the step of the folders.
'use strict';

// ==============
// Choosing a folder or a file: the dialog of the system in the window of SwarmUP, otherwise a browser of folders drawn here.
// ==============
async function pickPath(kind, start, onPick) {
  if (store.state.nativeDialogs) {
    const data = await act('pickPath', { kind, path: start || '' }, { busy: 'pick' });
    if (data && data.path) onPick(data.path);
    return;
  }
  const modal = { type: 'browse', kind, onPick, data: null, selected: null, fileName: '', hidden: false };
  if (kind === 'path' && start) { modal.fileName = start.split(/[\\/]/).pop(); }
  openModal(modal);
  browseTo(modal, start || '');
}

async function browseTo(modal, path) {
  const data = await act('browse', { path, files: modal.kind !== 'folder', hidden: modal.hidden }, { busy: 'browse' });
  if (data) { modal.data = data.browse; modal.selected = null; }
  render();
}

function browseModal(modal) {
  const data = modal.data;
  const titles = { folder: 'Choose a folder', file: 'Choose a file', path: 'Choose where to save the file' };
  const separator = data?.separator || '/';
  const crumbs = [];
  if (data) {
    let path = data.path;
    const parts = [];
    while (path) {
      parts.unshift(path);
      const parent = path.replace(/[\\/][^\\/]*$/, '') || (path.startsWith('/') && path !== '/' ? '/' : '');
      if (parent === path || !parent) break;
      path = parent.endsWith(':') ? `${parent}${separator}` : parent;
    }
    parts.forEach(part => crumbs.push(h('button', { type: 'button', onClick: () => browseTo(modal, part) }, part.split(/[\\/]/).filter(Boolean).pop() || part), icon('chevronRight', 'sm')));
  }
  const join = (folder, name) => folder.endsWith(separator) ? folder + name : folder + separator + name;
  const chosen = modal.kind === 'folder' ? (modal.selected?.folder ? modal.selected.path : data?.path) :
    modal.kind === 'file' ? (modal.selected && !modal.selected.folder ? modal.selected.path : null) : (data && modal.fileName.trim() ? join(data.path, modal.fileName.trim()) : null);
  const pick = () => { if (!chosen) return; const onPick = modal.onPick; closeModal(); onPick(chosen); };
  return modalFrame({
    size: 'wide', iconEl: h('span', { class: 'task-icon lg', style: { '--task': '#F4A62A', '--task-soft': '#F4A62A26' } }, icon('folderOpen', 'lg')), title: titles[modal.kind],
    subtitle: modal.kind === 'folder' ? 'Open a folder, then choose it.' : modal.kind === 'file' ? 'Open folders until you see your file, then click it.' : 'Open the folder, then write the name of the file.',
    body: !data ? loading('Reading the folder…') : h('div', { class: 'row top', style: { gap: '16px' } },
      h('div', { class: 'stack tight', style: { width: '170px', flex: 'none' } }, h('h4', {}, 'Places'), data.places.map(place => h('button', { type: 'button', class: 'browser-item',
        style: { borderRadius: '8px', border: '0' }, onClick: () => browseTo(modal, place.path) }, icon(place.name === 'Home' ? 'home' : place.name.startsWith('Drive') ? 'layers' : 'folder', 'sm'), place.name))),
      h('div', { class: 'grow stack' },
        h('div', { class: 'row' }, button('', { kind: 'ghost', size: 'sm', iconName: 'arrowLeft', title: 'Up one folder', disabled: !data.parent, onClick: () => browseTo(modal, data.parent) }),
          h('div', { class: 'crumbs grow' }, crumbs),
          h('label', { class: ['toggle small', modal.hidden && 'on'], onClick: () => { modal.hidden = !modal.hidden; browseTo(modal, data.path); } }, h('span', { class: 'track' }), 'Hidden')),
        data.problem ? callout('warning', 'alert', data.problem) : null,
        h('div', { class: 'browser-list', style: { height: '320px' } }, data.entries.length ? data.entries.map(entry => h('button', { type: 'button',
          class: ['browser-item', entry.folder && 'is-folder', modal.selected?.path === entry.path && 'selected'], title: entry.folder ? 'Double-click to open' : null,
          onClick: () => { modal.selected = entry; if (!entry.folder && modal.kind === 'path') modal.fileName = entry.name; render(); }, onDblclick: () => entry.folder ? browseTo(modal, entry.path) : pick() },
          icon(entry.folder ? 'folder' : 'file'), h('span', { class: 'grow' }, entry.name), entry.folder ? icon('chevronRight', 'sm') : null)) :
          h('div', { class: 'empty small' }, modal.kind === 'folder' ? 'No folder inside.' : 'This folder is empty.')),
        modal.kind === 'path' ? h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Name of the file'),
          h('input', { class: 'input', 'data-key': 'browse-file', value: modal.fileName, placeholder: 'like solution.py', onInput: event => { modal.fileName = event.target.value; render(); } })) : null,
        chosen ? h('div', { class: 'small muted row' }, icon('check', 'sm'), h('span', { class: 'mono' }, chosen)) : null)),
    foot: [h('div', { class: 'spacer' }), button('Cancel', { kind: 'ghost', onClick: closeModal }),
      button(modal.kind === 'folder' ? (modal.selected?.folder ? `Choose "${modal.selected.name}"` : 'Choose this folder') : 'Choose', { kind: 'primary', iconName: 'check', disabled: !chosen, onClick: pick })],
  });
}


// ==============
// Step 3: the folder of the swarm, where every agent works, and a folder of its own for an agent that needs one.
// ==============
function viewFolders() {
  const state = store.state;
  const agents = state.agents;
  const save = (agent, folder) => act('setFolder', { agentId: agent.id, folder }, { busy: `folder-${agent.id}`, form: folderForm(agent) }).then(data => {
    if (data) { ui.folderDrafts[agent.id] = undefined; toast(folder ? `${agent.name} works in ${folder}.` : `${agent.name} has no folder.`, 'success'); }
    render();
    return data;
  });
  const swarmForm = ui.errors.swarmFolder = ui.errors.swarmFolder || {};
  const swarmDraft = ui.swarmFolderDraft ?? (state.swarmFolder || '');
  const saveSwarm = folder => act('setSwarmFolder', { folder }, { busy: 'swarmFolder', form: swarmForm }).then(data => {
    if (data) {
      ui.swarmFolderDraft = null;
      toast(folder ? `Every agent works in ${folder}.` : 'The agents work without a folder.', 'success');
      for (const text of data.kept || []) toast(text, 'warning');
    }
    render();
    return data;
  });
  const own = ui.ownFolders ?? state.mixedFolders;
  const saveAll = async () => {
    if (ui.swarmFolderDraft !== null && ui.swarmFolderDraft !== (state.swarmFolder || '') && !await saveSwarm(ui.swarmFolderDraft)) return;
    for (const agent of agents) {
      const draft = ui.folderDrafts[agent.id];
      if (draft !== undefined && draft !== (agent.folder || '')) { if (!await save(agent, draft)) return; }
    }
    go('models');
  };
  const swarmError = swarmForm.errors?.folder || swarmForm.error || '';
  const suggestion = agents.map(agent => agent.suggestion).find(Boolean);
  const workers = agents.filter(agent => !agent.builder);
  return {
    header: header(stepEyebrow('folders'), 'Where does the swarm work?', 'Optional. The agents work in a folder of your computer: they read all its files, at any depth, and save what they make in its swarmup-results folder.'),
    content: h('div', { class: 'stack' },
      h('div', { class: ['card pad enter', swarmError && 'has-error'] }, h('div', { class: 'field', style: { margin: '0' } },
        h('label', { class: 'field-label', for: 'swarm-folder' }, 'The folder of the swarm', h('span', { class: 'opt' }, 'optional')),
        h('div', { class: 'input-group' },
          h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('folder', 'sm')),
            h('input', { id: 'swarm-folder', class: 'input with-icon', 'data-key': 'swarm-folder', value: swarmDraft, placeholder: state.mixedFolders ? 'The agents have different folders' : 'No folder: write a path or press Browse',
              onInput: event => { ui.swarmFolderDraft = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') saveSwarm(event.target.value); },
              onBlur: event => { if (event.target.value !== (state.swarmFolder || '')) saveSwarm(event.target.value); } })),
          button('Browse…', { iconName: 'folderOpen', onClick: () => pickPath('folder', swarmDraft || suggestion || '', path => { ui.swarmFolderDraft = path; saveSwarm(path); }) }),
          state.swarmFolder ? button('', { kind: 'ghost', iconName: 'external', title: 'Open the folder', onClick: () => act('openFolder', { path: state.swarmFolder }) }) : null,
          state.swarmFolder ? button('', { kind: 'ghost', iconName: 'x', title: 'No folder', busy: ui.busy.swarmFolder, onClick: () => { ui.swarmFolderDraft = ''; saveSwarm(''); } }) : null),
        swarmError ? h('div', { class: 'field-error' }, icon('alert'), swarmError) :
          h('div', { class: 'field-help' }, icon('info'), `Every agent works in it (${plural(workers.length, 'agent')}). An agent that works on a file somewhere else keeps the folder of its file.`))),
      suggestion && suggestion !== state.swarmFolder ? h('div', {}, h('button', { class: 'pill sm', type: 'button', onClick: () => { ui.swarmFolderDraft = suggestion; saveSwarm(suggestion); } },
        icon('sparkles', 'sm'), `Use the folder of the file of an agent: ${shorten(suggestion, 60)}`)) : null,
      h('div', { class: ['disclosure', own && 'open'] },
        h('button', { type: 'button', onClick: () => { ui.ownFolders = !own; render(); } }, icon('folderOpen', 'sm'), 'A different folder for some agents', icon('chevronDown', 'sm chev')),
        own ? h('div', { class: 'disclosure-body' }, h('div', { class: 'agent-rows' }, agents.map(agent => folderRow(agent, save)))) : null)),
    footer: footer({ onClick: () => go('agents') }, { label: 'Continue to the models', onClick: saveAll }, { text: state.swarmFolder ? `In ${shorten(state.swarmFolder, 40)}` : state.mixedFolders ? 'Different folders' : 'No folder' }),
    guide: [
      { q: 'Do I need a folder?', text: 'No. Without a folder, the agents read none of your files, and what they make is saved in agent-files/results of SwarmUP (the formatter and the math checker save next to your file).', tone: 'tip', icon: 'folder' },
      { q: 'What goes in it?', text: ['every approved result, in swarmup-results', 'the files an agent creates or changes, after you allow each change', 'the email writer: a copy of every email sent', 'the calendar planner: calendar_events.ics'] },
      { q: 'Safety', text: 'An agent only writes inside its own folder, and only after you approve. Every file it changes can be put back.' },
    ],
  };
}

function folderForm(agent) {
  ui.errors[`folder-${agent.id}`] = ui.errors[`folder-${agent.id}`] || {};
  return ui.errors[`folder-${agent.id}`];
}

function folderRow(agent, save) {
  const form = folderForm(agent);
  const draft = ui.folderDrafts[agent.id] ?? agent.folder ?? '';
  const changed = draft !== (agent.folder || '');
  const error = form.errors?.folder || (form.error && !form.errors?.folder ? form.error : '');
  return h('div', { class: ['card agent-row', ui.animate && 'enter'] }, taskIcon(agent.task),
    h('div', { class: 'stack' },
      h('div', { class: 'agent-row-head' }, h('span', { class: 'agent-row-name' }, agent.name), agent.isLeader ? badge('Leader', 'honey', 'crown') : null,
        h('span', { class: 'small muted' }, taskOf(agent.task).label), h('span', { class: 'grow' }),
        agent.folder ? badge('Folder set', 'success', 'check') : badge('No folder', '', null)),
      h('div', { class: 'small muted' }, agent.folderNote),
      h('div', { class: ['input-group', error && 'has-error'] },
        h('div', { class: 'input-wrap' }, h('span', { class: 'input-icon' }, icon('folder', 'sm')),
          h('input', { class: 'input with-icon', 'data-key': `folder-${agent.id}`, value: draft, placeholder: 'No folder: write a path or press Browse',
            onInput: event => { ui.folderDrafts[agent.id] = event.target.value; }, onKeydown: event => { if (event.key === 'Enter') save(agent, event.target.value); },
            onBlur: event => { if (event.target.value !== (agent.folder || '')) save(agent, event.target.value); } })),
        button('Browse…', { iconName: 'folderOpen', onClick: () => pickPath('folder', draft || agent.suggestion || '', path => { ui.folderDrafts[agent.id] = path; save(agent, path); }) }),
        agent.folder ? button('', { kind: 'ghost', iconName: 'external', title: 'Open the folder', onClick: () => act('openFolder', { path: agent.folder }) }) : null,
        agent.folder || changed ? button('', { kind: 'ghost', iconName: 'x', title: 'No folder', busy: ui.busy[`folder-${agent.id}`], onClick: () => { ui.folderDrafts[agent.id] = ''; save(agent, ''); } }) : null),
      error ? h('div', { class: 'field-error' }, icon('alert'), error) : null,
      agent.suggestion && agent.suggestion !== agent.folder ? h('div', {}, h('button', { class: 'pill sm', type: 'button', onClick: () => { ui.folderDrafts[agent.id] = agent.suggestion; save(agent, agent.suggestion); } },
        icon('sparkles', 'sm'), `Use the folder of its file: ${shorten(agent.suggestion, 60)}`)) : null));
}
