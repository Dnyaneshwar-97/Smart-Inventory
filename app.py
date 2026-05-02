"""
Smart Inventory — Streamlit retail dashboard backed by Cloud Firestore.

Configure credentials via environment variables (see README); never commit secrets.
"""

from __future__ import annotations

import html
import json
import os
from typing import Any

import pandas as pd
import streamlit as st


def _apply_streamlit_secrets_to_env() -> None:
    """Inject Streamlit Cloud / `.streamlit/secrets.toml` into os.environ before `database` imports Firebase."""
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

    for key in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_CREDENTIALS_JSON"):
        if key in sec:
            put(key, sec[key])

    if "firebase_credentials" in sec:
        put("FIREBASE_CREDENTIALS_JSON", json.dumps(dict(sec["firebase_credentials"])))

    if "gcp" in sec:
        g = sec["gcp"]
        if "project_id" in g and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            put("GOOGLE_CLOUD_PROJECT", g["project_id"])
        if "credentials_json" in g:
            put("FIREBASE_CREDENTIALS_JSON", str(g["credentials_json"]))


_apply_streamlit_secrets_to_env()

import streamlit.components.v1 as components
from database import FirestoreManager, credential_debug_info


def _inject_global_theme() -> None:
    """Dark retail shell for Streamlit; Tailwind runs inside metric components (iframe)."""
    if st.session_state.get("_theme_loaded"):
        return
    st.markdown(
        """
<style>
  .stApp { background: linear-gradient(160deg, #0f172a 0%, #1e293b 55%, #0f172a 100%); }
  [data-testid="stHeader"] { background: transparent; }
  h1, h2, h3, label, span, p, .stMarkdown { color: #e2e8f0 !important; }
  [data-baseweb="tab"] { color: #94a3b8 !important; }
  [data-baseweb="tab"][aria-selected="true"] { color: #38bdf8 !important; }
</style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state["_theme_loaded"] = True


def _metric_cards_tailwind(total_value: float, low_count: int) -> None:
    """Tailwind CDN + cards inside an iframe (main Streamlit DOM strips script tags)."""
    safe_val = html.escape(f"{total_value:,.2f}")
    safe_low = html.escape(str(low_count))
    components.html(
        f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-transparent m-0 p-0">
<div class="grid grid-cols-1 md:grid-cols-2 gap-4 font-sans">
  <div class="rounded-2xl bg-gradient-to-br from-slate-800 to-slate-900 border border-slate-600/80 p-6 shadow-xl">
    <p class="text-sm uppercase tracking-wider text-slate-400 mb-1">Total stock value</p>
    <p class="text-3xl font-semibold text-sky-400">Rs {safe_val}</p>
    <p class="text-xs text-slate-500 mt-2">Σ (quantity × unit price)</p>
  </div>
  <div class="rounded-2xl bg-gradient-to-br from-slate-800 to-slate-900 border border-slate-600/80 p-6 shadow-xl">
    <p class="text-sm uppercase tracking-wider text-slate-400 mb-1">Low stock SKUs</p>
    <p class="text-3xl font-semibold text-amber-400">{safe_low}</p>
    <p class="text-xs text-slate-500 mt-2">quantity ≤ reorder level</p>
  </div>
</div>
</body></html>
        """,
        height=220,
        scrolling=False,
    )


def _section_title(text: str) -> None:
    st.markdown(
        f'<h2 style="font-size:1.25rem;font-weight:600;color:#f1f5f9;border-bottom:1px solid #334155;padding-bottom:0.5rem;margin-bottom:1rem;">{html.escape(text)}</h2>',
        unsafe_allow_html=True,
    )


@st.cache_resource
def get_firestore_manager(*, _schema_version: str = "2026-05-streamlit-secrets") -> FirestoreManager:
    """`_schema_version` invalidates Streamlit's cache when FirestoreManager API changes."""
    _ = _schema_version
    return FirestoreManager()


def _low_stock_products(db: FirestoreManager) -> list[dict[str, Any]]:
    """Uses FirestoreManager.list_low_stock_products when present; otherwise filters client-side."""
    getter = getattr(db, "list_low_stock_products", None)
    if callable(getter):
        return getter()
    rows = [
        r
        for r in db.list_products()
        if int(r.get("quantity", 0)) <= int(r.get("reorder_level", 0))
    ]
    rows.sort(key=lambda r: (str(r.get("name", "")), str(r.get("sku", ""))))
    return rows


def main() -> None:
    st.set_page_config(
        page_title="Smart Inventory",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _inject_global_theme()

    st.markdown(
        """
<h1 style="font-size:2rem;font-weight:700;color:#fff;margin-bottom:0.25rem;">Retail Dashboard</h1>
<p style="color:#94a3b8;margin-bottom:1.5rem;">Smart Inventory · Streamlit + Firestore</p>
        """,
        unsafe_allow_html=True,
    )

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

    auth_stub = FirestoreManager.ensure_authenticated_user()
    if auth_stub is not None:
        st.sidebar.caption(f"Auth hook: {auth_stub}")

    jump_sku = st.session_state.pop("pending_sku_jump", None)
    if jump_sku:
        st.session_state["inv_search"] = jump_sku
        st.session_state["main_nav"] = "Inventory"

    section = st.radio(
        "Section",
        ["Dashboard", "Inventory", "Activity log"],
        horizontal=True,
        label_visibility="collapsed",
        key="main_nav",
    )

    if section == "Dashboard":
        render_dashboard(db)
    elif section == "Inventory":
        render_inventory(db)
    else:
        render_activity(db)


def render_dashboard(db: FirestoreManager) -> None:
    _section_title("Live metrics")
    try:
        m = db.get_dashboard_metrics()
    except Exception as err:
        st.error("Failed to load metrics.")
        st.exception(err)
        return

    _metric_cards_tailwind(float(m["total_stock_value"]), int(m["low_stock_count"]))

    low_cnt = int(m["low_stock_count"])
    if low_cnt > 0:
        try:
            low_rows = _low_stock_products(db)
        except Exception as err:
            st.exception(err)
            low_rows = []
        if low_rows:
            st.markdown(
                '<p style="color:#94a3b8;font-size:0.9rem;margin:0.75rem 0 0.25rem 0;">'
                "Low stock — click a SKU to jump to <strong>Inventory</strong> with that item:"
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
                            key=f"jump_low_{sku}",
                            help=name or sku,
                            use_container_width=True,
                        ):
                            st.session_state["pending_sku_jump"] = sku
                            st.rerun()

    _section_title("Quick search")
    q = st.text_input("Filter by SKU or name", key="dash_search", placeholder="Type to filter…")
    try:
        rows = db.search_products(q)
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


def _render_add_product_form(db: FirestoreManager) -> None:
    _section_title("Add product")
    with st.form("add_product", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        sku = c1.text_input("SKU *")
        name = c2.text_input("Name *")
        category = c3.text_input("Category")
        c4, c5, c6 = st.columns(3)
        qty = c4.number_input("Quantity", min_value=0, value=0, step=1)
        reorder = c5.number_input("Reorder level", min_value=0, value=0, step=1)
        price = c6.number_input("Unit price in Rs (optional)", min_value=0.0, value=0.0, step=0.01)
        submitted = st.form_submit_button("Create product")
        if not submitted:
            return
        try:
            payload: dict[str, Any] = {
                "sku": sku,
                "name": name,
                "category": category,
                "quantity": int(qty),
                "reorder_level": int(reorder),
            }
            if price and price > 0:
                payload["unit_price"] = float(price)
            db.create_product(payload)
            st.success(f"Created SKU {sku}")
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_edit_product_form(db: FirestoreManager, products: list[dict[str, Any]]) -> None:
    _section_title("Edit / delete")
    skus = [str(r.get("sku", "")) for r in products if r.get("sku")]
    pick = st.selectbox("Select SKU", options=[""] + skus, format_func=lambda x: x or "— choose —")
    if not pick:
        return
    current = db.get_product(pick)
    if not current:
        return
    with st.form("edit_product"):
        e1, e2 = st.columns(2)
        n = e1.text_input("Name", value=current.get("name", ""))
        cat = e2.text_input("Category", value=current.get("category", ""))
        e3, e4, e5 = st.columns(3)
        qv = e3.number_input(
            "Quantity",
            min_value=0,
            value=int(current.get("quantity", 0)),
            step=1,
        )
        rv = e4.number_input(
            "Reorder level",
            min_value=0,
            value=int(current.get("reorder_level", 0)),
            step=1,
        )
        up = float(current.get("unit_price") or 0)
        pv = e5.number_input("Unit price (Rs)", min_value=0.0, value=up, step=0.01)
        u_btn, d_btn = st.columns(2)
        if u_btn.form_submit_button("Save changes"):
            try:
                db.update_product(
                    pick,
                    {
                        "name": n,
                        "category": cat,
                        "quantity": int(qv),
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


def _render_stock_movement(db: FirestoreManager) -> None:
    _section_title("Stock movement (In / Out)")
    st.caption("Uses apply_stock_adjustment — same API as future POS / sync modules.")
    st.caption(
        "Use **Search** to narrow the list, then **Select product**—pick by name; the SKU is filled automatically."
    )

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
        help="Shows SKU and full product name. Pick one for IN/OUT—no need to type the SKU.",
    )
    selected_sku = pick_label.split(" — ", 1)[0].strip() if pick_label else ""

    col_units, col_dir, col_note = st.columns([1, 1, 2])
    with col_units:
        units = st.number_input("Units", min_value=1, value=1, step=1, key="mv_units")
    with col_dir:
        dir_in_out = st.radio("Direction", ["IN", "OUT"], horizontal=True, key="mv_direction")
    with col_note:
        note_val = st.text_input("Note (optional)", key="mv_note")

    if st.button("Apply movement", key="apply_mv"):
        if not selected_sku:
            st.warning("Select a product from the list.")
        else:
            try:
                db.apply_stock_adjustment(
                    selected_sku,
                    int(units),
                    "IN" if dir_in_out == "IN" else "OUT",
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


def render_inventory(db: FirestoreManager) -> None:
    search = st.text_input("Search inventory (SKU or name)", key="inv_search")
    try:
        products = db.search_products(search)
    except Exception as err:
        st.exception(err)
        return

    df = pd.DataFrame(products) if products else pd.DataFrame(
        columns=["sku", "name", "category", "quantity", "reorder_level", "unit_price"]
    )

    _section_title("Catalog")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "unit_price": st.column_config.NumberColumn(format="Rs %.2f"),
        },
    )

    _render_add_product_form(db)
    _render_edit_product_form(db, products)
    _render_stock_movement(db)


def render_activity(db: FirestoreManager) -> None:
    _section_title("Recent stock movements")
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
                "source": r.get("source"),
                "note": r.get("note", ""),
            }
        )
    adf = pd.DataFrame(display)
    st.dataframe(adf, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
