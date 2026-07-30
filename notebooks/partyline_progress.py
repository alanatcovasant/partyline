# Databricks notebook source
# COMMAND ----------
# MAGIC %md
# MAGIC # Partyline progress reporter
# MAGIC One notebook backs every task in the example job. The `action` widget selects
# MAGIC behavior: `start` seeds a thread, `step` posts a threaded progress reply, `finish`
# MAGIC posts the summary. Configure via job parameters (surfaced here as widgets).

# COMMAND ----------
import os
import sys

# Import the pure helpers that sit beside this notebook. The bundle syncs the whole
# notebooks/ directory to the workspace, so partyline_progress_helpers.py lands in the
# same folder. Serverless notebook tasks run with the synced files CWD-adjacent, so the
# current directory is tried first; if the helper isn't there, fall back to the notebook's
# own directory taken from the run context.
_nb_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
try:
    _nb_dir = os.path.dirname(_nb_ctx.notebookPath().get())
except Exception:
    _nb_dir = None
for _dir in (os.getcwd(), _nb_dir):
    if _dir and _dir not in sys.path:
        sys.path.append(_dir)

import partyline_progress_helpers as pl  # noqa: E402

# COMMAND ----------
# Job-level parameters (see resources/example_progress_job.job.yml) arrive as widgets.
dbutils.widgets.text("app_name", "mcp-covasant-partyline")
dbutils.widgets.text("partyline_url", "")     # optional explicit override; blank => resolve by app_name
dbutils.widgets.text("agent_id", "")          # blank => derive from run context
dbutils.widgets.text("owner", "covasant")
dbutils.widgets.text("repo", "covasant-partyline")
dbutils.widgets.text("branch", "main")
dbutils.widgets.text("run_label", "")
# Run-context identifiers arrive as job parameters (dynamic value references in the
# job yml: {{job.id}} / {{job.run_id}}). Serverless blocks getContext().tags(), so
# these are injected as parameters, never read from the command context.
dbutils.widgets.text("job_id", "")
dbutils.widgets.text("run_id", "")
# Task-level parameters (set per task in the job yml).
dbutils.widgets.dropdown("action", "step", ["start", "step", "finish"])
dbutils.widgets.text("task_label", "work")

app_name = dbutils.widgets.get("app_name")
partyline_url = dbutils.widgets.get("partyline_url").strip()
agent_id_override = dbutils.widgets.get("agent_id")
owner = dbutils.widgets.get("owner")
repo = dbutils.widgets.get("repo")
branch = dbutils.widgets.get("branch")
run_label = dbutils.widgets.get("run_label")
job_id = dbutils.widgets.get("job_id").strip() or None
run_id = dbutils.widgets.get("run_id").strip() or None
action = dbutils.widgets.get("action")
task_label = dbutils.widgets.get("task_label")

# COMMAND ----------
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
# job_id / run_id come from job parameters (Change 2), resolved from dynamic value
# references — NOT from getContext().tags(), which raises Py4JSecurityException on
# serverless compute (CommandContext is not whitelisted).
agent_id = pl.derive_agent_id(job_id, run_id, agent_id_override)

# Resolve the app URL by name unless an explicit override was given. Pass app_name
# POSITIONALLY: the SDK's apps.get param name has drifted (name vs app_name) across
# versions, so a keyword can raise TypeError on the wrong one; positional binds either way.
url = partyline_url or w.apps.get(app_name).url

# Token: the run identity's bearer, obtained provider-agnostically. Two other paths are
# dead on serverless: ctx.apiToken() reads the non-whitelisted CommandContext (Py4J block),
# and w.config.oauth_token() raises AttributeError because the resolved credential provider
# is a bare header-factory function with no .oauth_token accessor. w.config.authenticate()
# calls that factory directly and returns the {"Authorization": "Bearer <tok>"} headers the
# SDK already uses for every request (the w.apps.get call just above proves it works here) —
# so it is correct for every provider. Strip the scheme; the REST helpers re-add "Bearer ".
def _run_bearer(wc):
    headers = wc.config.authenticate()  # Dict[str, str]; provider-agnostic
    hdr = headers.get("Authorization") or headers.get("authorization") or ""
    if hdr[:7].lower() == "bearer ":
        return hdr[7:]
    if hdr:
        return hdr
    raise RuntimeError(f"no bearer token in auth headers (keys={sorted(headers)})")

token = _run_bearer(w)

print(f"action={action} agent_id={agent_id} url={url} task_label={task_label}")

# COMMAND ----------
if action == "start":
    pl.register_agent(url, token, agent_id, owner, repo, branch)
    row = pl.post_message(
        url, token, agent_id=agent_id, message_type="status",
        summary=pl.build_start_summary(run_label),
        payload=f"Example progress job started. run_label={run_label!r}",
        repo=repo, branch=branch,
    )
    seed_id = int(row["id"])
    dbutils.jobs.taskValues.set(key="seed_id", value=seed_id)
    print(f"seeded thread seed_id={seed_id}")

elif action == "step":
    seed_id = dbutils.jobs.taskValues.get(taskKey="notify_start", key="seed_id", default=0, debugValue=0)
    metric = pl.simulated_metric(task_label)
    import time
    time.sleep(2)  # simulated work
    row = pl.post_message(
        url, token, agent_id=agent_id, message_type="status",
        summary=pl.build_step_summary(task_label, metric),
        repo=repo, branch=branch, reply_to_id=(seed_id or None),
    )
    dbutils.jobs.taskValues.set(key="outcome", value=pl.build_step_summary(task_label, metric))
    print(f"posted step reply id={row.get('id')} onto seed_id={seed_id}")

elif action == "finish":
    seed_id = dbutils.jobs.taskValues.get(taskKey="notify_start", key="seed_id", default=0, debugValue=0)
    outcomes = []
    for upstream in ("work_a", "work_b"):
        val = dbutils.jobs.taskValues.get(taskKey=upstream, key="outcome", default="", debugValue="")
        if val:
            outcomes.append(val)
    # ALL_DONE means we run even if an upstream failed; missing outcomes => report failure.
    ok = len(outcomes) == 2
    summary = pl.build_finish_summary(outcomes) if ok else pl.build_failure_summary(outcomes)
    pl.post_message(
        url, token, agent_id=agent_id,
        message_type="done" if ok else "blocked",
        summary=summary, repo=repo, branch=branch, reply_to_id=(seed_id or None),
    )
    print(f"finished ok={ok}: {summary}")

else:
    raise ValueError(f"unknown action: {action!r}")
