"""
Unit tests for Google Chat space naming / identity resolution.

Fakes only, no network. Covers the ordered-source resolver chain, DM / group
labeling with self-exclusion, and the members.list fallbacks.
"""

import os
import sys

import pytest
from unittest.mock import Mock
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


@pytest.fixture(autouse=True)
def _clear_cache():
    """Keep the module-level sender cache from leaking between tests."""
    from gchat import space_naming

    space_naming._sender_name_cache.clear()
    yield
    space_naming._sender_name_cache.clear()


def _unwrap(tool):
    """Unwrap a FunctionTool + decorator chain to the original async function."""
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _http_error(status: int) -> HttpError:
    """Build a googleapiclient HttpError with the given status (e.g. 403)."""
    resp = Mock()
    resp.status = status
    return HttpError(resp=resp, content=b"forbidden")


def _member(user_id: str, display_name=None, member_type="HUMAN") -> dict:
    member = {"name": user_id, "type": member_type}
    if display_name is not None:
        member["displayName"] = display_name
    return {"member": member}


# ---------------------------------------------------------------------------
# resolve_space_label: direct messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_named_from_other_message_sender_without_members_call():
    """DM is named from the other posted sender; members.list is not needed."""
    chat_service = Mock()
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [
            {"sender": {"name": "users/self", "type": "HUMAN", "displayName": "Me"}},
            {
                "sender": {
                    "name": "users/999",
                    "type": "HUMAN",
                    "displayName": "Jacob Brain",
                }
            },
        ]
    }
    people_service = Mock()
    space = {"name": "spaces/D", "spaceType": "DIRECT_MESSAGE"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "DM: Jacob Brain"
    chat_service.spaces().members().list().execute.assert_not_called()


@pytest.mark.asyncio
async def test_dm_only_self_posted_uses_membership():
    """When only self has posted, name the DM from the enumerated other member."""
    chat_service = Mock()
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [
            {"sender": {"name": "users/self", "type": "HUMAN", "displayName": "Me"}}
        ]
    }
    chat_service.spaces().members().list().execute.return_value = {
        "memberships": [_member("users/self"), _member("users/888", display_name="Bob")]
    }
    people_service = Mock()
    space = {"name": "spaces/D", "spaceType": "DIRECT_MESSAGE"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "DM: Bob"


@pytest.mark.asyncio
async def test_dm_no_messages_and_members_403_is_unnamed_dm():
    """No posted senders and members.list 403 -> 'Unnamed DM'."""
    chat_service = Mock()
    chat_service.spaces().messages().list().execute.return_value = {"messages": []}
    chat_service.spaces().members().list().execute.side_effect = _http_error(403)
    people_service = Mock()
    space = {"name": "spaces/D", "spaceType": "DIRECT_MESSAGE"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "Unnamed DM"


# ---------------------------------------------------------------------------
# resolve_space_label: group chats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_enriches_members_from_senders_and_caps_at_five():
    """Members lacking displayName get enriched from message senders; a member
    who never posted (and lacks directory data) shows as raw id. Cap at 5."""
    # 7 non-self members, none carry displayName (typical for coworkers).
    memberships = [_member("users/self")]
    for uid in ("10", "11", "12", "13", "14", "15", "16"):
        memberships.append(_member(f"users/{uid}"))

    chat_service = Mock()
    chat_service.spaces().members().list().execute.return_value = {
        "memberships": memberships
    }
    # Everyone posted except users/12 (silent member -> raw id in label).
    posted = {
        "10": "Alice",
        "11": "Bob",
        "13": "Carol",
        "14": "Dave",
        "15": "Eve",
        "16": "Frank",
    }
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [
            {"sender": {"name": f"users/{uid}", "type": "HUMAN", "displayName": dn}}
            for uid, dn in posted.items()
        ]
    }
    people_service = Mock()
    people_service.people().get().execute.return_value = {}  # no directory data
    space = {"name": "spaces/G", "spaceType": "GROUP_CHAT"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "Group: Alice, Bob, users/12, Carol, Dave (+2 more)"


@pytest.mark.asyncio
async def test_group_members_403_falls_back_to_posted_senders():
    """No memberships scope -> name the group from posted non-self senders."""
    chat_service = Mock()
    chat_service.spaces().members().list().execute.side_effect = _http_error(403)
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [
            {"sender": {"name": "users/self", "type": "HUMAN", "displayName": "Me"}},
            {"sender": {"name": "users/20", "type": "HUMAN", "displayName": "Amy"}},
            {"sender": {"name": "users/21", "type": "HUMAN", "displayName": "Ben"}},
        ]
    }
    people_service = Mock()
    space = {"name": "spaces/G", "spaceType": "GROUP_CHAT"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "Group: Amy, Ben"


@pytest.mark.asyncio
async def test_group_no_members_and_no_senders_is_unnamed_group_chat():
    """members.list 403 and no posted senders -> 'Unnamed Group Chat'."""
    chat_service = Mock()
    chat_service.spaces().members().list().execute.side_effect = _http_error(403)
    chat_service.spaces().messages().list().execute.return_value = {"messages": []}
    people_service = Mock()
    space = {"name": "spaces/G", "spaceType": "GROUP_CHAT"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "Unnamed Group Chat"


@pytest.mark.asyncio
async def test_self_excluded_from_labels():
    """Self never appears in a DM/group label even when self has posted."""
    chat_service = Mock()
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [
            {
                "sender": {
                    "name": "users/self",
                    "type": "HUMAN",
                    "displayName": "MySelf",
                }
            },
            {"sender": {"name": "users/30", "type": "HUMAN", "displayName": "Other"}},
        ]
    }
    chat_service.spaces().members().list().execute.return_value = {
        "memberships": [_member("users/self"), _member("users/30")]
    }
    people_service = Mock()
    space = {"name": "spaces/G", "spaceType": "GROUP_CHAT"}

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert "MySelf" not in label
    assert label == "Group: Other"


@pytest.mark.asyncio
async def test_room_uses_display_name_without_api_call():
    """A named room returns its displayName and never calls members.list."""
    chat_service = Mock()
    people_service = Mock()
    space = {
        "name": "spaces/R",
        "spaceType": "SPACE",
        "displayName": "Engineering",
    }

    from gchat.space_naming import resolve_space_label

    label = await resolve_space_label(chat_service, people_service, space, "self")
    assert label == "Engineering"
    chat_service.spaces().members().list().execute.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_identity: ordered source chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_supplied_display_name_wins():
    """A supplied displayName short-circuits before any People API call."""
    people_service = Mock()

    from gchat.space_naming import resolve_identity

    result = await resolve_identity(
        people_service, {"name": "users/1", "displayName": "Direct Name"}
    )
    assert result == "Direct Name"
    people_service.people.assert_not_called()


@pytest.mark.asyncio
async def test_resolver_cache_hit():
    """A cached id resolves from cache without touching the People API."""
    from gchat import space_naming
    from gchat.space_naming import resolve_identity

    space_naming._sender_name_cache["users/42"] = "Cached Name"
    people_service = Mock()

    result = await resolve_identity(people_service, {"name": "users/42"})
    assert result == "Cached Name"
    people_service.people.assert_not_called()


def test_resolve_via_directory_is_noop():
    """The Option 3 seam is a no-op today: returns None, never raises."""
    from gchat.space_naming import _resolve_via_directory

    assert _resolve_via_directory("users/123") is None


@pytest.mark.asyncio
async def test_resolver_directory_noop_then_people_name():
    """With no displayName/cache and the directory seam None, People names win."""
    people_service = Mock()
    people_service.people().get().execute.return_value = {
        "names": [{"displayName": "People Name"}]
    }

    from gchat.space_naming import resolve_identity

    result = await resolve_identity(people_service, {"name": "users/50"})
    assert result == "People Name"


@pytest.mark.asyncio
async def test_resolver_people_email_fallback_then_raw_id():
    """People email is used when no name; raw users/{id} is the final fallback."""
    from gchat.space_naming import resolve_identity

    email_service = Mock()
    email_service.people().get().execute.return_value = {
        "emailAddresses": [{"value": "person@example.com"}]
    }
    result = await resolve_identity(email_service, {"name": "users/60"})
    assert result == "person@example.com"

    empty_service = Mock()
    empty_service.people().get().execute.return_value = {}
    result = await resolve_identity(empty_service, {"name": "users/61"})
    assert result == "users/61"


def test_list_spaces_not_gated_on_memberships_scope():
    """list_spaces must NOT require chat.memberships.readonly.

    Gating on it would make has_required_scopes hard-fail for any token that
    hasn't re-consented, so the tool body (and the DM message-sender fallback)
    would never run. Locks Design F: DMs keep working pre-consent.
    """
    from auth.scopes import CHAT_MEMBERSHIPS_READONLY_SCOPE
    from gchat.chat_tools import list_spaces

    fn = getattr(list_spaces, "fn", list_spaces)
    required = getattr(fn, "_required_google_scopes", None)
    assert required is not None
    assert CHAT_MEMBERSHIPS_READONLY_SCOPE not in required


@pytest.mark.asyncio
async def test_spacetype_filters_are_quoted():
    """spaces.list rejects unquoted enum filters (HTTP 400). The enum value must
    be wrapped in double quotes: spaceType = "DIRECT_MESSAGE" / "SPACE"."""
    from gchat.chat_tools import find_direct_message, list_spaces

    # find_direct_message -> quoted DM filter
    chat = Mock()
    chat.spaces().list().execute.return_value = {"spaces": []}
    await _unwrap(find_direct_message)(
        chat_service=chat,
        people_service=Mock(),
        user_google_email="test@example.com",
        query="x",
    )
    assert (
        chat.spaces().list.call_args.kwargs["filter"] == 'spaceType = "DIRECT_MESSAGE"'
    )

    # list_spaces(space_type="dm") -> quoted DM filter
    chat = Mock()
    chat.spaces().list().execute.return_value = {"spaces": []}
    await _unwrap(list_spaces)(
        chat_service=chat,
        people_service=Mock(),
        user_google_email="test@example.com",
        space_type="dm",
    )
    assert (
        chat.spaces().list.call_args.kwargs["filter"] == 'spaceType = "DIRECT_MESSAGE"'
    )

    # list_spaces(space_type="room") -> quoted SPACE filter
    chat = Mock()
    chat.spaces().list().execute.return_value = {"spaces": []}
    await _unwrap(list_spaces)(
        chat_service=chat,
        people_service=Mock(),
        user_google_email="test@example.com",
        space_type="room",
    )
    assert chat.spaces().list.call_args.kwargs["filter"] == 'spaceType = "SPACE"'


@pytest.mark.asyncio
async def test_resolve_sender_output_unchanged():
    """Regression guard: _resolve_sender still returns the sender's displayName."""
    from gchat.chat_tools import _resolve_sender

    people_service = Mock()
    result = await _resolve_sender(
        people_service, {"name": "users/1", "displayName": "Test User"}
    )
    assert result == "Test User"
