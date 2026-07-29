# D-047 — F4-T6: the two non-Entra transport bridges carry identity as a claim

**Context.** §7.2 names two transports that are not Entra relying parties — Temporal and HPC/Nextflow.
The rule is that identity rides *inside* the workflow payload (`requested_by`, D-044), never the
transport; the transports themselves are secured and, for HPC, every identity mapping is logged.

**Decision.**
- **Temporal transport auth.** `chemclaw/temporal_client.py` now builds its `Client.connect` kwargs
  in a pure `connect_options()`: mTLS (`temporal_tls_cert`/`_key`/`_ca` → `TLSConfig`, PEM paths read
  to bytes) when set, and/or a Temporal Cloud `temporal_api_key`. Extracting the options makes
  transport security assertable offline (constructed-args, no broker); dev stays plaintext when none
  are set. Identity is *not* put on the transport — it is already in the payload.
- **HPC identity bridge.** `agents/identity/hpc_bridge.py::map_to_hpc_identity(oid)` returns the one
  shared `hpc_bridge_identity` a user's job runs under (HPC is not an Entra RP) and **logs every
  oid→HPC-identity mapping** at INFO — the sole audit link from a cluster run back to the real user.
  No `hpc_bridge_log_dsn` key was added: the audit trail already *is* structured logging, so a DSN
  with no consumer would be a dead config knob.

**Consequence.** Both bridges are ready and proven offline; the live broker/cluster wiring is the only
gated edge. Together with D-043/D-044/D-045/D-046 this closes F4's offline-verifiable scope: front-door
OIDC, one authorization gate, the reject-if-absent core rule, federation, OBO, and both bridges — the
generic LLM key remaining the one documented exception.
