import { createApprovalForward } from "./approval-forward-core.js"

const INSTANCE_KEY = Symbol.for("agents-start.approval-forward.instance")

export const ApprovalForward = (input) => {
  const instances = globalThis[INSTANCE_KEY] || new Map()
  globalThis[INSTANCE_KEY] = instances
  const key = input.directory || ""
  if (instances.has(key)) return {}
  const core = createApprovalForward(input)
  const hooks = {
    ...core,
    dispose: async () => {
      await core.dispose?.()
      if (instances.get(key) === hooks) instances.delete(key)
    },
  }
  instances.set(key, hooks)
  return hooks
}
