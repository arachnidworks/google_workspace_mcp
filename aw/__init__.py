"""AW overlay on upstream google_workspace_mcp.

Entry: aw_main.py (built by Dockerfile.aw, deployed by deploy.sh). overlay.py
patches core.server; chat_tools.py replaces upstream list_spaces and adds
find_direct_message. AW-only modules live in auth/aw_*.py and
gchat/space_naming.py; their tests live in tests/aw/. Upstream files stay
upstream-owned so syncs merge without conflicts.
"""
