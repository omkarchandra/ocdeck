import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// With ~/.config/ocdeck/placement.json naming a monitor, the agents' Chrome
// stays on it (here the laptop panel, eDP-1) and never moves to the other one.
const source = readFileSync(new URL('../gnome-extensions/ocdeck-placement@local/extension.js', import.meta.url), 'utf8')
const LAPTOP = 0
const LG = 1

function win({wmClass = 'google-chrome', monitor = LG, pid = 1, title = 'Chrome'} = {}) {
    return {
        wmClass, monitor, pid, title, moves: [],
        get_wm_class() { return this.wmClass },
        get_title() { return this.title },
        get_pid() { return this.pid },
        get_monitor() { return this.monitor },
        move_to_monitor(index) { this.moves.push(index); this.monitor = index },
    }
}

function fixture({windows = [], laptopIndex = LAPTOP, cmdlines = {}, configured = true} = {}) {
    const signals = {}
    const timers = []
    const context = {
        Extension: class {},
        TextDecoder,
        Gio: {
            DBusExportedObject: {wrapJSObject: () => ({export() {}, unexport() {}})},
            DBus: {session: {}},
            BusNameOwnerFlags: {NONE: 0},
            bus_own_name_on_connection: () => 1,
            bus_unown_name() {},
            File: {new_for_path: (path) => ({load_contents: () => {
                const pid = Number(path.split('/')[2])
                if (!(pid in cmdlines)) throw new Error('no such process')
                return [true, new TextEncoder().encode(cmdlines[pid].join('\0'))]
            }})},
        },
        GLib: {
            build_filenamev: (parts) => parts.join('/'),
            get_user_state_dir: () => '/state',
            get_user_config_dir: () => '/config',
            file_get_contents: (path) => {
                if (configured && path === '/config/ocdeck/placement.json')
                    return [true, new TextEncoder().encode('{"agentBrowserMonitor": "eDP-1"}')]
                throw new Error('missing')
            },
            PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false,
            timeout_add: (_priority, delay, callback) => timers.push({delay, callback}) && timers.length,
            source_remove() {},
        },
        Meta: {TabList: {NORMAL_ALL: 0}},
        global: {
            display: {
                get_tab_list: () => windows,
                connect: (name, handler) => { signals[name] = handler; return Object.keys(signals).length },
                disconnect() {},
            },
            backend: {get_monitor_manager: () => ({
                get_monitor_for_connector: (connector) => connector === 'eDP-1' ? laptopIndex : -1,
            })},
        },
        Main: {
            activateWindow() {},
            layoutManager: {connect: (name, handler) => { signals[`layout:${name}`] = handler; return 99 }, disconnect() {}},
        },
    }
    vm.runInNewContext(
        source.replace(/^import .*;\n/gm, '').replace('export default class', 'class') +
        '\nglobalThis.PlacementExtension = OCDeckPlacementExtension', context)
    const extension = new context.PlacementExtension()
    extension.enable()
    const runTimers = () => { while (timers.length) timers.shift().callback() }
    return {extension, signals, timers, runTimers}
}

test('at startup an agent browser on the LG is moved to the laptop panel; others stay', () => {
    const browser = win({wmClass: 'opencode-agent-browser', monitor: LG})
    const mine = win({wmClass: 'google-chrome', monitor: LG, pid: 2})
    fixture({windows: [browser, mine]})
    assert.deepEqual(browser.moves, [LAPTOP])
    assert.deepEqual(mine.moves, [])  // the owner's own Chrome is never touched
})

test('recognised by its private profile even if the app id differs', () => {
    const browser = win({wmClass: 'google-chrome', monitor: LG, pid: 4242})
    fixture({windows: [browser], cmdlines: {
        4242: ['/opt/google/chrome/chrome', '--user-data-dir=/home/user/.local/share/opencode/agent-browser-profile'],
    }})
    assert.deepEqual(browser.moves, [LAPTOP])
})

test('a newly opened agent browser goes to the laptop, not the deck/viewer logic', () => {
    const browser = win({wmClass: 'opencode-agent-browser', monitor: LG})
    const f = fixture({windows: []})
    f.signals['window-created'](null, browser)
    f.runTimers()
    assert.deepEqual(browser.moves, [LAPTOP])
})

test('if it lands on the LG later (dragged, LG wakes), it goes back, after the signal', () => {
    const browser = win({wmClass: 'opencode-agent-browser', monitor: LAPTOP})
    const f = fixture({windows: [browser]})
    assert.deepEqual(browser.moves, [])  // already home
    browser.monitor = LG
    f.signals['window-entered-monitor'](null, LG, browser)
    assert.deepEqual(browser.moves, [])  // never moved inside the signal itself
    f.runTimers()
    assert.deepEqual(browser.moves, [LAPTOP])
    // Other windows entering a monitor schedule nothing.
    f.signals['window-entered-monitor'](null, LG, win({monitor: LG, pid: 9}))
    assert.equal(f.timers.length, 0)
})

test('after monitors change (LG sleeps or wakes) it is re-checked once settled', () => {
    const browser = win({wmClass: 'opencode-agent-browser', monitor: LAPTOP})
    const f = fixture({windows: [browser]})
    browser.monitor = LG
    f.signals['layout:monitors-changed']()
    assert.equal(f.timers.at(-1).delay, 2000)
    f.runTimers()
    assert.deepEqual(browser.moves, [LAPTOP])
})

test('laptop panel off or missing: the browser is left where it is', () => {
    const browser = win({wmClass: 'opencode-agent-browser', monitor: LG})
    fixture({windows: [browser], laptopIndex: -1})
    assert.deepEqual(browser.moves, [])
})

test('without a configured monitor the agents\' browser is never moved', () => {
    const browser = win({wmClass: 'opencode-agent-browser', monitor: LG})
    const f = fixture({windows: [browser], configured: false})
    f.signals['window-entered-monitor'](null, LG, browser)
    f.runTimers()
    assert.deepEqual(browser.moves, [])
})
