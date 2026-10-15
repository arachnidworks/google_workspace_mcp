"""
AW Chat tool override (overlay; gchat/chat_tools.py stays upstream-owned).

Loaded in place of gchat.chat_tools by aw_main.py. Imports the upstream module
so every upstream Chat tool registers as usual, then swaps in the AW
list_spaces (resolve_names flag, DM message-sender fallback) and adds
find_direct_message. Both were live in workspace-mcp-00025-hom.
"""

import asyncio
import logging

from mcp.types import ToolAnnotations

import gchat.chat_tools  # noqa: F401  registers the upstream Chat tools
from auth.service_decorator import require_multiple_services
from core.server import server
from core.utils import handle_http_errors
from gchat.space_naming import (
    resolve_self_user_id,
    resolve_space_label,
)

logger = logging.getLogger(__name__)

# Upstream registers its own list_spaces; the AW version below replaces it.
server.local_provider.remove_tool("list_spaces")


@server.tool(
    title="List Spaces",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_multiple_services(
    [
        # Only chat_spaces_readonly is gated here. chat.memberships.readonly is
        # requested at consent (in CHAT_SCOPES) but NOT required, so tokens that
        # haven't re-consented still run the tool; members.list then 403s and
        # resolve_space_label degrades (DM -> message-sender fallback, group ->
        # "Unnamed Group Chat"), per Design F.
        {
            "service_type": "chat",
            "scopes": ["chat_spaces_readonly"],
            "param_name": "chat_service",
        },
        {
            "service_type": "people",
            "scopes": "contacts_read",
            "param_name": "people_service",
        },
    ]
)
@handle_http_errors("list_spaces", service_type="chat")
async def list_spaces(
    chat_service,
    people_service,
    user_google_email: str,
    page_size: int = 100,
    space_type: str = "all",  # "all", "room", "dm"
    resolve_names: bool = True,
) -> str:
    """
    Lists Google Chat spaces (rooms and direct messages) accessible to the user.

    Args:
        resolve_names: When True (default), resolve participant names so DMs and
                       group chats get human-readable labels. Set False for a
                       faster bare listing that skips per-space member lookups.

    Returns:
        str: A formatted list of Google Chat spaces accessible to the user.
    """
    logger.info(f"[list_spaces] Email={user_google_email}, Type={space_type}")

    # Build filter based on space_type
    filter_param = None
    if space_type == "room":
        filter_param = 'spaceType = "SPACE"'
    elif space_type == "dm":
        filter_param = 'spaceType = "DIRECT_MESSAGE"'

    request_params = {"pageSize": page_size}
    if filter_param:
        request_params["filter"] = filter_param

    response = await asyncio.to_thread(
        chat_service.spaces().list(**request_params).execute
    )

    spaces = response.get("spaces", [])
    if not spaces:
        return f"No Chat spaces found for type '{space_type}'."

    self_user_id = (
        await resolve_self_user_id(people_service, user_google_email)
        if resolve_names
        else None
    )

    output = [f"Found {len(spaces)} Chat spaces (type: {space_type}):"]
    # ponytail: naming does O(N) sequential API calls (messages.list per DM/group, +members.list for groups); googleapiclient objects aren't thread-safe to fan out. Fine at tens of spaces; add a cap/pagination if a workspace has hundreds.
    for space in spaces:
        if resolve_names:
            space_name = await resolve_space_label(
                chat_service, people_service, space, self_user_id
            )
        else:
            space_name = space.get("displayName", "Unnamed Space")
        space_id = space.get("name", "")
        space_type_actual = space.get("spaceType", "UNKNOWN")
        output.append(f"- {space_name} (ID: {space_id}, Type: {space_type_actual})")

    return "\n".join(output)


@server.tool(
    title="Find Direct Message",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_multiple_services(
    [
        # Only chat_spaces_readonly is gated here. chat.memberships.readonly is
        # requested at consent (in CHAT_SCOPES) but NOT required, so tokens that
        # haven't re-consented still run the tool; members.list then 403s and
        # resolve_space_label degrades (DM -> message-sender fallback, group ->
        # "Unnamed Group Chat"), per Design F.
        {
            "service_type": "chat",
            "scopes": ["chat_spaces_readonly"],
            "param_name": "chat_service",
        },
        {
            "service_type": "people",
            "scopes": "contacts_read",
            "param_name": "people_service",
        },
    ]
)
@handle_http_errors("find_direct_message", is_read_only=True, service_type="chat")
async def find_direct_message(
    chat_service,
    people_service,
    user_google_email: str,
    query: str,
) -> str:
    """
    Finds Google Chat direct messages (DMs) by the other participant's name.

    Lists DM spaces only, resolves each to the other participant's name, and
    returns those whose label contains the query (case-insensitive substring).

    Args:
        query: Text to match against the resolved DM participant name.

    Returns:
        str: Matching DM space id(s) and labels, or a not-found message.
    """
    logger.info(f"[find_direct_message] Email={user_google_email}, Query='{query}'")

    response = await asyncio.to_thread(
        chat_service.spaces()
        .list(filter='spaceType = "DIRECT_MESSAGE"', pageSize=100)
        .execute
    )
    spaces = response.get("spaces", [])
    if not spaces:
        return "No direct messages found."

    self_user_id = await resolve_self_user_id(people_service, user_google_email)

    query_lower = query.lower()
    matches = []
    for space in spaces:
        label = await resolve_space_label(
            chat_service, people_service, space, self_user_id
        )
        if query_lower in label.lower():
            matches.append((label, space.get("name", "")))

    if not matches:
        return f"No direct messages found matching '{query}'."

    output = [f"Found {len(matches)} direct message(s) matching '{query}':"]
    for label, space_id in matches:
        output.append(f"- {label} (ID: {space_id})")
    return "\n".join(output)
