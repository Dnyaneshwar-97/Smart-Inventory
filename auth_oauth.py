"""
OAuth login layer for Streamlit (Google and GitHub).

Configure via environment variables only — never commit client secrets.

Redirect URI must match what you register at Google Cloud Console / GitHub OAuth App:
  {OAUTH_REDIRECT_BASE}/   e.g. http://localhost:8501/
"""

from __future__ import annotations

import html as html_module
import os
import secrets
import urllib.parse
from typing import Any

import httpx
import streamlit as st

SESSION_USER_KEY = "oauth_user"
_STATE_GOOGLE = "oauth_state_google"
_STATE_GITHUB = "oauth_state_github"


def _html_escape(s: str) -> str:
    return html_module.escape(s, quote=True)


def _env_flag(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _redirect_base() -> str:
    base = (os.environ.get("OAUTH_REDIRECT_BASE") or "http://localhost:8501").strip().rstrip("/")
    return base


def redirect_uri() -> str:
    """Registered OAuth redirect URL (must match provider console exactly)."""
    return f"{_redirect_base()}/"


def google_client() -> tuple[str | None, str | None]:
    cid = (os.environ.get("OAUTH_GOOGLE_CLIENT_ID") or "").strip()
    sec = (os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET") or "").strip()
    return (cid or None, sec or None)


def github_client() -> tuple[str | None, str | None]:
    cid = (os.environ.get("OAUTH_GITHUB_CLIENT_ID") or "").strip()
    sec = (os.environ.get("OAUTH_GITHUB_CLIENT_SECRET") or "").strip()
    return (cid or None, sec or None)


def auth_is_configured() -> bool:
    """At least one OAuth provider has client id + secret."""
    gc, gs = google_client()
    hc, hs = github_client()
    return bool(gc and gs) or bool(hc and hs)


def auth_is_enabled() -> bool:
    """Gate is on when OAuth is configured and not explicitly disabled."""
    if _env_flag("SMART_INVENTORY_AUTH_DISABLED", default=False):
        return False
    return auth_is_configured()


def auth_user() -> dict[str, Any] | None:
    u = st.session_state.get(SESSION_USER_KEY)
    return u if isinstance(u, dict) else None


def auth_is_logged_in() -> bool:
    u = auth_user()
    return bool(u and (u.get("email") or u.get("login") or u.get("sub")))


def auth_must_show_login_wall() -> bool:
    """True → render sign-in first and stop (before sidebar / inventory UI).

    Gate is on when OAuth credentials exist (unless disabled), or when
    SMART_INVENTORY_LOGIN_GATE=1 to show the login screen even without OAuth
    (configuration hint only; sign-in buttons need provider secrets).
    """
    if _env_flag("SMART_INVENTORY_AUTH_DISABLED", default=False):
        return False
    if auth_is_logged_in():
        return False
    if auth_is_configured():
        return True
    return _env_flag("SMART_INVENTORY_LOGIN_GATE", default=False)


def _qp_get(name: str) -> str | None:
    qp = st.query_params
    try:
        v = qp.get(name)
    except Exception:
        return None
    if v is None:
        return None
    if isinstance(v, list):
        return str(v[0]) if v else None
    return str(v)


def _qp_clear_auth_keys() -> None:
    for key in ("code", "state", "scope", "error", "error_description"):
        try:
            if key in st.query_params:
                del st.query_params[key]
        except Exception:
            pass


def _exchange_google_code(code: str) -> dict[str, Any]:
    cid, csec = google_client()
    if not cid or not csec:
        raise RuntimeError("Google OAuth is not configured.")
    data = {
        "code": code,
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "https://oauth2.googleapis.com/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        tok = r.json()
        access = tok.get("access_token")
        if not access:
            raise RuntimeError("Google token response missing access_token.")
        u = client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access}"},
        )
        u.raise_for_status()
        profile = u.json()
    return {
        "provider": "google",
        "sub": str(profile.get("sub", "")),
        "email": (profile.get("email") or "").strip(),
        "name": (profile.get("name") or "").strip(),
        "picture": (profile.get("picture") or "").strip(),
    }


def _exchange_github_code(code: str) -> dict[str, Any]:
    cid, csec = github_client()
    if not cid or not csec:
        raise RuntimeError("GitHub OAuth is not configured.")
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": cid,
                "client_secret": csec,
                "code": code,
                "redirect_uri": redirect_uri(),
            },
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        tok = r.json()
        access = tok.get("access_token")
        if not access:
            raise RuntimeError("GitHub token response missing access_token.")
        u = client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        )
        u.raise_for_status()
        profile = u.json()
        login = (profile.get("login") or "").strip()
        email = (profile.get("email") or "").strip()
        name = (profile.get("name") or login).strip()
        if not email:
            er = client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            )
            if er.is_success:
                for row in er.json():
                    if row.get("primary") and row.get("email"):
                        email = str(row["email"])
                        break
                if not email:
                    for row in er.json():
                        if row.get("email"):
                            email = str(row["email"])
                            break
        return {
            "provider": "github",
            "sub": str(profile.get("id", login)),
            "email": email,
            "name": name,
            "login": login,
            "picture": (profile.get("avatar_url") or "").strip(),
        }


def auth_try_finish_oauth_callback() -> None:
    """If URL contains OAuth callback params, complete login and clean the URL."""
    err = _qp_get("error")
    if err:
        st.session_state["_oauth_last_error"] = _qp_get("error_description") or err
        _qp_clear_auth_keys()
        return

    code = _qp_get("code")
    state = _qp_get("state")
    if not code or not state:
        return

    sg = st.session_state.get(_STATE_GOOGLE)
    sh = st.session_state.get(_STATE_GITHUB)
    if state == sg:
        provider = "google"
    elif state == sh:
        provider = "github"
    else:
        st.session_state["_oauth_last_error"] = "Invalid or expired login state. Try signing in again."
        _qp_clear_auth_keys()
        return

    try:
        if provider == "github":
            user = _exchange_github_code(code)
        else:
            user = _exchange_google_code(code)
    except Exception as ex:
        st.session_state["_oauth_last_error"] = str(ex)
        _qp_clear_auth_keys()
        return

    st.session_state[SESSION_USER_KEY] = user
    for k in (_STATE_GOOGLE, _STATE_GITHUB, "_oauth_last_error"):
        st.session_state.pop(k, None)
    _qp_clear_auth_keys()
    st.rerun()


def auth_google_authorize_url(*, state: str) -> str:
    cid, _ = google_client()
    if not cid:
        raise RuntimeError("Google OAuth client id missing.")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "include_granted_scopes": "true",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def auth_github_authorize_url(*, state: str) -> str:
    cid, _ = github_client()
    if not cid:
        raise RuntimeError("GitHub OAuth client id missing.")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "scope": "read:user user:email",
        "state": state,
    }
    return "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(params)


def auth_prepare_provider_states() -> None:
    """Create CSRF state tokens once per session for each provider button."""
    if _STATE_GOOGLE not in st.session_state:
        st.session_state[_STATE_GOOGLE] = secrets.token_urlsafe(32)
    if _STATE_GITHUB not in st.session_state:
        st.session_state[_STATE_GITHUB] = secrets.token_urlsafe(32)


def auth_logout() -> None:
    st.session_state.pop(SESSION_USER_KEY, None)
    st.session_state.pop(_STATE_GOOGLE, None)
    st.session_state.pop(_STATE_GITHUB, None)


def auth_render_login_screen(*, theme: dict[str, str]) -> None:
    """Full-page login when OAuth is enabled and user is anonymous."""
    t = theme
    err = st.session_state.pop("_oauth_last_error", None)

    # Full-width sign-in: hide sidebar chrome until authenticated.
    st.markdown(
        """
<style>
  section[data-testid="stSidebar"],
  div[data-testid="collapsedControl"] {
    display: none !important;
  }
  div[data-testid="stDecoration"] { display: none !important; }
  section[data-testid="stMain"] > div {
    padding-top: 1rem !important;
  }
</style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
<div style="max-width:420px;margin:3rem auto;padding:2rem 2rem 1.75rem;
  background:{t["surface"]};border-radius:16px;border:1px solid {t["border_strong"]};
  box-shadow:0 20px 50px rgba(0,0,0,.35);">
  <h1 style="margin:0 0 0.35rem 0;font-size:1.5rem;font-weight:800;color:{t["text"]};letter-spacing:-0.03em;text-align:center;">
    Smart Inventory
  </h1>
  <p style="margin:0 0 1.25rem 0;font-size:0.92rem;color:{t["text_muted"]};text-align:center;line-height:1.5;">
    Sign in to continue
  </p>
</div>
        """,
        unsafe_allow_html=True,
    )

    if err:
        st.error(_html_escape(err))

    gc, gs = google_client()
    hc, hs = github_client()
    if not auth_is_configured():
        st.warning(
            "OAuth is not configured. Set **OAUTH_GOOGLE_CLIENT_ID** / **OAUTH_GOOGLE_CLIENT_SECRET** "
            "and/or **OAUTH_GITHUB_CLIENT_ID** / **OAUTH_GITHUB_CLIENT_SECRET**, plus **OAUTH_REDIRECT_BASE** "
            "for production. Or set **SMART_INVENTORY_AUTH_DISABLED=1** to bypass this screen."
        )
        return

    auth_prepare_provider_states()
    c1, c2 = st.columns(2)
    with c1:
        if gc and gs:
            try:
                url = auth_google_authorize_url(state=str(st.session_state[_STATE_GOOGLE]))
            except Exception as ex:
                st.error(str(ex))
            else:
                st.link_button("Continue with Google", url, use_container_width=True, type="primary")
        else:
            st.caption("Google OAuth not configured.")
    with c2:
        if hc and hs:
            try:
                url = auth_github_authorize_url(state=str(st.session_state[_STATE_GITHUB]))
            except Exception as ex:
                st.error(str(ex))
            else:
                st.link_button("Continue with GitHub", url, use_container_width=True)

    st.markdown(
        f"""
<p style="margin-top:1.5rem;font-size:0.8rem;color:{t["text_muted"]};text-align:center;line-height:1.45;">
  Redirect URI for provider settings:<br/>
  <code style="font-size:0.75rem;">{_html_escape(redirect_uri())}</code>
</p>
        """,
        unsafe_allow_html=True,
    )


def auth_sidebar_account(*, theme: dict[str, str]) -> None:
    """Show signed-in user and sign-out in the sidebar."""
    u = auth_user()
    if not u:
        return
    t = theme
    label = (u.get("email") or u.get("login") or u.get("name") or "Signed in").strip()
    prov = (u.get("provider") or "").strip()
    st.sidebar.markdown(
        f'<p style="font-size:0.78rem;color:{t["sidebar_muted"]};margin:0 0 0.25rem 0;">Signed in</p>'
        f'<p style="font-size:0.88rem;color:{t["sidebar_text"]};margin:0 0 0.75rem 0;font-weight:600;">{_html_escape(label)}</p>'
        f'<p style="font-size:0.72rem;color:{t["sidebar_muted"]};margin:0;">{_html_escape(prov)}</p>',
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Sign out", key="oauth_sign_out"):
        auth_logout()
        st.rerun()
