import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// The owner's pinned viewer slot: new OC Deck session windows open at exactly
// the pinned window's position and size, and the slot survives a login.
const source = readFileSync(new URL('../gnome-extensions/ocdeck-placement@local/extension.js', import.meta.url), 'utf8')
const MY_WINDOW = {x: 1040, y: 32, width: 640, height: 1400}

function win(title, rect, pid = 1) {
    return {
        title, rect: {...rect}, pid, moved: null, minimized: false,
        maximized_horizontally: false, maximized_vertically: false,
        get_title() { return this.title },
        get_wm_class: () => 'org.gnome.Ptyxis',
        get_pid() { return this.pid },
        get_frame_rect() { return this.rect },
        get_compositor_private: () => ({}),
        get_work_area_current_monitor: () => ({x: 0, y: 32, width: 2560, height: 1400}),
        get_user_time: () => 1,
        unmaximize() {},
        move_resize_frame(_user, x, y, width, height) { this.moved = {x, y, width, height} },
    }
}

function fixture({slotFile = null, windows = []} = {}) {
    const saved = []
    const context = {
        Extension: class {},
        TextDecoder,
        Gio: {DBusExportedObject: {wrapJSObject: () => ({export() {}})}, DBus: {session: {}},
              bus_own_name_on_connection: () => 1,
              File: {new_for_path: () => ({load_contents: () => { throw new Error('no proc') }})},
              BusNameOwnerFlags: {NONE: 0}},
        GLib: {
            build_filenamev: (parts) => parts.join('/'),
            get_user_state_dir: () => '/state', get_user_config_dir: () => '/config',
            path_get_dirname: (path) => path.split('/').slice(0, -1).join('/'),
            mkdir_with_parents: () => 0,
            file_get_contents: () => {
                if (slotFile === null) throw new Error('missing')
                return [true, new TextEncoder().encode(slotFile)]
            },
            file_set_contents: (path, text) => saved.push([path, JSON.parse(text)]),
            timeout_add: () => 0, PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false,
        },
        Meta: {TabList: {NORMAL_ALL: 0}, MaximizeFlags: {HORIZONTAL: 1, VERTICAL: 2}},
        global: {display: {get_tab_list: () => windows, connect: () => 0, disconnect() {}, focus_window: null},
                 backend: {get_monitor_manager: () => ({get_monitor_for_connector: () => -1})}},
        Main: {activateWindow() {}, layoutManager: {connect: () => 0, disconnect() {}}},
    }
    vm.runInNewContext(
        source.replace(/^import .*;\n/gm, '').replace('export default class', 'class') +
        '\nglobalThis.PlacementExtension = OCDeckPlacementExtension', context)
    const extension = new context.PlacementExtension()
    extension.enable()
    return {extension, saved, context}
}

test('Shift+P pins the exact position and size of that window and saves it', () => {
    const mine = win('OpenCode · ocdeck_expansion', MY_WINDOW)
    const f = fixture({windows: [mine]})
    assert.equal(f.extension.SetAgentReference('ocdeck_expansion'), true)
    assert.deepEqual(f.saved, [['/state/ocdeck/viewer-slot.json', MY_WINDOW]])
    const opened = win('OpenCode · project_b', {x: 0, y: 0, width: 800, height: 600}, 2)
    assert.equal(f.extension._placeWindow(opened), true)
    assert.deepEqual(opened.moved, MY_WINDOW)
})

test('the pinned slot applies after a login, before any window is open', () => {
    const f = fixture({slotFile: JSON.stringify(MY_WINDOW)})
    const opened = win('OpenCode · project_b', {x: 5, y: 5, width: 300, height: 300}, 2)
    f.extension._placeWindow(opened)
    assert.deepEqual(opened.moved, MY_WINDOW)
})

test('moving the pinned window moves the slot with it', () => {
    const mine = win('OpenCode · ocdeck_expansion', MY_WINDOW)
    const f = fixture({windows: [mine]})
    f.extension.SetAgentReference('ocdeck_expansion')
    mine.rect = {x: 100, y: 40, width: 900, height: 1000}
    const opened = win('OpenCode · project_b', {x: 0, y: 0, width: 800, height: 600}, 2)
    f.extension._placeWindow(opened)
    assert.deepEqual(opened.moved, mine.rect)
    assert.deepEqual(f.saved.at(-1)[1], mine.rect)
})

test('the pinned window itself is never moved onto itself', () => {
    const mine = win('OpenCode · ocdeck_expansion', MY_WINDOW)
    const f = fixture({windows: [mine]})
    f.extension.SetAgentReference('ocdeck_expansion')
    assert.equal(f.extension._placeWindow(mine), false)
    assert.equal(mine.moved, null)
})

test('a corrupt or tiny slot file is ignored (old quarter-tile placement)', () => {
    for (const slotFile of ['not json', JSON.stringify({x: 1, y: 2, width: 10, height: 10}),
                            JSON.stringify({x: 'a', y: 2, width: 800, height: 800})]) {
        const f = fixture({slotFile})
        assert.equal(f.extension._slot, null)
    }
})

test('with no reference and no pin, windows go to the largest screen', () => {
    const f = fixture()
    const laptop = {x: 0, y: 32, width: 1920, height: 1168}
    const lg = {x: 1920, y: 32, width: 3440, height: 1408}
    f.extension._referenceArea = null
    f.extension._referenceRect = null
    const opened = win('OpenCode · project_b', {x: 10, y: 40, width: 700, height: 500}, 2)
    opened.get_workspace = () => ({get_work_area_for_monitor: (index) => [laptop, lg][index]})
    f.context.global.display.get_n_monitors = () => 2
    assert.equal(f.extension._placeWindow(opened), true)
    assert.deepEqual(opened.moved, {x: 1920, y: 32, width: 860, height: 1408})
})

test('the OC Deck window opens at its pinned spot, and PinDeck saves that spot', () => {
    const deckSpot = {x: 1920, y: 32, width: 860, height: 1408}
    const f = fixture({slotFile: JSON.stringify(deckSpot)})  // the file read serves both slots here
    const deck = win('OC Deck', {x: 0, y: 0, width: 2560, height: 1400}, 9)
    deck.maximized_horizontally = true
    assert.equal(f.extension._placeDeck(deck), true)
    assert.deepEqual(deck.moved, deckSpot)

    const g = fixture({windows: [win('OC Deck', deckSpot, 9)]})
    assert.equal(g.extension.PinDeck(), true)
    assert.deepEqual(g.saved.at(-1), ['/state/ocdeck/deck-slot.json', deckSpot])
})

test('without a pinned deck spot the deck keeps its maximized launch', () => {
    const f = fixture()
    const deck = win('OC Deck', {x: 0, y: 0, width: 2560, height: 1400}, 9)
    assert.equal(f.extension._placeDeck(deck), false)
    assert.equal(deck.moved, null)
    assert.equal(fixture({windows: []}).extension.PinDeck(), false)
})
