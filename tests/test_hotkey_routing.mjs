import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import vm from "node:vm"

const source = readFileSync(new URL("../desktop/gnome-extensions/ocdeck-switch@local/extension.js", import.meta.url), "utf8")
const command = 'exec "$HOME/.local/bin/ocdeck-entrypoint"'

function harness(windows) {
  const activated = [], spawned = []
  const context = {
    Extension: class {},
    global: { display: { get_tab_list: () => windows, focus_window: null } },
    Meta: { TabList: { NORMAL_ALL: 0 } },
    Main: { activateWindow: window => activated.push(window) },
    GLib: { get_monotonic_time: () => 1000, get_home_dir: () => "/home/test" },
    Gio: { Subprocess: { new: argv => spawned.push(argv) }, SubprocessFlags: { NONE: 0 } },
  }
  vm.runInNewContext(source.replace(/^import .*;\n/gm, "").replace("export default class", "class") +
    "\nglobalThis.Switch = OCDeckSwitchExtension", context)
  const extension = new context.Switch()
  extension._cycleIndex = 0
  extension._windowProcessArguments = window => window.args
  extension._pulseWindow = () => {}
  return { extension, context, activated, spawned }
}

function window(title, args, minimized = false) {
  return { args, minimized, get_wm_class: () => "org.gnome.Ptyxis", get_title: () => title,
    get_pid: () => 123, get_stable_sequence: () => 1,
    unminimize() { this.minimized = false } }
}

test("dynamic dashboard title still focuses and unminimizes its launcher", () => {
  const deck = window("user@laptop: ~ — python /home/user/.local/bin/ocdeck", ["ptyxis", "--standalone", "--title", "OC Deck", "--", "bash", "-lc", command], true)
  const state = harness([deck])
  assert.equal(state.extension.LaunchOrCycle(), true)
  assert.deepEqual(state.activated, [deck])
  assert.equal(deck.minimized, false)
  assert.equal(state.spawned.length, 0)
  assert.equal(JSON.parse(state.extension.Inspect())[0].pid, 123)
})

test("agent terminal is not mistaken for the dashboard and repeated launch is debounced", () => {
  const agent = window("OpenCode · OC Deck", ["ptyxis", "--standalone", "tmux", "attach-session", "-t", "oc-ses_test"])
  const state = harness([agent])
  state.extension.LaunchOrCycle()
  state.extension.LaunchOrCycle()
  assert.equal(state.spawned.length, 1)
  assert.equal(state.activated.length, 0)
})
