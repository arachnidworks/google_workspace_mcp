"""deploy.sh must reproduce the live Cloud Run config in one revision.

Runs the real script against a fake `gcloud` that records its argv, so no
network or credentials are needed.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parents[2] / "deploy.sh"

GCLOUD_SHIM = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["GCLOUD_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "--source" in sys.argv:
    src = sys.argv[sys.argv.index("--source") + 1]
    with open(os.environ["GCLOUD_LOG"] + ".dockerfile", "w") as f:
        f.write(open(os.path.join(src, "Dockerfile")).read())
    with open(os.environ["GCLOUD_LOG"] + ".files", "w") as f:
        f.write("\\n".join(sorted(os.listdir(src))))
if "describe" in sys.argv:
    print("https://workspace-mcp-420082496003.us-central1.run.app")
"""


def _run_deploy(tmp_path):
    shutil.copy(DEPLOY_SH, tmp_path / "deploy.sh")
    for name in ("Dockerfile", "Dockerfile.aw", ".gitignore"):
        shutil.copy(DEPLOY_SH.parent / name, tmp_path / name)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
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
        "GIT_CONFIG_NOSYSTEM": "1",
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
    # Exact match: --set-env-vars replaces the whole env, so any missing or
    # extra key is a change to live.
    assert env == {
        "MCP_ENABLE_OAUTH21": "true",
        "WORKSPACE_MCP_STATELESS_MODE": "true",
        "SERVICE_NAME": "workspace-mcp",
        "ALLOWED_EMAILS": "a@example.com,b@example.com",
        "GOOGLE_OAUTH_CLIENT_ID": "client-id",
        "WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND": "firestore",
        "WORKSPACE_MCP_OAUTH_PROXY_FIRESTORE_COLLECTION": "workspace_mcp_oauth_proxy",
        "WORKSPACE_MCP_AW_STORE_BACKEND": "firestore",
        "WORKSPACE_MCP_AW_REAUTH_COLLECTION": "aw_reauth_policy",
        "REAUTH_INACTIVITY_DAYS": "5",
        "REAUTH_MAX_DAYS": "30",
        "WORKSPACE_MCP_BRAND": "on",
        "WORKSPACE_MCP_BRAND_VERIFIED_DOMAIN": "arachnidworks.com",
        "WORKSPACE_MCP_BRAND_HELP_URL": "https://arachnidworks.com",
        "TOOLS": "gmail calendar drive docs sheets slides chat forms tasks contacts",
        "WORKSPACE_EXTERNAL_URL": "https://workspace-mcp-420082496003.us-central1.run.app",
    }
    assert _flag(deploys[0], "--set-secrets") == (
        "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:29"
    )


def test_deploy_builds_from_dockerfile_aw(tmp_path):
    _run_deploy(tmp_path)
    uploaded = (tmp_path / "gcloud.log.dockerfile").read_text()
    assert uploaded == (DEPLOY_SH.parent / "Dockerfile.aw").read_text()
    assert "aw_main.py" in uploaded
    files = (tmp_path / "gcloud.log.files").read_text().splitlines()
    # .env holds local config; .gitignore keeps it out of the upload.
    assert ".env" not in files
    assert "deploy.sh" in files
