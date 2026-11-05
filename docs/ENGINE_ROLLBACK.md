# Agent Engine Rollback Procedure

Because the agent engine is dynamically cloned at boot from the public `atobar-flow` repository, supply-chain security is critical. We guarantee integrity by enforcing that the engine clone uses an exact 40-character commit SHA (never a floating tag or branch like `main`).

## How the Pin is Enforced

When a client repository triggers a `repository_dispatch` event, the GitHub Action workflow (`flow_dispatcher.yml`) running in the **Control Plane** (`atobar-flow`) computes the `ENGINE_REF` as `github.sha`. This means the agent boots with the exact code present on the `main` branch of the Control Plane at the moment the workflow was triggered.

The cloud-init script strictly enforces that this `ENGINE_REF` is a 40-character SHA-1 hash.

## Bumping the Pinned Reference

To bump the version of the engine used by agents:
1. Make a Pull Request with your code changes to `atobar-flow`.
2. Merge the Pull Request into `main`.
3. All subsequent agent launches will automatically use the new commit SHA on `main`.

## Rolling Back

If a bad engine version is deployed and is affecting in-flight or newly launched agents:

### Fast Path (Rollback `main`)
The fastest way to revert is to revert the offending commit in `atobar-flow`'s `main` branch:
1. Revert the bad commit via the GitHub UI.
2. Once the revert is merged, `main` will point to a new safe SHA.
3. Subsequent dispatches will immediately use the safe code.

### Manual Override for Emergency Debugging
If you need to manually launch an agent with a specific historical SHA:
1. Manually trigger the `flow_dispatcher.yml` workflow via `workflow_dispatch` (if enabled) or dispatch the event with a custom payload containing an `ENGINE_REF` override (this requires modifying `flow_dispatcher.yml` to accept a custom `ENGINE_REF` input in the future, if desired).

## In-Flight Agents
Agents that have already booted have already cloned their specific engine SHA. They will not automatically update or break if the engine repository changes, ensuring deterministic execution (Axiom 6). Preempted Spot instances resuming work will reuse the *exact same* SHA they originally booted with, avoiding mid-run version drift.
