# Plugins and helpers for OpenCode

Optional pieces that make OC Deck faster and more useful with OpenCode. None is
needed to run the deck.

| File | For | What it does |
|---|---|---|
| `permission-notify.js`, `permission-notify-entry.js` | OpenCode V1 | Desktop notification when an agent asks for permission or a question; clicking it focuses the session (needs the [GNOME integration](../desktop/README.md)). |
| `v2/permission-notify*.js`, `v2/runtime.js` | OpenCode V2 | The same for V2, filtered to the plugin instance's exact directory and workspace. |
| `v2/session-notify-*.js(mjs)` | OpenCode V2 | Watcher that announces finished turns; started by `ocdeck-permission-watcher`. |
| `v2/read-api-bridge.mjs`, `v2/read-api-core.js` | OpenCode V2 | A persistent read-only bridge OC Deck uses instead of starting `opencode2 api` for every poll. It answers only an allow-list of read operations. Set `OCDECK_READ_BRIDGE` to use a copy elsewhere. |
| `org.local.OCDeckSwitch.desktop` | GNOME | Desktop entry so notification actions route to OC Deck. |

## Installing the V2 plugins

The V2 plugins target `@opencode/plugin` **2.0.14** and are written to run
against exactly that version.

```sh
cd plugins/v2
npm ci --omit=dev --ignore-scripts
```

Then list the package *directories* in your OpenCode V2 configuration (the
beta skips single-file plugin paths):

```jsonc
{ "plugins": ["/path/to/ocdeck/plugins/v2/permission-notify"] }
```

If your OpenCode configuration is root-owned or release-managed, add the
plugin there; OC Deck never writes protected configuration.

## Known limits (V2 beta)

The plugin context can list permissions per session but has no location-wide
listing and no form-list API. Each plugin instance therefore remembers the
sessions it has seen and re-lists them each time the event stream reconnects.
A permission created for a session that never emitted an event while the stream
was down stays unseen until that session emits one, and forms are live-event
only. The plugins never invent state they cannot read.

## Tests

```sh
node --test plugins/test_permission_notify.mjs
```
