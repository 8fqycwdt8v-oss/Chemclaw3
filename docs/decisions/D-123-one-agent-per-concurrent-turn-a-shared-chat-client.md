# D-123 — One agent per concurrent turn: a shared chat client corrupts streamed tool calls

**Context.** The live 50-user run (Haiku, 4 workers, 50 signed identities) admitted every turn —
150/150, no shed, no conflict, no transport error — and then lost **30 of them (20 %)** to an
Anthropic 400:

```
messages.1.content.3.tool_use.name: String should have at least 1 character
```

**The cause, isolated by elimination.** Eight live attempts per configuration:

| Variant | Setup | Result |
|---|---|---|
| A | bare `agent_framework`, 3 tools, sequential | 0/8 fail |
| B | + the 6 MCP connectors, sequential | 0/8 fail |
| C | full `build_agent()` + connectors, sequential | 0/8 fail |
| **D** | full `build_agent()`, 8 turns **concurrent, one shared agent** | **8/8 fail** |
| **E** | identical, but **one agent per turn** | 0/8 fail |
| **F** | **per-turn agents, one shared *client*** | **8/8 fail** |

E and F differ only in whether the *client* is shared, which is what names the client rather than
the agent. `agent_framework_anthropic/_chat_client.py` keeps the tool call it is currently parsing
on the instance:

```python
case "tool_use":
    self._last_call_id_name = (content_block.id, content_block.name)
...
case "input_json_delta":
    call_id = self._last_call_id_name[0] if self._last_call_id_name else ""
    contents.append(Content.from_function_call(call_id=call_id, name="", ...))
```

An argument delta carries `name=""` **by design** and recovers its identity from that attribute. Two
turns streaming through one client interleave: B's `tool_use` overwrites the attribute between A's
`tool_use` and A's deltas, A's arguments are filed under B's call id, and A's assistant message goes
out carrying a `tool_use` block with an empty name. It needs two or more tool calls in one message
to show, which is why every failure named `content.2` or `content.3`.

**Decision.** `agents/agent_pool.py::AgentPool` leases one agent — and with it one chat client — to
one turn at a time, sized to `service_max_concurrent_turns`. The front door leases around the
streamed run; everything that does not stream (session creation, `/readyz`) keeps the cached
per-profile agent, because only a stream can interleave.

A pool rather than per-turn construction: building is cheap enough (~90 ms agent, ~95 ms client) but
a fresh client is a fresh `AsyncAnthropic`, hence a fresh connection pool and TLS handshake on every
turn — reintroducing exactly the per-call handshake churn D-119 removed from Postgres. A lease keeps
connections warm across turns while guaranteeing no two *concurrent* turns share one.

Sized to the admission cap so the pool is never the queue: the semaphore already bounds concurrency
at the same number, so a lease does not block in normal operation.

**Result, measured on the same live run:**

| | before | after |
|---|---|---|
| answers / errors | 120 / **30** | **150 / 0** |
| empty `tool_use` names in the log | 30 | **0** |
| p50 | 19.8 s | 16.9 s |
| throughput | 1.76/s | 1.99/s |
| tool calls | 151 | 208 |

Latency and throughput improved as well, which follows: a turn that died at its first tool call was
finishing early, and 208 tool calls against 151 is the count of tools that now run to completion.

**Why no test caught it.** Every stub run reported a clean 150/150 because the stub emits exactly
one tool call per response, and a single `tool_use` block has nothing to interleave with. Only a
real model making *parallel* calls under *concurrency* reaches it — the intersection of two
conditions, neither of which a unit test has.

`tests/test_agent_pool.py` asserts the property that makes the corruption impossible — no agent held
by two turns at once — rather than the corruption itself, which is upstream code.

**This is a workaround, written to be deleted.** The real fix is for the parser to hold that state
per stream. `DEFERRED.md` records the trigger: when it does, the pool collapses back to one shared
agent per profile and `agents/agent_pool.py` goes away.
