import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../gnome-extensions/ocdeck-notification-focus@local/extension.js', import.meta.url), 'utf8')

function fixture() {
    let focused = null
    const context = {
        Extension: class {},
        Shell: {ActionMode: {NORMAL: 1, OVERVIEW: 2}},
        global: {stage: {get_key_focus: () => focused}},
        Main: {sessionMode: {isLocked: false}, messageTray: {
            _banner: null,
            _expandActiveNotification() { focused = this._banner._buttonBox.get_children()[0] ?? this._banner },
        }},
    }
    vm.runInNewContext(source.replace(/^import .*;\n/gm, '').replace('export default class', 'class') +
        '\nglobalThis.FocusExtension = OCDeckNotificationFocus', context)
    const extension = new context.FocusExtension()
    function show(labels) {
        const actor = label => ({label, visible: true, reactive: true, grab_key_focus() { focused = this }})
        const buttons = labels.map(actor)
        context.Main.messageTray._banner = Object.assign(actor('body'), {
            _buttonBox: {get_children: () => buttons},
        })
    }
    return {extension, context, show, focused: () => focused?.label, unfocus: () => { focused = null }}
}

test('each native invocation advances once, including rapid repeated N', () => {
    const f = fixture()
    f.show(['Reject', 'Allow once', 'Always allow'])
    for (const expected of ['Always allow', 'Allow once', 'Reject', 'body', 'Always allow']) {
        assert.equal(f.extension.Focus(), true)
        assert.equal(f.focused(), expected)
    }
})

test('new banners and a return from application focus restart at the first action', () => {
    const f = fixture()
    f.show(['Always allow', 'Allow once', 'Reject'])
    f.extension.Focus()
    f.extension.Focus()
    f.unfocus()
    f.extension.Focus()
    assert.equal(f.focused(), 'Always allow')
    f.show(['Open question'])
    f.extension.Focus()
    assert.equal(f.focused(), 'Open question')
})

test('buttonless completion targets its activatable body', () => {
    const f = fixture()
    f.show([])
    assert.equal(f.extension.Focus(), true)
    assert.equal(f.focused(), 'body')
    assert.equal(f.extension.Focus(), true)
    assert.equal(f.focused(), 'body')
})

test('a locked desktop or absent banner is not reported as focused', () => {
    const f = fixture()
    assert.equal(f.extension.Focus(), false)
    f.show(['Allow once', 'Reject'])
    f.context.Main.sessionMode.isLocked = true
    assert.equal(f.extension.Focus(), false)
    assert.equal(f.focused(), undefined)
})
