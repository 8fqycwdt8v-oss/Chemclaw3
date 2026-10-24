# deepagents: `import deepagents` eagerly imports three provider SDKs

**File against:** `langchain-ai/deepagents`. **Status:** not filed — fixed upstream: `import deepagents` 0.7.32 loads none of the anthropic, openai or google-genai SDKs. **Observed on:**
deepagents 0.7.25 (latest on PyPI, 2026-10-14) and 0.7.8 (installed here).

## Title

`import deepagents` imports `langchain_anthropic`, `langchain_openai` and `langchain_google_genai`
(and so the `anthropic`, `openai` and `google-genai` SDKs) whether or not the agent uses them

## What happens

Three module-level imports pull a provider package in:

- `deepagents/graph.py`: `from langchain_anthropic import ChatAnthropic`, and
  `deepagents/middleware/_prompt_caching.py`: `from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware`.
- `deepagents/middleware/filesystem.py`: `try: from langchain_openai import AzureChatOpenAI, ChatOpenAI`
  and `try: from langchain_google_genai import ChatGoogleGenerativeAI`, used only to decide, by
  `isinstance`, whether a model accepts non-PDF `file` blocks.

The two in `filesystem.py` are already guarded for absence, which shows they are optional. They are
still paid for by every process that imports the package, including a process whose model is a
different provider's client.

## Measurement

`python -X importtime -c "import deepagents"` in a fresh environment holding only `deepagents`,
`langchain-openai` and `langchain-mcp-adapters` (so `langchain-google-genai` is not installed):

```
import deepagents total 3.5 s (a shared, loaded 4-CPU host)
  1.80 s  50.6%  anthropic        (imported by deepagents.graph)
  0.50 s  14.0%  openai           (imported by langchain_openai)
  0.27 s   7.5%  langsmith
  everything else  under 0.15 s each
provider SDKs together: 67% of the import
```

With `langchain-google-genai` installed, as `deepagents` requires, its SDK is the largest single item
in the import of an application that depends on `deepagents` (about 2.5 s of a 16 s import of the
front door here). Removing the package from the environment, which the `try`/`except ImportError`
already tolerates, cut the front door's CPU time to import from 7.8 s to 6.3 s.

## Proposed change

Resolve the provider classes where they are used instead of at import: compare the model's class by
qualified name, or import inside the function that builds the `isinstance` tuple (cached), and import
`ChatAnthropic` and the caching middleware only when the active model is an Anthropic one. Optionally
move `langchain-google-genai` and `langchain-anthropic` from required to extras, as `langchain-aws`
already is.

## First-party shim

None possible: the cost is paid inside `import deepagents`, so the only first-party lever is to import
the package later (`src/chemclaw/api/app.py`'s import path) or to drop `langchain-google-genai` from
the environment (a dependency decision, not taken here).
