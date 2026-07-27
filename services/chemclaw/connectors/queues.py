"""Which Temporal queue a bundle's durable work runs on — derived, never declared three times.

The queue name had three copies that all had to agree: the manifest's `task_queue:`, the worker
module's `TASK_QUEUE` constant, and the Helm component `connector-worker-<name>`. Two that disagree
is a job sitting forever in a queue nobody polls — which is the exact failure
`workflows/registry.py` exists to prevent, one level up. Deriving it from the bundle name removes
the class of mistake rather than adding a check for it (D-118).
"""

from typing import Literal

from chemclaw.config import settings

# Where a declared job's workflow runs. `bundle` is the default and the reason the seam exists: the
# bundle's own worker, its own image, its own dependency closure. `background` is for the one case
# where a capability's closure *is* core's, so a separate worker would isolate nothing and cost a
# pod — the development report (D-115). Two members, both with a real caller.
JobRuntime = Literal["bundle", "background"]


def bundle_queue(connector: str) -> str:
    """The Temporal queue a bundle's own worker polls."""
    return f"connector-{connector}"


def task_queue_for(connector: str, runtime: JobRuntime) -> str:
    """Resolve a job's declared `runtime` to the queue its workflow is served on.

    `background` reads the queue from config rather than deriving it, because that name *is* a
    deployment knob (`CHEMCLAW_BACKGROUND_TASK_QUEUE`): a manifest that spelled it out would go
    stale the first time a deployment renamed it.
    """
    return bundle_queue(connector) if runtime == "bundle" else settings.background_task_queue
