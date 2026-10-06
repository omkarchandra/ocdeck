import { Plugin } from "@opencode/plugin"

import { createPermissionNotifyV2 } from "./permission-notify-core.js"
import { startEventSubscription } from "./runtime.js"

export default Plugin.define({
  id: "agents-start.permission-notify.v2",
  setup(context) {
    const notifier = createPermissionNotifyV2(context, context.options)
    const subscription = startEventSubscription(
      context.event,
      (event) => notifier.handle(event),
      { onReconnect: () => notifier.recover() },
    )

    return async () => {
      await subscription.dispose()
      await notifier.dispose()
    }
  },
})
