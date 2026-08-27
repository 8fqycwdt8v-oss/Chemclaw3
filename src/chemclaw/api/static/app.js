// Thin Chemclaw chat surface (plan step F2-T2). Renders the typed turn events
// (service/events.py) streamed as SSE. The messages endpoint is POST, so we read the
// response body as a stream and parse `data:` lines ourselves (native EventSource is GET-only).

const transcript = document.getElementById("transcript");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const send = document.getElementById("send");

let sessionId = null;
// The push-back stream (`GET /sessions/{id}/events`), opened once the session exists. A durable
// job finishes long after the turn that launched it ended, so its completion arrives on this
// connection and on no other — without it the dev page showed "⏳ job started" and then nothing,
// forever, which is the one failure mode a manual test of an async job most needs to see.
let events = null;

function add(cls, text) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.textContent = text;
  transcript.appendChild(el);
  transcript.scrollTop = transcript.scrollHeight;
  return el;
}

// Build an Error for a failed response, carrying the server's `detail` when it sent one — a
// non-2xx (401/404/409/429/503) must surface in the transcript, never vanish silently.
async function httpError(res, what) {
  let detail = "";
  try {
    detail = (await res.json()).detail || "";
  } catch (e) {
    // Non-JSON error body — the status alone still tells the user what happened.
  }
  return new Error(`${what} failed (HTTP ${res.status}${detail ? `: ${detail}` : ""})`);
}

async function ensureSession() {
  if (sessionId) return sessionId;
  const res = await fetch("/sessions", { method: "POST" });
  if (!res.ok) throw await httpError(res, "creating a session");
  sessionId = (await res.json()).session_id;
  openEventStream(sessionId);
  return sessionId;
}

// Subscribe to the session's job push-back. `EventSource` rather than the hand-rolled reader
// above because this route is a GET — the parsing only exists for the POSTed turn stream. Each
// pushed event is applied with no answer element: it belongs to no turn's token stream.
function openEventStream(id) {
  if (events) events.close();
  events = new EventSource(`/sessions/${encodeURIComponent(id)}/events`);
  events.addEventListener("job_completed", (e) => applyEvent(JSON.parse(e.data), null));
  events.addEventListener("job_failed", (e) => applyEvent(JSON.parse(e.data), null));
  // A dropped push-back connection must not be silent: the page would look identical to one where
  // no job ever finished. `EventSource` reconnects on its own, so this reports rather than retries.
  events.onerror = () => add("trace", "… job stream interrupted, reconnecting");
}

// Apply one decoded event to the transcript; `answerEl` accumulates streamed tokens.
// Which specialist raised an event, as a trace prefix. Empty for the main agent, which is what
// every event carried before teams existed — so an untagged line reads exactly as it always did.
function agentTag(evt) {
  return evt.agent ? `[${evt.agent}] ` : "";
}

function applyEvent(evt, answerEl) {
  switch (evt.type) {
    case "queued":
      // Only a turn that had to wait emits this, and it is the first thing it emits (D-166).
      // Before it existed the same wait was a blank page followed by a 503.
      add("trace", "⏸ waiting for a free turn slot…");
      return answerEl;
    case "plan":
      add("trace", "Plan:\n- " + (evt.todos || []).join("\n- "));
      return answerEl;
    case "tool_call":
      add("trace", `${agentTag(evt)}→ ${evt.tool}(${evt.arguments || ""})`);
      return answerEl;
    case "token":
      // Attributed tokens are another agent's working prose, not this turn's answer. The server
      // already knows: `api/runner` concatenates only unattributed tokens into `AnswerEvent.text`,
      // so appending them here made the bubble a chemist reads diverge permanently from the
      // durable transcript — permanently, because `case "answer"` below only fills an *empty*
      // element and so never overwrites the contaminated one. Every other agent-bearing event
      // routes through `agentTag`; this is the one where the attribution decides what the answer
      // *is*, which is why it was the one worth getting wrong.
      if (evt.agent) {
        add("trace", `${agentTag(evt)}${evt.text}`);
        transcript.scrollTop = transcript.scrollHeight;
        return answerEl;
      }
      if (!answerEl) answerEl = add("assistant", "");
      answerEl.textContent += evt.text;
      transcript.scrollTop = transcript.scrollHeight;
      return answerEl;
    case "tool_result":
      // The value itself, not the model's paraphrase of it (D-159). Paired with the `tool_call`
      // line above, the trace now shows a call's whole lifecycle — issued, then what came back.
      add("trace", `${agentTag(evt)}← ${evt.tool} → ${evt.preview || ""}`);
      return answerEl;
    case "job_started":
      add("trace", `⏳ ${evt.kind || "job"} started (${evt.job_id})`);
      return answerEl;
    case "job_completed":
      // Arrives on the push-back stream, outside any turn — see `openEventStream`.
      add("trace", `✓ job ${evt.job_id} completed ${JSON.stringify(evt.summary || {})}`);
      return answerEl;
    case "job_failed":
      // A failure lane, not the trace: this retracts a "job started" the reader has already been
      // shown, so it must not scroll past as one more grey line.
      add("warn", `\u2717 job ${evt.job_id} failed \u2014 ${evt.reason || "no reason reported"}`);
      return answerEl;
    case "capability_degraded":
      // Its own lane, not the trace: this qualifies the answer that follows, and an answer
      // assembled without the ELN is indistinguishable from one assembled with it unless the
      // page says so where a reader will not scroll past it.
      add("warn", `⚠ answering with fewer tools — unreachable: ${(evt.connectors || []).join(", ")}`);
      return answerEl;
    case "question":
      add("trace", `❓ ${evt.question}` + ((evt.options || []).length ? `\n   options: ${evt.options.join(" | ")}` : ""));
      return answerEl;
    case "note_proposed":
      add("trace", `📝 proposed ${evt.note_id} for review — ${evt.reference}`);
      return answerEl;
    case "tool_failed":
      // In the trace, not the error lane: the step failed, the turn did not. Without this the
      // transcript showed a silent gap wherever a tool raised.
      add("trace", `${agentTag(evt)}✗ ${evt.tool} failed — ${evt.message}`);
      return answerEl;
    case "evidence_source":
      // Zero is the point of this line, not noise to filter: a source that found nothing is the
      // failure D-2026-08-01 went looking for, and it is invisible in the merged evidence list.
      add("trace", `⌕ ${evt.source}: ${evt.chunks} chunk(s)`);
      return answerEl;
    case "handoff":
      // Its own line rather than a prefix on what follows: a reader needs to see *where* control
      // went, not only that the next tool call came from somewhere else.
      add("trace", evt.to ? `⇢ handed to ${evt.to}${evt.reason ? ` — ${evt.reason}` : ""}` : "⇠ back to the main agent");
      return answerEl;
    case "answer":
      if (!answerEl) add("assistant", evt.text);
      return answerEl;
    case "error":
      add("error", evt.message);
      return answerEl;
    default:
      return answerEl;
  }
}

async function sendMessage(message) {
  const id = await ensureSession();
  const res = await fetch(`/sessions/${id}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw await httpError(res, "sending the message");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answerEl = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop(); // keep the trailing partial frame
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      answerEl = applyEvent(JSON.parse(line.slice(5).trim()), answerEl);
    }
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  add("user", message);
  input.value = "";
  send.disabled = true;
  try {
    await sendMessage(message);
  } catch (err) {
    add("error", String(err));
  } finally {
    send.disabled = false;
    input.focus();
  }
});
