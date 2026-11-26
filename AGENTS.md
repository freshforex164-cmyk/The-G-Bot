# Runtime architecture

- Run the scanner/Telegram poller and auto-trading engine as separate supervised processes on one VPS; separate event loops isolate their lifecycles without changing hosting.
- Keep SDK initialization in `broker/sdk_async.py` and pin BinaryOptionsToolsV2 0.2.18; its synchronous constructor blocks the event loop and the bridge mirrors that version's wrapper state.
- Use per-account ownership for balance sockets and generation-aware shared-feed teardown; unrelated users must not serialize or close one another's replacement connections.
- Treat `docs/oracle-vps.md` as the deployment authority; earlier project/audit snapshots describe historical environments, not current deployment requirements.
- Retain explicit Live confirmation, vault encryption, payout/risk checks, and no blind order/write retries; hosting changes must not weaken trading safeguards.
