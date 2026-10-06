// The icons of SwarmUP: 24 x 24 line drawings, drawn with the colour of the text around them (currentColor).
// Each one is the inside of an <svg>. icon(name) in app.js makes the element.
const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18.5 14.2A6.5 6.5 0 0 1 21.5 20"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  folderOpen: '<path d="M3 18V7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V11"/><path d="M3 18l2.6-6.2A1.5 1.5 0 0 1 7 11h13.3a1 1 0 0 1 .9 1.4L18.6 18.6A2 2 0 0 1 16.8 20H5a2 2 0 0 1-2-2z"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  cloud: '<path d="M7 18.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4 4 0 0 1-.5 7.97V16.5"/><path d="M7 18.5h10.5"/>',
  workflow: '<rect x="3" y="3" width="7" height="6" rx="1.5"/><rect x="14" y="15" width="7" height="6" rx="1.5"/><rect x="3" y="15" width="7" height="6" rx="1.5"/><path d="M6.5 9v6M10 18h4M10 6h4.5a3 3 0 0 1 3 3v6"/>',
  rocket: '<path d="M12.5 15.5 8.5 11.5c1.4-4 4.6-7.6 11-8.5-.9 6.4-4.5 9.6-8.5 11z"/><path d="M8.5 11.5 5 11l2.5-3.5H11M12.5 15.5l.5 3.5 3.5-2.5V13"/><path d="M6 16c-1.5.5-2.5 2.5-2.5 4.5 2 0 4-1 4.5-2.5"/><circle cx="15" cy="9" r="1.3"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/>',
  calendar: '<rect x="3" y="4.5" width="18" height="16" rx="2"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4"/><path d="M8 13.5h2M14 13.5h2M8 17h2"/>',
  newspaper: '<path d="M4 5h13v13a2 2 0 0 0 2 2H6a2 2 0 0 1-2-2z"/><path d="M17 9h3v9a2 2 0 0 1-4 0"/><path d="M7.5 8.5h6v3.5h-6zM7.5 15h6M7.5 17.5h4"/>',
  pen: '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/><path d="M14.5 5.5l3 3"/><path d="M13 20h8"/>',
  book: '<path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H20v15H5.5A1.5 1.5 0 0 0 4 19.5z"/><path d="M4 19.5A1.5 1.5 0 0 0 5.5 21H20v-3"/><path d="M8.5 7.5h7M8.5 11h5"/>',
  layout: '<path d="M4 6h16M4 10h10M4 14h16M4 18h8"/>',
  sigma: '<path d="M18 6V4H6l7 8-7 8h12v-2"/>',
  code: '<path d="m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/>',
  crown: '<path d="M3 8l4.5 4L12 5l4.5 7L21 8l-2 10H5z"/><path d="M5 21h14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/>',
  arrowLeft: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronUp: '<path d="m6 15 6-6 6 6"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16.5 9.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  xCircle: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
  alert: '<path d="M10.3 3.9 2.4 17.6A2 2 0 0 0 4.1 20.6h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6h.01"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 2-2.8 2.5-2.8 4.2M12 17.4h.01"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12.5" rx="2"/><path d="M8.5 20.5h7M12 16.5v4"/>',
  key: '<circle cx="8" cy="15" r="4.5"/><path d="m11.2 11.8 8.3-8.3M16.5 6.5l2.5 2.5M14 9l2 2"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10.5" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/><path d="M12 14.5v2.5"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M3 3l18 18"/><path d="M10.6 5.6A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3 3.8M6.6 6.6C3.9 8.4 2.5 12 2.5 12S6 18.5 12 18.5a9 9 0 0 0 4.4-1.1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  download: '<path d="M12 3.5v12M7 11l5 5 5-5"/><path d="M4 20.5h16"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/>',
  refresh: '<path d="M20 11.5A8 8 0 0 0 5.6 6.6L4 8.5"/><path d="M4 3.5v5h5"/><path d="M4 12.5a8 8 0 0 0 14.4 4.9L20 15.5"/><path d="M20 20.5v-5h-5"/>',
  play: '<path d="M7 4.5v15l12.5-7.5z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  send: '<path d="M21 3 10 14"/><path d="m21 3-7 18-4-7-7-4z"/>',
  message: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v10a1.5 1.5 0 0 1-1.5 1.5H9l-5 4z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 21h4"/>',
  sparkles: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  shield: '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.3 7.5 9.5 4.3-1.2 7.5-4.9 7.5-9.5V6z"/><path d="m8.8 12 2.2 2.2 4.4-4.4"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  power: '<path d="M12 3v8"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/>',
  zap: '<path d="M13 2.5 4.5 13.5h7L10.5 21.5 19.5 10h-7z"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1" fill="currentColor"/><circle cx="4.5" cy="12" r="1" fill="currentColor"/><circle cx="4.5" cy="18" r="1" fill="currentColor"/>',
  gauge: '<path d="M4.2 17.5a9 9 0 1 1 15.6 0"/><path d="m12 13 4-5"/><circle cx="12" cy="13" r="1.4"/>',
  hourglass: '<path d="M6.5 3h11M6.5 21h11"/><path d="M7.5 3c0 4.5 4.5 6 4.5 9s-4.5 4.5-4.5 9M16.5 3c0 4.5-4.5 6-4.5 9s4.5 4.5 4.5 9"/>',
  wifiOff: '<path d="M3 3l18 18"/><path d="M8.5 16.4a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 4.2-2.4M19 12.9a10 10 0 0 0-2.6-1.8M2 9.3a15 15 0 0 1 4.3-2.8M22 9.3A15 15 0 0 0 11 5.5"/><path d="M12 20h.01"/>',
  phone: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.7 5.6 3.7 9s-1.2 6.4-3.7 9c-2.5-2.6-3.7-5.6-3.7-9S9.5 5.6 12 3z"/>',
  grip: '<circle cx="9" cy="6" r="1.2" fill="currentColor"/><circle cx="15" cy="6" r="1.2" fill="currentColor"/><circle cx="9" cy="12" r="1.2" fill="currentColor"/><circle cx="15" cy="12" r="1.2" fill="currentColor"/><circle cx="9" cy="18" r="1.2" fill="currentColor"/><circle cx="15" cy="18" r="1.2" fill="currentColor"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M12.5 15h4.5"/>',
  dollar: '<path d="M12 2.5v19"/><path d="M16.5 6.5a4 4 0 0 0-4-2h-1a3.5 3.5 0 0 0 0 7h1a3.5 3.5 0 0 1 0 7h-1.5a4 4 0 0 1-4-2"/>',
  flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
};

// The task of an agent: its icon and its colour, used everywhere the agent appears.
const TASK_STYLE = {
  email: { icon: 'mail', color: '#3B82F6' },
  calendar: { icon: 'calendar', color: '#10B981' },
  news: { icon: 'newspaper', color: '#EAB308' },
  author: { icon: 'pen', color: '#A855F7' },
  literature: { icon: 'book', color: '#06B6D4' },
  format: { icon: 'layout', color: '#EC4899' },
  math: { icon: 'sigma', color: '#6366F1' },
  coder: { icon: 'code', color: '#F97316' },
  leader: { icon: 'crown', color: '#F4A62A' },
};

const LOGO = '<svg viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="lg-a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7C6CFF"/>' +
  '<stop offset="1" stop-color="#4F3FE0"/></linearGradient><linearGradient id="lg-b" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FFC857"/>' +
  '<stop offset="1" stop-color="#F49B1F"/></linearGradient></defs><rect width="64" height="64" rx="16" fill="url(#lg-a)"/>' +
  '<g stroke="url(#lg-b)" stroke-width="2.6" stroke-linecap="round">' +
  '<path d="M45.52 27.62L48.90 28.53"/><path d="M41.90 33.90L44.37 36.37"/><path d="M35.62 37.52L36.53 40.90"/><path d="M28.38 37.52L27.47 40.90"/>' +
  '<path d="M22.10 33.90L19.63 36.37"/><path d="M18.48 27.62L15.10 28.53"/><path d="M18.48 20.38L15.10 19.47"/><path d="M22.10 14.10L19.63 11.63"/>' +
  '<path d="M28.38 10.48L27.47 7.10"/><path d="M35.62 10.48L36.53 7.10"/><path d="M41.90 14.10L44.37 11.63"/><path d="M45.52 20.38L48.90 19.47"/></g>' +
  '<g transform="translate(32 26) scale(1.75)">' +
  '<ellipse cx="-4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(-30 -4 -2.6)" fill="#FFE9A8"/>' +
  '<ellipse cx="4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(30 4 -2.6)" fill="#FFE9A8"/>' +
  '<path d="M-1 -3.5Q-1.8-6.6-3.4-7.8M1-3.5Q1.8-6.6 3.4-7.8" fill="none" stroke="#2A1D57" stroke-width=".8" stroke-linecap="round"/>' +
  '<circle r="3.8" fill="url(#lg-b)"/><path d="M-2.9 1.2h5.8M-1.8 2.9h3.6" stroke="#2A1D57" stroke-width="1.3" stroke-linecap="round"/>' +
  '<circle cx="-1.3" cy="-1.1" r=".85" fill="#2A1D57"/><circle cx="1.3" cy="-1.1" r=".85" fill="#2A1D57"/></g>' +
  '<g transform="translate(13 44.5) scale(1.25)">' +
  '<ellipse cx="-4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(-30 -4 -2.6)" fill="#fff" fill-opacity=".92"/>' +
  '<ellipse cx="4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(30 4 -2.6)" fill="#fff" fill-opacity=".92"/>' +
  '<path d="M-1 -3.5Q-1.7-6-3.1-7M1-3.5Q1.7-6 3.1-7" fill="none" stroke="#2A1D57" stroke-width=".8" stroke-linecap="round"/>' +
  '<circle r="3.8" fill="#FFE07A"/><path d="M-2.9 1.2h5.8M-1.8 2.9h3.6" stroke="#2A1D57" stroke-width="1.3" stroke-linecap="round"/>' +
  '<circle cx="-1.3" cy="-1.1" r=".85" fill="#2A1D57"/><circle cx="1.3" cy="-1.1" r=".85" fill="#2A1D57"/></g>' +
  '<g transform="translate(32 54.6) scale(1.45)">' +
  '<ellipse cx="-4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(-30 -4 -2.6)" fill="#fff" fill-opacity=".92"/>' +
  '<ellipse cx="4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(30 4 -2.6)" fill="#fff" fill-opacity=".92"/>' +
  '<path d="M-1 -3.5Q-1.7-6-3.1-7M1-3.5Q1.7-6 3.1-7" fill="none" stroke="#2A1D57" stroke-width=".8" stroke-linecap="round"/>' +
  '<circle r="3.8" fill="#FFE07A"/><path d="M-2.9 1.2h5.8M-1.8 2.9h3.6" stroke="#2A1D57" stroke-width="1.3" stroke-linecap="round"/>' +
  '<circle cx="-1.3" cy="-1.1" r=".85" fill="#2A1D57"/><circle cx="1.3" cy="-1.1" r=".85" fill="#2A1D57"/></g>' +
  '<g transform="translate(51 44.5) scale(1.25)">' +
  '<ellipse cx="-4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(-30 -4 -2.6)" fill="#fff" fill-opacity=".92"/>' +
  '<ellipse cx="4" cy="-2.6" rx="3.2" ry="2.2" transform="rotate(30 4 -2.6)" fill="#fff" fill-opacity=".92"/>' +
  '<path d="M-1 -3.5Q-1.7-6-3.1-7M1-3.5Q1.7-6 3.1-7" fill="none" stroke="#2A1D57" stroke-width=".8" stroke-linecap="round"/>' +
  '<circle r="3.8" fill="#FFE07A"/><path d="M-2.9 1.2h5.8M-1.8 2.9h3.6" stroke="#2A1D57" stroke-width="1.3" stroke-linecap="round"/>' +
  '<circle cx="-1.3" cy="-1.1" r=".85" fill="#2A1D57"/><circle cx="1.3" cy="-1.1" r=".85" fill="#2A1D57"/></g></svg>';
