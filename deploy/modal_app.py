"""Optional: run tailwatch's scheduler on Modal (serverless, a cron tick every 30 minutes).

    modal secret create tailwatch ANTHROPIC_API_KEY=... DATABASE_URL=postgresql://... [SLACK_BOT_TOKEN=...]
    modal volume create tailwatch-config      # holds tailwatch.yaml and your watchlist
    modal volume put tailwatch-config tailwatch.yaml /tailwatch.yaml
    modal deploy deploy/modal_app.py

Use Postgres (any hosted one) for the database: set ``database: ${DATABASE_URL}`` is not
interpolated, so put the URL in tailwatch.yaml or pass TAILWATCH_DATABASE (read below).
The Slack bot needs a long-lived process: run ``tailwatch slack`` with Docker anywhere instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .pip_install_from_pyproject(str(ROOT / "pyproject.toml"), optional_dependencies=["slack", "postgres"])
    .add_local_dir(str(ROOT / "src"), "/app/src", copy=True)
    .add_local_dir(str(ROOT / "prompts"), "/app/prompts", copy=True)
    .add_local_dir(str(ROOT / "skills"), "/app/skills", copy=True)
    .add_local_dir(str(ROOT / "presets"), "/app/presets", copy=True)
    .add_local_file(str(ROOT / "pyproject.toml"), "/app/pyproject.toml", copy=True)
    .add_local_file(str(ROOT / "README.md"), "/app/README.md", copy=True)
    .run_commands("pip install -e /app")
)
app = modal.App("tailwatch", image=image)
config_volume = modal.Volume.from_name("tailwatch-config", create_if_missing=True)


@app.function(schedule=modal.Cron("0,30 * * * *"), secrets=[modal.Secret.from_name("tailwatch")],
              volumes={"/config": config_volume}, timeout=6 * 3600)
def tick() -> list:
    from tailwatch import config as config_module
    from tailwatch.scheduler import tick as run_tick
    from tailwatch.store import open_store

    overrides = {"database": os.environ["TAILWATCH_DATABASE"]} if os.environ.get("TAILWATCH_DATABASE") else None
    config = config_module.load("/config/tailwatch.yaml", overrides=overrides)
    env = dict(os.environ)
    return run_tick(open_store(config.database, config), config, env)
