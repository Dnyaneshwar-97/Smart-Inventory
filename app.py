"""
Smart Inventory — Streamlit retail dashboard backed by Cloud Firestore.

Configure credentials via environment variables (see README); never commit secrets.

WCAG 2.x AA-oriented palette (contrast targets in _THEME comment block — verify with a contrast checker when changing).
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

_APP_ROOT = Path(__file__).resolve().parent
load_dotenv(_APP_ROOT / ".env")
_LOGO_ASSETS_DIR = _APP_ROOT / "assets"
_LOGO_FILENAMES = ("logo.png", "logo.jpg", "logo.jpeg", "logo.svg", "logo.webp", "brand.png")

# Warm orange dashboard shell: amber/brown dark canvas (not blue-navy), orange accents,
# cream surfaces — body copy on white cards stays readable.
_THEME = {
    "canvas": "#120807",
    "canvas_band": "#1a0f0a",
    "sidebar": "#0c0705",
    "sidebar_border": "#3f2318",
    "sidebar_text": "#fff7ed",
    "sidebar_muted": "#a8a29e",
    "surface": "#ffffff",
    "surface_soft": "#fff7ed",
    "text": "#1c1917",
    "text_muted": "#57534e",
    "text_on_dark": "#e7e5e4",
    "border": "#e7e5e4",
    "border_strong": "#d6d3d1",
    "toolbar": "#431407",
    "toolbar_border": "#c2410c",
    "primary": "#ea580c",
    "on_primary": "#ffffff",
    "primary_soft_bg": "#fff7ed",
    "accent_link": "#f97316",
    "metric_muted": "#78716c",
    "metric_value": "#1c1917",
    "metric_warn": "#c2410c",
    "metric_warn_bg": "#fff7ed",
    "chart_bar": "#ea580c",
    "focus_ring": "#fb923c",
    "tab_inactive": "#78716c",
    "tab_active": "#ea580c",
    "tab_bg": "#fff7ed",
}

_NAV_SECTIONS: list[str] = [
    "Dashboard",
    "Warehouses",
    "Inventory",
    "Suppliers",
    "Purchase orders",
    "Transfers",
    "Activity log",
]

_SECTION_HEADINGS: dict[str, tuple[str, str]] = {
    "Dashboard": ("Overview", "Warehouse-scoped metrics and catalog search."),
    "Warehouses": ("Warehouses", "Storage locations and on-hand stock namespaces."),
    "Inventory": ("Products & stock", "Catalog, adjustments, and movements."),
    "Suppliers": ("Suppliers", "Vendors and optional SKU links."),
    "Purchase orders": ("Purchase orders", "Create POs and record receipts."),
    "Transfers": ("Transfers", "Move stock between warehouses."),
    "Activity log": ("Activity", "Recent inventory events."),
}


def _file_to_data_uri(path: Path) -> str | None:
    """Small inline image for fixed-position UI (collapsed sidebar corner logo)."""
    if not path.is_file():
        return None
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        ext = path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resolve_brand_logo_path() -> Path | None:
    """Optional shop/factory logo: `SMART_INVENTORY_LOGO` env path, else first match under `assets/`."""
    raw = (os.environ.get("SMART_INVENTORY_LOGO") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_file():
            return p
    for name in _LOGO_FILENAMES:
        candidate = _LOGO_ASSETS_DIR / name
        if candidate.is_file():
            return candidate
    return None


def _workspace_logo_path() -> Path | None:
    """Logo file stored under `assets/` (ignores SMART_INVENTORY_LOGO env)."""
    for name in _LOGO_FILENAMES:
        candidate = _LOGO_ASSETS_DIR / name
        if candidate.is_file():
            return candidate
    return None


def _persist_logo_upload(uploaded: Any) -> tuple[bool, str]:
    """Write uploaded image to `assets/logo.<ext>` and remove conflicting logo.* copies."""
    try:
        _LOGO_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(uploaded.name).suffix.lower()
        allowed = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        if suffix not in allowed:
            suffix = ".png"
        dest = _LOGO_ASSETS_DIR / f"logo{suffix}"
        for name in _LOGO_FILENAMES:
            other = _LOGO_ASSETS_DIR / name
            if other.is_file() and other.resolve() != dest.resolve():
                try:
                    other.unlink()
                except OSError:
                    pass
        dest.write_bytes(uploaded.getvalue())
        return True, "Logo saved to assets."
    except OSError as exc:
        return False, f"Could not save logo ({exc}). Check folder permissions."


def _clear_workspace_logo_files() -> tuple[bool, str]:
    """Delete saved files under assets matching logo/brand names."""
    try:
        n = 0
        for name in _LOGO_FILENAMES:
            p = _LOGO_ASSETS_DIR / name
            if p.is_file():
                p.unlink()
                n += 1
        return True, "Removed saved logo." if n else "No file to remove."
    except OSError as exc:
        return False, str(exc)


def _render_sidebar_logo() -> None:
    """Reserved top-of-sidebar area for brand mark (image or placeholder)."""
    t = _THEME
    path = _resolve_brand_logo_path()
    if path is not None:
        st.sidebar.image(str(path), use_container_width=True)
        st.sidebar.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
    else:
        st.sidebar.markdown(
            f"""
<div style="margin-bottom:14px;padding:16px 12px;min-height:88px;border-radius:14px;border:2px dashed {t["toolbar_border"]};
  background:rgba(255,255,255,.05);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;">
  <span style="font-size:11px;font-weight:700;letter-spacing:0.1em;color:{t["sidebar_muted"]};text-transform:uppercase;">Shop / factory logo</span>
  <span style="font-size:11px;color:{t["sidebar_muted"]};opacity:.9;margin-top:8px;line-height:1.45;">
    Use <strong>Logo · upload</strong> below, add <code style="color:{t["accent_link"]};">assets/logo.png</code>,
    or env <code style="color:{t["accent_link"]};">SMART_INVENTORY_LOGO</code>
  </span>
</div>
            """,
            unsafe_allow_html=True,
        )


def _render_logo_upload_controls() -> None:
    """Sidebar UI to upload/replace/remove logo stored in `assets/`."""
    with st.sidebar.expander("Logo · upload or replace", expanded=False):
        up = st.file_uploader(
            "Choose logo image",
            type=["png", "jpg", "jpeg", "webp", "svg"],
            key="brand_logo_uploader",
            help="Stored on the server under assets/logo.*",
        )
        if up is not None:
            sig = (up.name, up.size)
            last = st.session_state.get("_logo_upload_sig")
            if last != sig:
                ok, msg = _persist_logo_upload(up)
                if ok:
                    st.session_state["_logo_upload_sig"] = sig
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
        st.caption(
            "Upload replaces any previous saved logo. "
            "`SMART_INVENTORY_LOGO` in environment overrides the file below if set."
        )
        if _workspace_logo_path() is not None:
            if st.button("Remove saved logo file", key="btn_clear_workspace_logo"):
                ok, msg = _clear_workspace_logo_files()
                if ok:
                    st.session_state.pop("_logo_upload_sig", None)
                    st.info(msg)
                    st.rerun()
                else:
                    st.error(msg)


def _panel():
    """Grouped UI block; bordered when supported (Streamlit ≥1.33)."""
    try:
        return st.container(border=True)
    except TypeError:
        return st.container()


def _page_intro(section: str) -> None:
    """Neutral page heading — typography on the navy canvas (no card/orange strip that reads like an alert)."""
    title, blurb = _SECTION_HEADINGS.get(section, (section, ""))
    t = _THEME
    st.markdown(
        f"""
<div style="margin-bottom:1.35rem;padding-bottom:1.05rem;border-bottom:1px solid {t["toolbar_border"]};">
  <h2 style="margin:0 0 0.45rem 0;font-size:1.65rem;font-weight:700;color:{t["sidebar_text"]};letter-spacing:-0.03em;">
    {html.escape(title)}
  </h2>
  <p style="margin:0;font-size:0.94rem;color:{t["sidebar_muted"]};line-height:1.55;max-width:46rem;">
    {html.escape(blurb)}
  </p>
</div>
        """,
        unsafe_allow_html=True,
    )


def _apply_streamlit_secrets_to_env() -> None:
    """Inject Streamlit Cloud / `.streamlit/secrets.toml` into os.environ before `database` imports Firebase.

    Avoid `key in st.secrets`: Streamlit may raise StreamlitSecretNotFoundError from `__contains__`
    when secrets.toml is missing or a key is absent (common when developing locally with `.env` only).
    """
    try:
        sec = st.secrets
    except Exception:
        return

    def put(key: str, val: object) -> None:
        if val is None:
            return
        text = str(val).strip()
        if text:
            os.environ[key] = text

    def try_secret(key: str) -> None:
        try:
            put(key, sec[key])
        except Exception:
            pass

    for key in (
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "FIREBASE_CREDENTIALS_JSON",
        "OAUTH_REDIRECT_BASE",
        "OAUTH_GOOGLE_CLIENT_ID",
        "OAUTH_GOOGLE_CLIENT_SECRET",
        "OAUTH_GITHUB_CLIENT_ID",
        "OAUTH_GITHUB_CLIENT_SECRET",
        "SMART_INVENTORY_AUTH_DISABLED",
        "SMART_INVENTORY_LOGIN_GATE",
    ):
        try_secret(key)

    try:
        fc = sec["firebase_credentials"]
        put("FIREBASE_CREDENTIALS_JSON", json.dumps(dict(fc)))
    except Exception:
        pass

    try:
        g = sec["gcp"]
        if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            try:
                put("GOOGLE_CLOUD_PROJECT", g["project_id"])
            except Exception:
                pass
        try:
            put("FIREBASE_CREDENTIALS_JSON", str(g["credentials_json"]))
        except Exception:
            pass
    except Exception:
        pass


_apply_streamlit_secrets_to_env()

import streamlit.components.v1 as components
from auth_oauth import (
    auth_must_show_login_wall,
    auth_render_login_screen,
    auth_sidebar_account,
    auth_try_finish_oauth_callback,
)
from database import FirestoreManager, credential_debug_info


def _inject_global_theme() -> None:
    """Warm orange-tinted dark shell and CTAs (no blue-navy chrome).

    Must run on every Streamlit rerun: custom HTML/CSS from a prior run is dropped from the DOM
    if we skip emitting it on the next run, so navigation would strip sidebar/list styling.
    """
    t = _THEME
    st.markdown(
        f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&display=swap');
  html, body, .stApp {{
    font-family: 'DM Sans', ui-sans-serif, system-ui, sans-serif !important;
  }}
  .stApp {{
    background: radial-gradient(1100px 560px at 18% -8%, rgba(234,88,12,0.28) 0%, transparent 58%),
                radial-gradient(820px 480px at 92% 8%, rgba(251,146,60,0.14) 0%, transparent 52%),
                linear-gradient(168deg, {t["canvas"]} 0%, #050302 100%) !important;
  }}
  [data-testid="stHeader"] {{
    background: transparent !important;
    border-bottom: 1px solid {t["toolbar_border"]}30;
  }}
  section[data-testid="stSidebar"] {{
    background: {t["sidebar"]} !important;
    border-right: 1px solid {t["sidebar_border"]} !important;
  }}
  section[data-testid="stSidebar"] [data-testid="stImage"] img {{
    border-radius: 12px !important;
    object-fit: contain !important;
  }}
  /* Mini logo only when sidebar is collapsed (Streamlit sets aria-expanded on the sidebar section) */
  body:has(section[data-testid="stSidebar"][aria-expanded="false"]) section[data-testid="stSidebar"] [data-testid="stImage"] {{
    display: none !important;
  }}
  .si-mini-brand-wrap {{
    position: fixed !important;
    top: 10px !important;
    left: 10px !important;
    z-index: 1000010 !important;
    width: 42px !important;
    height: 42px !important;
    border-radius: 12px !important;
    overflow: hidden !important;
    box-shadow: 0 4px 18px rgba(0,0,0,.4) !important;
    border: 1px solid rgba(255,255,255,.12) !important;
    background: rgba(12,8,7,.92) !important;
    display: none !important;
    align-items: center !important;
    justify-content: center !important;
    pointer-events: none !important;
  }}
  body:has(section[data-testid="stSidebar"][aria-expanded="false"]) .si-mini-brand-wrap {{
    display: flex !important;
  }}
  .si-mini-brand-wrap img {{
    width: 100% !important;
    height: 100% !important;
    object-fit: contain !important;
    padding: 5px !important;
    display: block !important;
  }}
  section[data-testid="stSidebar"] .block-container {{
    padding-top: 1.25rem !important;
  }}
  section[data-testid="stSidebar"] p, section[data-testid="stSidebar"] span,
  section[data-testid="stSidebar"] label {{
    color: {t["sidebar_muted"]} !important;
  }}
  section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {{
    color: {t["sidebar_text"]} !important;
  }}
  section[data-testid="stSidebar"] [data-baseweb="radio"] label {{
    color: {t["sidebar_text"]} !important;
  }}
  section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{
    font-size: 0.7rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    color: {t["sidebar_muted"]} !important;
    opacity: 0.95 !important;
    margin-bottom: 0.35rem !important;
  }}
  section[data-testid="stSidebar"] div[role="radiogroup"] {{
    display: flex !important;
    flex-direction: column !important;
    gap: 6px !important;
    padding: 10px 8px !important;
    border-radius: 14px !important;
    background: rgba(255,255,255,.035) !important;
    border: 1px solid rgba(255,255,255,.08) !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.04) !important;
  }}
  section[data-testid="stSidebar"] [data-baseweb="radio"] {{
    margin-bottom: 0 !important;
    align-items: center !important;
    min-height: auto !important;
    padding: 9px 12px !important;
    border-radius: 11px !important;
    border: 1px solid rgba(255,255,255,.06) !important;
    background: rgba(0,0,0,.12) !important;
    transition: background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease !important;
  }}
  section[data-testid="stSidebar"] [data-baseweb="radio"]:hover {{
    background: rgba(255,255,255,.06) !important;
    border-color: rgba(255,255,255,.1) !important;
  }}
  section[data-testid="stSidebar"] [data-baseweb="radio"]:has(input:checked) {{
    background: rgba(234,88,12,.22) !important;
    border-color: rgba(234,88,12,.48) !important;
    box-shadow: 0 0 0 1px rgba(234,88,12,.2) inset !important;
  }}
  section[data-testid="stSidebar"] [data-baseweb="radio"] input {{
    accent-color: {t["primary"]} !important;
  }}
  .main .block-container {{
    padding-top: 1.25rem !important;
    padding-bottom: 3rem !important;
    max-width: 1280px !important;
  }}
  [data-testid="stVerticalBlockBorderWrapper"] {{
    background: {t["surface"]} !important;
    border-color: {t["border_strong"]} !important;
    border-radius: 14px !important;
    box-shadow: 0 14px 44px rgba(0,0,0,.32) !important;
  }}
  .stTabs [data-baseweb="tab-list"] {{
    background: {t["tab_bg"]} !important;
    border-radius: 12px !important;
    padding: 6px !important;
    gap: 4px !important;
  }}
  [data-baseweb="tab"] {{
    color: {t["tab_inactive"]} !important;
    border-radius: 8px !important;
  }}
  [data-baseweb="tab"][aria-selected="true"] {{
    color: {t["tab_active"]} !important;
    font-weight: 600 !important;
    background: {t["surface"]} !important;
    box-shadow: 0 1px 3px rgba(0,0,0,.12) !important;
  }}
  button[kind="primary"] {{
    background-color: {t["primary"]} !important;
    border-color: {t["primary"]} !important;
    color: {t["on_primary"]} !important;
    font-weight: 600 !important;
    border-radius: 10px !important;
  }}
  button[kind="secondary"] {{
    border-radius: 10px !important;
  }}
  div[data-testid="stMetricValue"] {{
    color: {t["text"]} !important;
  }}
  button:focus-visible, [role="tab"]:focus-visible {{
    outline: 2px solid {t["focus_ring"]} !important;
    outline-offset: 2px !important;
  }}
  [data-testid="stVegaLiteChart"] g.mark-bar rect,
  [data-testid="stVegaLiteChart"] path.mark-bar {{
    fill: {t["chart_bar"]} !important;
  }}
</style>
        """,
        unsafe_allow_html=True,
    )


def _render_floating_mini_logo() -> None:
    """Fixed top-left mini mark when sidebar is collapsed (CSS :has + aria-expanded on stSidebar)."""
    path = _resolve_brand_logo_path()
    if path is None:
        return
    uri = _file_to_data_uri(path)
    if uri is None:
        return
    st.markdown(
        f'<div class="si-mini-brand-wrap" aria-hidden="true"><img src="{uri}" alt="" /></div>',
        unsafe_allow_html=True,
    )


def _metric_cards_tailwind(total_value: float, low_count: int) -> None:
    """KPI cards styled like Appsmith template: white tiles on navy + highlighted low-stock."""
    safe_val = html.escape(f"{total_value:,.2f}")
    safe_low = html.escape(str(low_count))
    t = _THEME
    components.html(
        f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-transparent m-0 p-0">
<div class="rounded-xl p-4 mb-1" style="background:{t["toolbar"]};border:1px solid {t["toolbar_border"]};">
  <p class="text-[11px] uppercase tracking-[0.14em] mb-3" style="color:{t["text_on_dark"]};opacity:.75;">Key metrics</p>
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 font-sans">
    <div class="rounded-xl border p-5 shadow-xl" style="background:{t["surface"]};border-color:{t["border_strong"]};color:{t["text"]};box-shadow:0 18px 48px rgba(0,0,0,.35);">
      <div class="flex justify-between items-start mb-2">
        <span class="text-xs font-semibold uppercase tracking-wide" style="color:{t["metric_muted"]};">Total stock value</span>
        <span style="color:{t["accent_link"]};font-size:18px;line-height:1;">↗</span>
      </div>
      <p class="text-3xl font-bold tracking-tight" style="color:{t["metric_value"]};">Rs {safe_val}</p>
      <p class="text-xs mt-2" style="color:{t["metric_muted"]};">Σ (qty × unit price)</p>
    </div>
    <div class="rounded-xl border p-5 shadow-xl" style="background:{t["metric_warn_bg"]};border-color:{t["border_strong"]};box-shadow:0 18px 48px rgba(0,0,0,.28);">
      <div class="flex justify-between items-start mb-2">
        <span class="text-xs font-semibold uppercase tracking-wide" style="color:{t["metric_muted"]};">Low stock SKUs</span>
        <span style="color:{t["metric_warn"]};font-size:18px;line-height:1;">!</span>
      </div>
      <p class="text-3xl font-bold tracking-tight" style="color:{t["metric_warn"]};">{safe_low}</p>
      <p class="text-xs mt-2" style="color:{t["metric_muted"]};">Qty at or below reorder level</p>
    </div>
  </div>
</div>
</body></html>
        """,
        height=260,
        scrolling=False,
    )


def _appsmith_scope_strip(scope_display: str) -> None:
    """Dark toolbar strip similar to Appsmith dashboard filter row."""
    t = _THEME
    safe = html.escape(scope_display)
    st.markdown(
        f"""
<div style="background:{t["toolbar"]};border:1px solid {t["toolbar_border"]};border-radius:12px;padding:12px 18px;margin:0 0 1rem 0;display:flex;flex-wrap:wrap;align-items:center;gap:14px;">
  <span style="font-size:11px;font-weight:700;letter-spacing:0.14em;color:{t["text_on_dark"]};opacity:.72;">SCOPE</span>
  <span style="font-size:14px;font-weight:600;color:{t["sidebar_text"]};">{safe}</span>
</div>
        """,
        unsafe_allow_html=True,
    )


def _section_title(text: str, *, level: str = "h3") -> None:
    t = _THEME
    fs = "1.05rem" if level == "h4" else "1.15rem"
    st.markdown(
        f'<{level} style="font-size:{fs};font-weight:600;color:{t["text"]};margin:1.1rem 0 0.55rem 0;padding-bottom:0.35rem;border-bottom:1px solid {t["border"]};letter-spacing:-0.02em;">{html.escape(text)}</{level}>',
        unsafe_allow_html=True,
    )


@st.cache_resource
def get_firestore_manager(*, _schema_version: str = "2026-05-multi-warehouse") -> FirestoreManager:
    """`_schema_version` invalidates Streamlit's cache when FirestoreManager API changes."""
    _ = _schema_version
    return FirestoreManager()


def _warehouse_choices(db: FirestoreManager) -> tuple[list[str], dict[str, str]]:
    """Returns (ids, id_to_label)."""
    try:
        rows = db.list_warehouses(active_only=True)
    except Exception:
        return [], {}
    ids = [str(r["id"]) for r in rows]
    label: dict[str, str] = {}
    for r in rows:
        rid = str(r["id"])
        short = f"{rid[:8]}…" if len(rid) > 8 else rid
        label[rid] = f"{r.get('name', rid)} ({short})"
    return ids, label


def _low_stock_products(db: FirestoreManager, warehouse_id: str | None) -> list[dict[str, Any]]:
    return db.list_low_stock_products(warehouse_id=warehouse_id)


def _render_migration_sidebar(db: FirestoreManager) -> None:
    try:
        legacy = db.has_legacy_flat_quantity()
    except Exception:
        legacy = False
    if legacy:
        st.sidebar.warning("Legacy per-product `quantity` detected. Migrate to warehouse stock.")
        if st.sidebar.button("Migrate to default warehouse", key="btn_migrate"):
            try:
                res = db.migrate_legacy_inventory_to_default_warehouse()
                st.sidebar.success(
                    f"Migrated {res.get('products_moved', 0)} products → warehouse `{res.get('warehouse_id', '')}`."
                )
                st.rerun()
            except Exception as ex:
                st.sidebar.error(str(ex))


def main() -> None:
    st.set_page_config(
        page_title="Smart Inventory",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # OAuth return URL must be handled before any other UI; then sign-in page first if needed.
    auth_try_finish_oauth_callback()
    if auth_must_show_login_wall():
        _inject_global_theme()
        auth_render_login_screen(theme=_THEME)
        st.stop()

    _inject_global_theme()
    _render_floating_mini_logo()
    _render_sidebar_logo()
    _render_logo_upload_controls()
    t = _THEME

    try:
        db = get_firestore_manager()
    except Exception as err:
        st.error(
            "Could not connect to Firestore. "
            "Locally: use `.env`. **Streamlit Cloud:** set secrets (see README) — "
            "at minimum `GOOGLE_CLOUD_PROJECT` and `FIREBASE_CREDENTIALS_JSON` (or `firebase_credentials`)."
        )
        with st.expander("Credential diagnostics (no secrets shown)"):
            for k, v in credential_debug_info().items():
                st.text(f"{k}: {v}")
        st.code(
            "export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-service-account.json\n"
            "# or\n"
            "export FIREBASE_CREDENTIALS_JSON='{\"type\":\"service_account\",...}'\n"
            "export GOOGLE_CLOUD_PROJECT=your-project-id",
            language="bash",
        )
        st.exception(err)
        return

    jump_sku = st.session_state.pop("pending_sku_jump", None)
    if jump_sku:
        st.session_state["inv_search"] = jump_sku
        st.session_state["main_nav"] = "Inventory"

    st.sidebar.markdown(
        f'<p style="font-size:1.35rem;font-weight:800;color:{t["sidebar_text"]};margin:0 0 0.15rem 0;letter-spacing:-0.03em;">Smart Inventory</p>'
        f'<p style="font-size:0.85rem;color:{t["sidebar_muted"]};margin:0 0 1rem 0;">Inventory dashboard</p>',
        unsafe_allow_html=True,
    )
    section = st.sidebar.radio(
        "Navigate",
        _NAV_SECTIONS,
        label_visibility="visible",
        key="main_nav",
    )
    auth_sidebar_account(theme=t)
    _render_migration_sidebar(db)

    auth_stub = FirestoreManager.ensure_authenticated_user()
    if auth_stub is not None:
        st.sidebar.caption(f"Auth hook: {auth_stub}")

    _page_intro(section)

    if section == "Dashboard":
        render_dashboard(db)
    elif section == "Warehouses":
        render_warehouses(db)
    elif section == "Inventory":
        render_inventory(db)
    elif section == "Suppliers":
        render_suppliers(db)
    elif section == "Purchase orders":
        render_purchase_orders(db)
    elif section == "Transfers":
        render_transfers(db)
    else:
        render_activity(db)


def render_dashboard(db: FirestoreManager) -> None:
    wh_ids, wh_labels = _warehouse_choices(db)
    scope_options = ["All warehouses"] + wh_ids
    top_a, top_b = st.columns([1, 2])
    with top_a:
        scope_ix = st.selectbox(
            "Scope",
            range(len(scope_options)),
            format_func=lambda i: scope_options[i]
            if i == 0
            else wh_labels.get(scope_options[i], scope_options[i]),
            key="dash_wh_scope",
            help="Metrics and search filter to one warehouse or aggregate across all.",
        )
    wh_filter: str | None = None if scope_ix == 0 else scope_options[scope_ix]
    scope_display = scope_options[0] if scope_ix == 0 else wh_labels.get(
        scope_options[scope_ix], scope_options[scope_ix]
    )
    with top_b:
        st.caption(
            "Tip: low-stock tiles jump to **Inventory** with that SKU. "
            "Create warehouses first if the scope list is empty."
        )

    _appsmith_scope_strip(scope_display)

    try:
        m = db.get_dashboard_metrics(warehouse_id=wh_filter)
    except Exception as err:
        st.error("Failed to load metrics.")
        st.exception(err)
        return

    with _panel():
        _section_title("Live metrics", level="h4")
        _metric_cards_tailwind(float(m["total_stock_value"]), int(m["low_stock_count"]))

        try:
            rows_chart = db.search_products("", warehouse_id=wh_filter)
        except Exception:
            rows_chart = []
        if rows_chart:
            _section_title("Stock quantity — top SKUs", level="h4")
            cdf = pd.DataFrame(rows_chart)
            if "quantity" in cdf.columns and not cdf.empty:
                topn = cdf.nlargest(min(10, len(cdf)), "quantity")[["sku", "quantity"]].set_index("sku")
                st.bar_chart(topn, height=280)

    low_cnt = int(m["low_stock_count"])
    if low_cnt > 0:
        try:
            low_rows = _low_stock_products(db, warehouse_id=wh_filter)
        except Exception as err:
            st.exception(err)
            low_rows = []
        if low_rows:
            with _panel():
                _section_title("Low stock — quick jump", level="h4")
                st.markdown(
                    f'<p style="color:{_THEME["text_muted"]};font-size:0.9rem;margin:0 0 0.75rem 0;">'
                    "Opens <strong>Inventory</strong> filtered to that SKU."
                    "</p>",
                    unsafe_allow_html=True,
                )
                chunk_size = 4
                for chunk_start in range(0, len(low_rows), chunk_size):
                    chunk = low_rows[chunk_start : chunk_start + chunk_size]
                    cols = st.columns(len(chunk))
                    for col, row in zip(cols, chunk, strict=True):
                        sku = str(row.get("sku", ""))
                        name = str(row.get("name", ""))
                        qty = int(row.get("quantity", 0))
                        with col:
                            if st.button(
                                f"{sku} · qty {qty}",
                                key=f"jump_low_{sku}_{wh_filter or 'all'}",
                                help=name or sku,
                                use_container_width=True,
                                type="secondary",
                            ):
                                st.session_state["pending_sku_jump"] = sku
                                st.rerun()

    with _panel():
        _section_title("Catalog search", level="h4")
        q = st.text_input("Filter by SKU or name", key="dash_search", placeholder="Type to filter…")
        try:
            rows = db.search_products(q, warehouse_id=wh_filter)
        except Exception as err:
            st.exception(err)
            return

        if not rows:
            st.info("No products match.")
            return

        df = pd.DataFrame(rows)
        cols = ["sku", "name", "category", "quantity", "reorder_level", "unit_price"]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        st.dataframe(
            df[cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "unit_price": st.column_config.NumberColumn(format="Rs %.2f"),
                "quantity": st.column_config.NumberColumn(format="%d"),
                "reorder_level": st.column_config.NumberColumn(format="%d"),
            },
        )


def _render_add_product_form(db: FirestoreManager, *, show_heading: bool = True) -> None:
    if show_heading:
        _section_title("Add product", level="h4")
    wh_ids, wh_labels = _warehouse_choices(db)
    with st.form("add_product", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        sku = c1.text_input("SKU *")
        name = c2.text_input("Name *")
        category = c3.text_input("Category")
        c4, c5 = st.columns(2)
        reorder = c4.number_input("Reorder level", min_value=0, value=0, step=1)
        price = c5.number_input("Unit price in Rs (optional)", min_value=0.0, value=0.0, step=0.01)
        st.caption("Optional: seed starting stock at a warehouse (uses stock adjustment).")
        c6, c7 = st.columns(2)
        seed_wh = c6.selectbox(
            "Seed warehouse",
            options=[""] + wh_ids,
            format_func=lambda x: "— none —" if x == "" else wh_labels.get(x, x),
        )
        seed_qty = c7.number_input("Seed quantity", min_value=0, value=0, step=1)
        submitted = st.form_submit_button("Create product", type="primary", use_container_width=True)
        if not submitted:
            return
        try:
            payload: dict[str, Any] = {
                "sku": sku,
                "name": name,
                "category": category,
                "reorder_level": int(reorder),
            }
            if price and price > 0:
                payload["unit_price"] = float(price)
            if seed_wh and int(seed_qty) > 0:
                payload["seed_warehouse_id"] = seed_wh
                payload["seed_quantity"] = int(seed_qty)
            db.create_product(payload)
            st.success(f"Created SKU {sku}")
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_edit_product_form(db: FirestoreManager, products: list[dict[str, Any]], *, show_heading: bool = True) -> None:
    if show_heading:
        _section_title("Edit / delete", level="h4")
    skus = [str(r.get("sku", "")) for r in products if r.get("sku")]
    pick = st.selectbox("Select SKU", options=[""] + skus, format_func=lambda x: x or "— choose —")
    if not pick:
        return
    current = db.get_product(pick)
    if not current:
        return
    row_qty = next((int(r.get("quantity", 0)) for r in products if str(r.get("sku")) == pick), 0)
    st.caption(f"Aggregated on-hand quantity (all warehouses): **{row_qty}** — adjust via Stock movement or transfers.")
    with st.form("edit_product"):
        e1, e2 = st.columns(2)
        n = e1.text_input("Name", value=current.get("name", ""))
        cat = e2.text_input("Category", value=current.get("category", ""))
        e4, e5 = st.columns(2)
        rv = e4.number_input(
            "Reorder level",
            min_value=0,
            value=int(current.get("reorder_level", 0)),
            step=1,
        )
        up = float(current.get("unit_price") or 0)
        pv = e5.number_input("Unit price (Rs)", min_value=0.0, value=up, step=0.01)
        u_btn, d_btn = st.columns(2)
        if u_btn.form_submit_button("Save changes", type="primary"):
            try:
                db.update_product(
                    pick,
                    {
                        "name": n,
                        "category": cat,
                        "reorder_level": int(rv),
                        "unit_price": pv if pv > 0 else None,
                    },
                )
                st.success("Updated.")
                st.rerun()
            except Exception as ex:
                st.error(str(ex))
        if d_btn.form_submit_button("Delete product"):
            try:
                db.delete_product(pick)
                st.success("Deleted.")
                st.rerun()
            except Exception as ex:
                st.error(str(ex))


def _render_stock_movement(db: FirestoreManager, *, show_heading: bool = True) -> None:
    if show_heading:
        _section_title("Stock movement (In / Out)", level="h4")
    wh_ids, wh_labels = _warehouse_choices(db)
    if not wh_ids:
        st.warning("Create a warehouse under **Warehouses** before recording movements.")
        return
    wid = st.selectbox(
        "Warehouse",
        options=wh_ids,
        format_func=lambda w: wh_labels.get(w, w),
        key="mv_wh",
    )
    st.caption("Per-warehouse stock. Uses apply_stock_adjustment for POS/sync compatibility.")

    st.text_input(
        "Search by SKU or product name",
        key="mv_filter",
        placeholder="Type to filter products (live search)…",
    )
    try:
        filter_q = (st.session_state.get("mv_filter") or "").strip()
        matches = db.search_products(filter_q)
    except Exception as err:
        st.exception(err)
        return

    if not matches:
        st.warning("No matching products. Add items under **Add product** or clear the search.")
        return

    labels = [f"{r.get('sku', '')} — {r.get('name', '')}" for r in matches]
    pick_label = st.selectbox(
        "Select product",
        options=labels,
        key="mv_product_select",
        help="Shows SKU and full product name.",
    )
    selected_sku = pick_label.split(" — ", 1)[0].strip() if pick_label else ""

    col_units, col_dir, col_note = st.columns([1, 1, 2])
    with col_units:
        units = st.number_input("Units", min_value=1, value=1, step=1, key="mv_units")
    with col_dir:
        dir_in_out = st.radio("Direction", ["IN", "OUT"], horizontal=True, key="mv_direction")
    with col_note:
        note_val = st.text_input("Note (optional)", key="mv_note")

    if st.button("Apply movement", key="apply_mv", type="primary", use_container_width=True):
        if not selected_sku:
            st.warning("Select a product from the list.")
        else:
            try:
                db.apply_stock_adjustment(
                    selected_sku,
                    int(units),
                    "IN" if dir_in_out == "IN" else "OUT",
                    warehouse_id=wid,
                    source="ui",
                    note=note_val or None,
                )
                st.success("Movement recorded.")
                st.rerun()
            except Exception as ex:
                st.error(str(ex))

    if st.button("Barcode / QR hook (demo)", key="barcode_demo"):
        FirestoreManager.barcode_scan_placeholder({"sku_hint": selected_sku, "format": "CODE128"})
        st.info("Hook invoked — wire hardware/SDK here.")


def render_warehouses(db: FirestoreManager) -> None:
    try:
        rows = db.list_warehouses(active_only=False)
    except Exception as err:
        st.exception(err)
        return
    with _panel():
        _section_title("Your warehouses", level="h4")
        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No warehouses yet — add one below.")

    with _panel():
        _section_title("Add warehouse", level="h4")
        with st.form("add_wh"):
            nm = st.text_input("Display name")
            sub = st.form_submit_button("Create warehouse", type="primary", use_container_width=True)
            if sub and nm.strip():
                try:
                    wid = db.create_warehouse(nm.strip(), active=True)
                    st.success(f"Created warehouse `{wid}`.")
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))


def render_suppliers(db: FirestoreManager) -> None:
    try:
        sups = db.list_suppliers()
    except Exception as err:
        st.exception(err)
        return
    with _panel():
        _section_title("Supplier directory", level="h4")
        if sups:
            st.dataframe(pd.DataFrame(sups), use_container_width=True, hide_index=True)
        else:
            st.info("No suppliers yet.")

    with _panel():
        _section_title("Add supplier", level="h4")
        with st.form("add_sup"):
            n = st.text_input("Name *")
            em = st.text_input("Email")
            ph = st.text_input("Phone")
            notes = st.text_area("Notes")
            sub = st.form_submit_button("Create supplier", type="primary", use_container_width=True)
            if sub:
                try:
                    sid = db.create_supplier({"name": n, "email": em, "phone": ph, "notes": notes})
                    st.success(f"Supplier `{sid}` created.")
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))

    if sups:
        with _panel():
            _section_title("Supplier ↔ SKU links", level="h4")
            sid_pick = st.selectbox(
                "Supplier",
                options=[s["id"] for s in sups],
                format_func=lambda i: next((s.get("name", i) for s in sups if s["id"] == i), i),
                key="sup_pick_items",
            )
            skus = [str(r.get("sku", "")) for r in db.list_products()]
            with st.form("sup_item"):
                sku_in = st.selectbox("Product SKU", options=[""] + skus)
                sup_sku = st.text_input("Supplier SKU (optional)")
                uc = st.number_input("Unit cost (optional)", min_value=0.0, value=0.0, step=0.01)
                if st.form_submit_button("Link product", type="primary"):
                    if not sku_in:
                        st.warning("Choose a SKU.")
                    else:
                        try:
                            db.upsert_supplier_item(
                                sid_pick,
                                sku_in,
                                supplier_sku=sup_sku,
                                unit_cost=float(uc) if uc > 0 else None,
                            )
                            st.success("Linked.")
                            st.rerun()
                        except Exception as ex:
                            st.error(str(ex))

            try:
                items = db.list_supplier_items(sid_pick)
            except Exception:
                items = []
            if items:
                st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)


def render_purchase_orders(db: FirestoreManager) -> None:
    sups = db.list_suppliers()
    wh_ids, wh_labels = _warehouse_choices(db)

    with _panel():
        _section_title("Create purchase order", level="h4")
        if not sups or not wh_ids:
            st.info("Create at least one supplier and one warehouse first.")
        else:
            with st.form("create_po"):
                sid = st.selectbox(
                    "Supplier",
                    options=[s["id"] for s in sups],
                    format_func=lambda i: next((s.get("name", i) for s in sups if s["id"] == i), i),
                )
                dst = st.selectbox(
                    "Destination warehouse",
                    options=wh_ids,
                    format_func=lambda w: wh_labels.get(w, w),
                )
                st.caption("Enter one or more SKU lines (leave unused rows empty).")
                lines: list[tuple[str, int]] = []
                for i in range(5):
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        sku_i = st.text_input(f"SKU {i + 1}", key=f"po_sku_{i}")
                    with c2:
                        q_i = st.number_input(f"Qty {i + 1}", min_value=0, value=0, step=1, key=f"po_q_{i}")
                    if sku_i.strip() and q_i > 0:
                        lines.append((sku_i.strip(), int(q_i)))
                stt = st.selectbox("Initial status", options=["draft", "ordered"], index=0)
                if st.form_submit_button("Create PO", type="primary", use_container_width=True):
                    try:
                        pid = db.create_purchase_order(sid, dst, lines, status=stt)  # type: ignore[arg-type]
                        st.success(f"Created PO `{pid}`.")
                        st.rerun()
                    except Exception as ex:
                        st.error(str(ex))

    with _panel():
        _section_title("Receive goods", level="h4")
        try:
            pos = db.list_purchase_orders(80)
        except Exception as err:
            st.exception(err)
            return
        if not pos:
            st.info("No purchase orders.")
            return

        po_labels = {p["id"]: f"{p['id'][:10]}… · {p.get('status', '')}" for p in pos}
        sel_po = st.selectbox("Select PO", options=[p["id"] for p in pos], format_func=lambda x: po_labels.get(x, x))
        detail = db.get_purchase_order(sel_po)
        if not detail:
            return

        m1, m2, m3 = st.columns(3)
        m1.metric("Status", str(detail.get("status", "—")))
        sid_disp = str(detail.get("supplier_id", "—"))
        m2.metric("Supplier", sid_disp[:16] + ("…" if len(sid_disp) > 16 else ""))
        dw_disp = str(detail.get("destination_warehouse_id", "—"))
        m3.metric("Destination WH", dw_disp[:16] + ("…" if len(dw_disp) > 16 else ""))
        st.caption(f"PO ID: `{detail.get('id', sel_po)}`")

        lines = detail.get("lines") or []
        if not lines:
            st.caption("No lines on this PO.")
            return
        df = pd.DataFrame(lines)
        st.dataframe(df, use_container_width=True, hide_index=True)

        open_lines = [
            ln
            for ln in lines
            if int(ln.get("qty_ordered", 0)) > int(ln.get("qty_received", 0))
            and detail.get("status") != "cancelled"
        ]
        if not open_lines:
            st.success("Nothing left to receive for this PO (or it is cancelled).")
            return

        with st.form("recv_po"):
            line_pick = st.selectbox(
                "Line",
                options=[str(ln["id"]) for ln in open_lines],
                format_func=lambda lid: next(
                    (
                        f"{ln.get('sku')} · remaining {int(ln.get('qty_ordered', 0)) - int(ln.get('qty_received', 0))}"
                        for ln in open_lines
                        if str(ln.get("id")) == lid
                    ),
                    lid,
                ),
            )
            rq = st.number_input("Receive qty", min_value=1, value=1, step=1)
            note = st.text_input("Note (optional)")
            if st.form_submit_button("Record receipt", type="primary", use_container_width=True):
                try:
                    db.receive_purchase_line(sel_po, line_pick, int(rq), note=note or None)
                    st.success("Received.")
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))


def render_transfers(db: FirestoreManager) -> None:
    wh_ids, wh_labels = _warehouse_choices(db)
    with _panel():
        _section_title("Create transfer", level="h4")
        if len(wh_ids) < 2:
            st.warning("Need at least two warehouses for transfers.")
        else:
            with st.form("create_tr"):
                fw = st.selectbox("From warehouse", options=wh_ids, format_func=lambda w: wh_labels.get(w, w))
                tw = st.selectbox(
                    "To warehouse",
                    options=wh_ids,
                    format_func=lambda w: wh_labels.get(w, w),
                    index=min(1, len(wh_ids) - 1),
                )
                lines: list[tuple[str, int]] = []
                for i in range(5):
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        sku_i = st.text_input(f"SKU {i + 1}", key=f"tr_sku_{i}")
                    with c2:
                        q_i = st.number_input(f"Qty {i + 1}", min_value=0, value=0, step=1, key=f"tr_q_{i}")
                    if sku_i.strip() and q_i > 0:
                        lines.append((sku_i.strip(), int(q_i)))
                if st.form_submit_button("Create draft transfer", type="primary", use_container_width=True):
                    if fw == tw:
                        st.error("Choose different warehouses.")
                    else:
                        try:
                            tid = db.create_stock_transfer(fw, tw, lines, status="draft")
                            st.success(f"Transfer `{tid}` created.")
                            st.rerun()
                        except Exception as ex:
                            st.error(str(ex))

    with _panel():
        _section_title("Transfer history", level="h4")
        try:
            trs = db.list_stock_transfers(60)
        except Exception as err:
            st.exception(err)
            return
        if not trs:
            st.info("No transfers.")
            return
        st.dataframe(pd.DataFrame(trs), use_container_width=True, hide_index=True)

        open_tr = [x for x in trs if str(x.get("status")) in ("draft", "in_transit")]
        if open_tr:
            tid = st.selectbox(
                "Complete transfer",
                options=[x["id"] for x in open_tr],
                format_func=lambda x: f"{x[:12]}… ({next((t.get('status') for t in open_tr if t['id'] == x), '')})",
            )
            if st.button("Complete selected transfer", type="primary", use_container_width=True):
                try:
                    db.complete_stock_transfer(tid)
                    st.success("Transfer completed.")
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))


def render_inventory(db: FirestoreManager) -> None:
    search = st.text_input(
        "Search inventory (SKU or name)",
        key="inv_search",
        placeholder="Filter catalog…",
    )
    try:
        products = db.search_products(search)
    except Exception as err:
        st.exception(err)
        return

    df = pd.DataFrame(products) if products else pd.DataFrame(
        columns=["sku", "name", "category", "quantity", "reorder_level", "unit_price"]
    )

    tab_cat, tab_add, tab_edit, tab_mv = st.tabs(["Catalog", "Add product", "Edit / delete", "Stock movement"])
    with tab_cat:
        with _panel():
            _section_title("Matching products", level="h4")
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "unit_price": st.column_config.NumberColumn(format="Rs %.2f"),
                },
            )
    with tab_add:
        with _panel():
            _render_add_product_form(db, show_heading=False)
    with tab_edit:
        with _panel():
            _render_edit_product_form(db, products, show_heading=False)
    with tab_mv:
        with _panel():
            _render_stock_movement(db, show_heading=False)


def render_activity(db: FirestoreManager) -> None:
    with _panel():
        _section_title("Recent stock movements", level="h4")
        try:
            rows = db.list_recent_activity(150)
        except Exception as err:
            st.exception(err)
            return

        if not rows:
            st.info("No activity yet.")
            return

        display = []
        for r in rows:
            ts = r.get("timestamp")
            ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if hasattr(ts, "strftime") else str(ts)
            display.append(
                {
                    "time": ts_str,
                    "sku": r.get("sku"),
                    "type": r.get("movement_type"),
                    "units": r.get("quantity_delta"),
                    "warehouse": r.get("warehouse_id", ""),
                    "event": r.get("event_type", ""),
                    "source": r.get("source"),
                    "note": r.get("note", ""),
                }
            )
        adf = pd.DataFrame(display)
        st.dataframe(adf, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
