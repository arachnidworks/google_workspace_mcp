"""deploy.sh must reproduce the live Cloud Run config in one revision.

Runs the real script against a fake `gcloud` that records its argv, so no
network or credentials are needed.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parent.parent / "deploy.sh"

GCLOUD_SHIM = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["GCLOUD_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "describe" in sys.argv:
    print("https://workspace-mcp-420082496003.us-central1.run.app")
"""


def _run_deploy(tmp_path):
    shutil.copy(DEPLOY_SH, tmp_path / "deploy.sh")
    (tmp_path / ".env").write_text(
        "ALLOWED_EMAILS=a@example.com,b@example.com\nGOOGLE_OAUTH_CLIENT_ID=client-id\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "gcloud"
    shim.write_text(GCLOUD_SHIM)
    shim.chmod(0o755)
    log = tmp_path / "gcloud.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GCLOUD_LOG": str(log),
    }
    subprocess.run(
        ["bash", "deploy.sh"], cwd=tmp_path, env=env, check=True, capture_output=True
    )
    return [json.loads(line) for line in log.read_text().splitlines()]


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_deploy_sets_live_env_in_one_revision(tmp_path):
    calls = _run_deploy(tmp_path)
    deploys = [c for c in calls if c[:2] == ["run", "deploy"]]
    assert len(deploys) == 1
    assert not [c for c in calls if c[:3] == ["run", "services", "update"]]

    raw = _flag(deploys[0], "--set-env-vars")
    assert raw.startswith("^##^")
    env = dict(kv.split("=", 1) for kv in raw[4:].split("##"))
    assert env["ALLOWED_EMAILS"] == "a@example.com,b@example.com"
    assert env["WORKSPACE_MCP_STATELESS_MODE"] == "true"
    assert "TOOL_TIER" not in env
    assert env["TOOLS"] == (
        "gmail calendar drive docs sheets slides chat forms tasks contacts"
    )
    assert env["WORKSPACE_MCP_BRAND_VERIFIED_DOMAIN"] == "arachnidworks.com"
    assert env["WORKSPACE_MCP_BRAND_HELP_URL"] == "https://arachnidworks.com"
    assert env["WORKSPACE_EXTERNAL_URL"] == (
        "https://workspace-mcp-420082496003.us-central1.run.app"
    )
    assert _flag(deploys[0], "--set-secrets") == (
        "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:29"
    )
