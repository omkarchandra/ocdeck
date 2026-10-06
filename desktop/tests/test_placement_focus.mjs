import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const SOURCE_PATH = process.env.PLACEMENT_EXTENSION_SOURCE
    ?? new URL('../gnome-extensions/ocdeck-placement@local/extension.js', import.meta.url)
const source = readFileSync(SOURCE_PATH, 'utf8')

// The process tree observed on the owner's desktop: every terminal OC Deck
// opens is a descendant of OC Deck's own Ptyxis window.
const SYSTEMD_USER = 4424
const DECK_WINDOW = 1336965     // ptyxis --title=OC Deck
const DECK_AGENT = 1336982      // ptyxis-agent
const DECK_PYTHON = 1337028     // .venv/bin/python .local/bin/ocdeck
const SESSION_WINDOW = 1555989  // ptyxis --title "OpenCode · project_a"
const SESSION_CLIENT = 1556077  // tmux attach-session inside that window
const PARENTS = {
    [SESSION_CLIENT]: SESSION_WINDOW,
    [SESSION_WINDOW]: DECK_PYTHON,
    [DECK_PYTHON]: DECK_AGENT,
    [DECK_AGENT]: DECK_WINDOW,
    [DECK_WINDOW]: SYSTEMD_USER,
    [SYSTEMD_USER]: 1,
}

function fakeWindow(pid, {minimized = false} = {}) {
    return {
        pid,
        minimized,
        get_pid: () => pid,
        unminimize() { this.minimized = false },
    }
}

// `windows` is in most-recently-used order, as Meta's tab list returns it.
function fixture(windows) {
    const activated = []
    const context = {
        Extension: class {},
        Gio: {},
        GLib: {build_filenamev: (parts) => parts.join('/'), get_user_state_dir: () => '/state', get_user_config_dir: () => '/config',
               file_get_contents: () => { throw new Error('no slot file') }},
        Meta: {TabList: {NORMAL_ALL: 0}},
        global: {display: {get_tab_list: () => windows}},
        Main: {activateWindow: (window) => activated.push(window.pid)},
    }
    vm.runInNewContext(
        source.replace(/^import .*;\n/gm, '').replace('export default class', 'class') +
        '\nglobalThis.PlacementExtension = OCDeckPlacementExtension',
        context,
    )
    const extension = new context.PlacementExtension()
    // Only /proc access is replaced; the real ancestry walk is exercised.
    extension._parentPid = (pid) => PARENTS[pid] ?? 0
    return {extension, activated}
}

test('reopening a terminal focuses it, not the OC Deck window it descends from', () => {
    // Regression: the deck is most recently used (the key was pressed there) and
    // is an ancestor of the terminal, so "any ancestor" matched the deck, reported
    // success, and the terminal never came forward (only the first open worked).
    const deck = fakeWindow(DECK_WINDOW)
    const session = fakeWindow(SESSION_WINDOW)
    const f = fixture([deck, session])
    assert.equal(f.extension.FocusPid(SESSION_WINDOW), true)
    assert.deepEqual(f.activated, [SESSION_WINDOW])
})

test('a process inside a terminal focuses that terminal (nearest window owner)', () => {
    const f = fixture([fakeWindow(DECK_WINDOW), fakeWindow(SESSION_WINDOW)])
    assert.equal(f.extension.FocusPid(SESSION_CLIENT), true)
    assert.deepEqual(f.activated, [SESSION_WINDOW])
})

test('the Super+O caller passes the deck python pid and still reaches the deck window', () => {
    const f = fixture([fakeWindow(SESSION_WINDOW), fakeWindow(DECK_WINDOW)])
    assert.equal(f.extension.FocusPid(DECK_PYTHON), true)
    assert.deepEqual(f.activated, [DECK_WINDOW])
})

test('a minimized terminal is restored and focused', () => {
    const session = fakeWindow(SESSION_WINDOW, {minimized: true})
    const f = fixture([fakeWindow(DECK_WINDOW), session])
    assert.equal(f.extension.FocusPid(SESSION_WINDOW), true)
    assert.equal(session.minimized, false)
    assert.deepEqual(f.activated, [SESSION_WINDOW])
})

test('no window anywhere in the ancestry is reported honestly', () => {
    const f = fixture([fakeWindow(99)])
    assert.equal(f.extension.FocusPid(SESSION_WINDOW), false)
    assert.deepEqual(f.activated, [])
})
