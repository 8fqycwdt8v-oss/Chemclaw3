// Thin Chemclaw chat surface (plan step F2-T2). Renders the typed turn events
// (service/events.py) streamed as SSE. The messages endpoint is POST, so we read the
// response body as a stream and parse `data:` lines ourselves (native EventSource is GET-only).

const transcript = document.getElementById("transcript");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const send = document.getElementById("send");

let sessionId = null;

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
  return sessionId;
}

// Apply one decoded event to the transcript; `answerEl` accumulates streamed tokens.
function applyEvent(evt, answerEl) {
  switch (evt.type) {
    case "plan":
      add("trace", "Plan:\n- " + (evt.todos || []).join("\n- "));
      return answerEl;
    case "tool_call":
      add("trace", `→ ${evt.tool}(${evt.arguments || ""})`);
      return answerEl;
    case "token":
      if (!answerEl) answerEl = add("assistant", "");
      answerEl.textContent += evt.text;
      transcript.scrollTop = transcript.scrollHeight;
      return answerEl;
    case "job_started":
      add("trace", `job started (${evt.job_id})`);
      return answerEl;
    case "approval_request":
      add("trace", `⏸ approval requested: ${evt.prompt}`);
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
