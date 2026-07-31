"""
Google Chat space naming / identity resolution.

Centralizes resolving a Chat ``users/{id}`` to a display name and building
human-readable labels for spaces (rooms, DMs, group chats). Kept in its own
module so the diff to upstream-owned files stays minimal (easier fork sync).
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# In-memory cache for user ID -> display name (bounded to avoid unbounded growth)
_SENDER_CACHE_MAX_SIZE = 256
_sender_name_cache: Dict[str, str] = {}

# Cap on how many group-chat member names to list before "(+N more)".
_GROUP_LABEL_NAME_CAP = 5

# How many recent messages to scan when falling back to sender-based DM naming.
_DM_FALLBACK_MESSAGE_PAGE_SIZE = 10


def _cache_sender(user_id: str, name: str) -> None:
    """Store a resolved sender name, evicting oldest entries if cache is full."""
    if len(_sender_name_cache) >= _SENDER_CACHE_MAX_SIZE:
        to_remove = list(_sender_name_cache.keys())[: _SENDER_CACHE_MAX_SIZE // 2]
        for k in to_remove:
            del _sender_name_cache[k]
    _sender_name_cache[user_id] = name


def _user_numeric_id(user_name: str) -> str:
    """Extract the numeric id from a Chat ``users/{id}`` resource name."""
    return user_name.rsplit("/", 1)[-1] if user_name else ""


def _resolve_via_directory(user_id: str) -> Optional[str]:
    """Option 3 seam: resolve a Chat ``users/{id}`` via the Workspace directory.

    Implemented as a graceful no-op today: returns ``None`` and never raises, so
    it is safe to call whether or not a ``directory.readonly`` scope is held.
    When the Workspace admin enables Directory contact-sharing and we add that
    scope, the real People-directory / Admin-SDK lookup drops in HERE and nothing
    else changes (this is the single seam for "Option 3").
    """
    return None


async def resolve_identity(people_service, user_obj: dict) -> str:
    """Resolve a Chat ``users/{id}`` object to a display name.

    Ordered source chain, first hit wins:
      1. supplied ``displayName`` on the object
      2. in-memory cache
      3. directory seam (``_resolve_via_directory``; no-op today)
      4. People contacts slow path
      5. fallback: raw ``users/{id}`` string
    """
    # Fast path - Chat API sometimes provides displayName directly
    display_name = user_obj.get("displayName")
    if display_name:
        return display_name

    user_id = user_obj.get("name", "")  # e.g. "users/123456789"
    if not user_id:
        return "Unknown Sender"

    # Check cache
    if user_id in _sender_name_cache:
        return _sender_name_cache[user_id]

    # Directory seam (Option 3). Never raises; returns None until implemented.
    directory_name = _resolve_via_directory(user_id)
    if directory_name:
        _cache_sender(user_id, directory_name)
        return directory_name

    # People API contacts slow path.
    # Chat API uses "users/ID" but People API expects "people/ID"
    people_resource = user_id.replace("users/", "people/", 1)
    if people_service:
        try:
            person = await asyncio.to_thread(
                people_service.people()
                .get(resourceName=people_resource, personFields="names,emailAddresses")
                .execute
            )
            names = person.get("names", [])
            if names:
                resolved = names[0].get("displayName", user_id)
                _cache_sender(user_id, resolved)
                return resolved
            # Fall back to email if no name
            emails = person.get("emailAddresses", [])
            if emails:
                resolved = emails[0].get("value", user_id)
                _cache_sender(user_id, resolved)
                return resolved
        except HttpError as e:
            logger.debug(f"People API lookup failed for {user_id}: {e}")
        except Exception as e:
            logger.debug(f"Unexpected error resolving {user_id}: {e}")

    # Final fallback
    _cache_sender(user_id, user_id)
    return user_id


async def resolve_self_user_id(people_service, user_google_email: str) -> Optional[str]:
    """Resolve the caller's own Chat ``users/{id}`` numeric id via People ``people/me``.

    Returns the numeric Gaia id string (compared against the numeric part of a
    Chat ``users/{id}``), or ``None`` if it cannot be determined. Defensive: any
    failure returns ``None`` so labeling still works (it just cannot exclude self).
    """
    # ponytail: assumes People people/me Gaia id == Chat users/{id} numeric id (same Gaia namespace); verified live during rollout.
    if not people_service:
        return None
    try:
        me = await asyncio.to_thread(
            people_service.people()
            .get(resourceName="people/me", personFields="metadata,names,emailAddresses")
            .execute
        )
    except Exception as e:
        logger.debug(f"people/me lookup failed: {e}")
        return None

    for source in me.get("metadata", {}).get("sources", []):
        if source.get("type") == "PROFILE" and source.get("id"):
            return source["id"]
    return None


def _extract_human_members(members_response: dict) -> List[dict]:
    """Return the ``member`` user objects for HUMAN members of a members.list response.

    A membership looks like ``{"member": {"name": "users/123", "type": "HUMAN"}}``.
    Non-HUMAN members (bots) are skipped for labeling purposes.
    """
    users = []
    for membership in members_response.get("memberships", []):
        member = membership.get("member", {})
        if member.get("type") and member.get("type") != "HUMAN":
            continue
        if member.get("name", "").startswith("users/"):
            users.append(member)
    return users


async def _message_name_map(chat_service, space_name: str) -> Dict[str, str]:
    """Map numeric user id -> displayName from a space's most recent messages.

    Message ``sender.displayName`` is reliably populated (verified live), unlike
    members.list member objects for coworkers, so senders are the naming
    authority. Records the first (most recent) occurrence per HUMAN sender.
    Returns an empty map on any error.
    """
    try:
        response = await asyncio.to_thread(
            chat_service.spaces()
            .messages()
            .list(
                parent=space_name,
                pageSize=_DM_FALLBACK_MESSAGE_PAGE_SIZE,
                orderBy="createTime desc",
            )
            .execute
        )
    except Exception as e:
        logger.debug(f"messages.list failed for {space_name}: {e}")
        return {}

    name_map: Dict[str, str] = {}
    for msg in response.get("messages", []):
        sender = msg.get("sender", {})
        if sender.get("type") and sender.get("type") != "HUMAN":
            continue
        nid = _user_numeric_id(sender.get("name", ""))
        display_name = sender.get("displayName")
        if nid and display_name and nid not in name_map:
            name_map[nid] = display_name
    return name_map


async def _human_member_objs(
    chat_service, space_name: str, self_user_id: Optional[str]
) -> Tuple[List[dict], bool]:
    """Enumerate HUMAN member user-dicts of a space, excluding self.

    Returns ``(member_objs, ok)`` where ``ok`` is ``False`` if members.list
    raised (e.g. 403 before re-consent). Names are NOT resolved here; member
    objects may lack displayName for coworkers (enriched from messages later).
    When ``self_user_id`` is ``None`` self cannot be excluded.
    """
    try:
        response = await asyncio.to_thread(
            chat_service.spaces().members().list(parent=space_name).execute
        )
    except HttpError as e:
        logger.debug(f"members.list failed for {space_name}: {e}")
        return [], False
    except Exception as e:
        logger.debug(f"Unexpected members.list error for {space_name}: {e}")
        return [], False

    members = []
    for member in _extract_human_members(response):
        if self_user_id and _user_numeric_id(member.get("name", "")) == self_user_id:
            continue
        members.append(member)
    return members, True


def _enrich(member_obj: dict, name_map: Dict[str, str]) -> dict:
    """Inject a message-derived displayName onto a member obj that lacks one."""
    if not member_obj.get("displayName"):
        nid = _user_numeric_id(member_obj.get("name", ""))
        if nid in name_map:
            return {**member_obj, "displayName": name_map[nid]}
    return member_obj


def _format_group_label(names: List[str]) -> str:
    """Join member names for a group chat, capping at 5 with a "(+N more)" suffix."""
    shown = names[:_GROUP_LABEL_NAME_CAP]
    remaining = len(names) - len(shown)
    label = ", ".join(shown)
    if remaining > 0:
        label += f" (+{remaining} more)"
    return f"Group: {label}"


async def resolve_space_label(
    chat_service, people_service, space: dict, self_user_id: Optional[str]
) -> str:
    """Build a human-readable label for a Chat space.

    Rooms use their ``displayName`` (no API call). DMs and group chats take names
    from message senders (reliably populated), using members.list only to
    enumerate membership and enrich silent members with a posted displayName.
    """
    space_type = space.get("spaceType", "")
    space_name = space.get("name", "")

    # Room: the Chat API already supplies a displayName.
    display_name = space.get("displayName")
    if display_name:
        return display_name

    if space_type not in {"DIRECT_MESSAGE", "GROUP_CHAT"}:
        return "Unnamed Space"

    # Message senders are the naming authority (displayName reliably populated).
    name_map = await _message_name_map(chat_service, space_name)

    if space_type == "DIRECT_MESSAGE":
        # Primary: the other participant who has posted (works with current
        # scopes, no memberships scope needed). If self_user_id is None we
        # cannot exclude self, so this may pick self as a best-effort label.
        for nid, dn in name_map.items():
            if nid != self_user_id:
                return f"DM: {dn}"
        # Nobody else has posted: fall back to enumerating membership.
        member_objs, ok = await _human_member_objs(
            chat_service, space_name, self_user_id
        )
        if ok and member_objs:
            resolved = await resolve_identity(
                people_service, _enrich(member_objs[0], name_map)
            )
            return f"DM: {resolved}"
        return "Unnamed DM"

    # GROUP_CHAT
    member_objs, ok = await _human_member_objs(chat_service, space_name, self_user_id)
    if ok and member_objs:
        names = [
            await resolve_identity(people_service, _enrich(m, name_map))
            for m in member_objs
        ]
    else:
        # No memberships scope / error: name whoever has posted (non-self).
        names = [dn for nid, dn in name_map.items() if nid != self_user_id]
    if not names:
        return "Unnamed Group Chat"
    return _format_group_label(names)
