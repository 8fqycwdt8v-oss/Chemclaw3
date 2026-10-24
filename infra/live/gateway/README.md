# infra/live/gateway

The live lane's caching gateway: LiteLLM on loopback, in front of Anthropic's native Messages API,
with prompt-cache breakpoints at the system message and the last message (`litellm.yaml`). Why it
exists and what it measured: `docs/decisions/D-2026-10-24-the-live-lane-caches-through-a-local-gateway.md`.

```
export ANTHROPIC_API_KEY=...            # or the variable literally named API-KEY
bash infra/live/gateway/up.sh up        # starts it on 127.0.0.1:4000
eval "$(bash infra/live/gateway/up.sh env)"
make live-up                            # the lane now dials the gateway
bash infra/live/gateway/up.sh down
```

The vendor key stays in the gateway's environment. The lane gets a local token (`.live/gateway/gateway.token`,
mode 0600) as `CHEMCLAW_LLM_API_KEY`. `CHEMCLAW_LIVE_GATEWAY_PORT` and `CHEMCLAW_LIVE_GATEWAY_MODEL`
override the port and the model id `env` prints.
