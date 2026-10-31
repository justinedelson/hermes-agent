const assert = require('node:assert/strict');
const test = require('node:test');

// Execute the shipped IIFE through the host SDK, without inspecting source text.
async function mount(search) {
  const states = [], effects = [], requests = [];
  let cursor = 0, first = true, page;
  const hooks = {
    useState(initial) {
      const key = cursor++;
      if (first) states[key] = typeof initial === 'function' ? initial() : initial;
      return [states[key], value => { states[key] = typeof value === 'function' ? value(states[key]) : value; }];
    },
    useRef(initial) { return this.useState({ current: initial })[0]; },
    useEffect(callback) { if (first) effects.push(callback); },
    useMemo(callback) { return callback(); },
    useCallback(callback) { return callback; },
  };
  hooks.useRef = initial => hooks.useState({ current: initial })[0];
  global.window = {
    location: { search },
    localStorage: { getItem: () => 'stored-board', setItem() {} },
    addEventListener() {}, removeEventListener() {},
    __HERMES_PLUGINS__: { register(name, component) { page = component; } },
    __HERMES_PLUGIN_SDK__: {
      React: { ...hooks, Component: class {}, createElement: (type, props, ...children) => ({ type, props, children }) },
      hooks, components: {}, utils: { cn: (...values) => values.join(' '), timeAgo: () => '' },
      fetchJSON(url) {
        requests.push(url);
        if (url.includes('/boards')) return Promise.resolve({ boards: [{ slug: 'linked-board' }, { slug: 'stored-board' }], current: 'default' });
        if (url.includes('/board')) return Promise.resolve({ columns: [], tenants: [], assignees: [] });
        return Promise.resolve({});
      },
    },
  };
  delete require.cache[require.resolve('./dist/index.js')];
  require('./dist/index.js');
  page();
  first = false;
  for (const effect of effects) effect();
  await new Promise(resolve => setImmediate(resolve));
  cursor = 0;
  return { tree: page(), requests };
}

function findDrawer(tree) {
  if (!tree || typeof tree !== 'object') return null;
  if (tree.props && tree.props.taskId) return tree;
  for (const child of tree.children || []) {
    for (const node of [child].flat(Infinity)) {
      const found = findDrawer(node);
      if (found) return found;
    }
  }
  return null;
}

test('deep link selects the requested board and opens the exact task drawer', async () => {
  const { tree, requests } = await mount('?board=linked-board&task=t_1234abcd');
  const drawer = findDrawer(tree);
  assert.ok(drawer, 'linked task drawer must open');
  assert.equal(drawer.props.taskId, 't_1234abcd');
  assert.equal(drawer.props.boardSlug, 'linked-board');
  assert.ok(requests.some(url => url.includes('/board?board=linked-board')));
});

test('no link retains the selected board and leaves the drawer closed', async () => {
  const { tree, requests } = await mount('');
  assert.equal(findDrawer(tree), null);
  assert.ok(requests.some(url => url.includes('/board?board=stored-board')));
});
