"""
Current-user seam.

v1: the sidebar renders an `st.selectbox` of names drawn from the APP_USERS
env var (comma-separated). `get_current_user()` returns whichever name the
session picked, or None if nothing has been chosen yet.

Future: once IT confirms whether the internal server sits behind a reverse
proxy that injects an identity header (e.g. X-Forwarded-User from Azure AD
Application Proxy or Okta), only `get_current_user()` needs to change — it
will read the header via `st.context.headers` and the selectbox goes away.
No callers need touching.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import streamlit as st

logger = logging.getLogger(__name__)

_SESSION_KEY = "current_user"


def _app_users() -> list[str]:
    raw = os.environ.get("APP_USERS", "").strip()
    if not raw:
        raise RuntimeError(
            "APP_USERS is not set. Provide a comma-separated list of employee "
            "names in .env (e.g. APP_USERS=\"Jane Doe,John Smith\")."
        )
    return [name.strip() for name in raw.split(",") if name.strip()]


def user_selector() -> None:
    """
    Render the sidebar user picker. Safe to call on every Streamlit rerun.

    No default selection: the user must explicitly pick their name so we never
    attribute an upload to the wrong person.
    """
    users = _app_users()
    current = st.session_state.get(_SESSION_KEY)
    index = users.index(current) if current in users else None
    selected = st.sidebar.selectbox(
        "Signed in as",
        options=users,
        index=index,
        placeholder="Select your name…",
        key="_user_selector",
    )
    if selected:
        st.session_state[_SESSION_KEY] = selected


def get_current_user() -> Optional[str]:
    """
    Return the currently-selected user, or None if the session has not
    chosen one yet. Upload paths should block on None and prompt the user.
    """
    return st.session_state.get(_SESSION_KEY)
