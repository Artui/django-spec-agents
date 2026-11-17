# Timeouts and prompt caching

Both are pydantic-ai features that an agent from `build_agent` takes as it is. A run's
deadline goes on the run. The cache setting goes on the config.

## A run's deadline

pydantic-ai takes `timeout=` on each run (`run`, `run_stream`, `run_stream_events`,
`iter`, and the UI adapters' `run_stream` / `run_stream_native`), not on the `Agent`, so
`build_agent` has nothing to set. Whatever starts the run passes it:

```python
from pydantic_ai.exceptions import RunTimedOut

try:
    result = await agent.run(prompt, deps=deps, timeout=120.0)
except RunTimedOut as error:
    partial = error.all_messages()
```

The `timeout=` covers the whole run: every model request, every tool call, and any
agent run a tool starts, which inherits the deadline and can only shorten it. When the
deadline passes, the run stops the way a cancel does. `RunTimedOut` subclasses
`RunCancelled`, so catch it first if the two mean different things to you. It is
also a `TimeoutError`. `all_messages()` holds what the run completed. A streamed run
that times out mid-answer also keeps the text streamed so far, and you can resume from
there.

The capabilities `build_agent` composes leave the timeout alone. Suppose the deadline
passes while a tool is running. `ToolFailurePolicy` does not turn that into a failed
tool call, and the run carrying on would be wrong. The run ends with `RunTimedOut`, and
the policy logs nothing.

A tool can read the time it has left with `ctx.deadline` (a UTC `datetime`, or `None`
when the run has none) or `ctx.remaining_time()` (seconds). For example, it can pass
that time on to a slow call of its own.

Two limits apply to each model request rather than to the run, through `model_settings`:

```python
AgentConfig(model=..., model_settings={"request_timeout": 30.0, "max_retries": 2})
```

`request_timeout` bounds one model request and `max_retries` sets how often the
provider client retries it; pydantic-ai's `ModelSettings` reference has the exact
semantics per provider.

### Testing a deadline

`django_spec_agents.testing.slow_model(delay, chunks)` is a model whose requests
take `delay` seconds. The wait sits in the request, where a provider's wait is, and the
double never raises `TimeoutError` itself. When streamed, every chunk but the last
arrives before the wait. So a run that times out keeps those chunks as its partial
answer, exactly as when a real provider's stream stalls:

```python
import pytest
from pydantic_ai.exceptions import RunTimedOut
from django_spec_agents.testing import slow_model

agent = build_agent(registry, AgentConfig(model=slow_model(3600.0, ("partial ", "late"))))
with pytest.raises(RunTimedOut):
    await agent.run("go", deps=deps, timeout=0.1)
```

## Prompt caching

pydantic-ai's unified `cache` setting places the provider's cache breakpoints: on
the tool definitions and static instructions, and on the growing conversation. You
can turn it on through `model_settings`, or with the `Caching` capability:

```python
from pydantic_ai.capabilities import Caching

AgentConfig(model=..., model_settings={"cache": True})
AgentConfig(model=..., capabilities=[Caching(retention="1h")])
```

It is off by default, because a cache write costs more than uncached input: 1.25x for
a 5-minute cache, and 2x or more for longer ones. A cache read costs about 0.1x, so
caching pays on a long session that re-sends the same prefix, which is what an admin
chat does. A cached run's `usage` reports what it wrote and read, as
`cache_write_tokens` and `cache_read_tokens`, for a provider that reports them.

The capabilities `build_agent` composes, `AuditCapability`, `ToolGuard` and
`ToolFailurePolicy`, leave the tool definitions and instructions unchanged from one
request to the next. So the cached prefix stays valid for the whole run.

With the `[harness]` extra, `CodeMode` (pydantic-ai-harness 0.63 and later; the
extra's floor is 0.69) defaults to `dynamic_catalog=True`. Its catalog of sandboxed tools
then goes into the instructions, and a newly discovered tool is appended to the
history rather than rewriting `run_code`'s description, so a discovery keeps the cache
warm.
