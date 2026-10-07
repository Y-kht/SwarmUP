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
// Step 3: the folder of each agent.
// ==============
function viewFolders() {
  const agents = store.state.agents;
  const save = (agent, folder) => act('setFolder', { agentId: agent.id, folder }, { busy: `folder-${agent.id}`, form: folderForm(agent) }).then(data => {
    if (data) { ui.folderDrafts[agent.id] = undefined; toast(folder ? `${agent.name} works in ${folder}.` : `${agent.name} has no folder.`, 'success'); }
    render();
    return data;
  });
  const saveAll = async () => {
    for (const agent of agents) {
      const draft = ui.folderDrafts[agent.id];
      if (draft !== undefined && draft !== (agent.folder || '')) { if (!await save(agent, draft)) return; }
    }
    go('models');
  };
  return {
    header: header(stepEyebrow('folders'), 'Where does each agent work?', 'Optional. An agent can work inside a folder of your computer: it can read all its files, at any depth, and it saves what it makes in its swarmup-results folder.'),
    content: h('div', { class: 'agent-rows' }, agents.map(agent => folderRow(agent, save))),
    footer: footer({ onClick: () => go('agents') }, { label: 'Continue to the models', onClick: saveAll }, { text: `${agents.filter(agent => agent.folder).length} of ${agents.length} with a folder` }),
    guide: [
      { q: 'Do I need folders?', text: 'No. Without a folder, an agent reads none of your files, and what it makes is saved in agent-files/results of SwarmUP (the formatter and the math checker save next to your file).', tone: 'tip', icon: 'folder' },
      { q: 'What goes in it?', text: ['the email writer: a copy of every email sent', 'the calendar planner: calendar_events.ics', 'the writers: every approved text', 'the coder: its file and its test run'] },
      { q: 'Safety', text: 'An agent only writes inside its own folder, and only after you approve. The original of a document is never changed.' },
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
