# AI-friendly modular orchestrator boundaries

YTS Render keeps `JobOrchestrator` as the compatible lifecycle shell for jobs, worker loop, lease, retry, event logging and public methods, while domain behavior lives in focused modules under `app/pipelines/`, `app/providers/`, `app/publication_ops.py` and `app/hub_context.py`. This avoids a risky public API break while making common maintenance small enough for AI-assisted work: future changes should start in the module that owns the domain and only touch the orchestrator when lifecycle contracts change.

The trade-off is that some compatibility imports and wrappers remain temporarily in `app/orchestrator.py` and `app.providers`. Removing them is an incremental cleanup task, not a reason to move domain rules back into the shell.
