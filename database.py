"""
Firestore persistence for Smart Inventory.

Credentials (no secrets in code):
  - GOOGLE_APPLICATION_CREDENTIALS: path to service account JSON
  - FIREBASE_CREDENTIALS_JSON: raw JSON string (e.g. Secret Manager on Cloud Run)
  - GOOGLE_CLOUD_PROJECT / GCLOUD_PROJECT: optional explicit project ID

POS/accounting modules should use FirestoreManager public methods only so schema stays consistent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Always load `.env` from the package directory — Streamlit's cwd may not be the project root.
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

from abc import ABC, abstractmethod
from typing import Any, Literal, Mapping, Protocol, TypedDict, runtime_checkable

import firebase_admin
import firebase_admin.firestore as fs
from firebase_admin import credentials
from google.cloud.firestore import SERVER_TIMESTAMP

COLLECTION_INVENTORY = "inventory"
COLLECTION_ACTIVITY_LOG = "activity_log"
COLLECTION_WAREHOUSES = "warehouses"
COLLECTION_SUPPLIERS = "suppliers"
COLLECTION_PURCHASE_ORDERS = "purchase_orders"
COLLECTION_STOCK_TRANSFERS = "stock_transfers"

SUB_STOCK = "stock"
SUB_LINES = "lines"
SUB_ITEMS = "items"

MovementType = Literal["IN", "OUT"]
MovementSource = Literal["ui", "pos", "sync", "api"]
PoStatus = Literal["draft", "ordered", "partially_received", "received", "cancelled"]
TransferStatus = Literal["draft", "in_transit", "completed", "cancelled"]
ActivityEventType = Literal["movement", "receipt", "transfer", "adjustment"]


class InventoryProduct(TypedDict, total=False):
    """Product master: SKU is document ID. On-hand qty lives under warehouses/{id}/stock/{sku}."""

    name: str
    category: str
    reorder_level: int
    unit_price: float
    quantity: int  # legacy only; removed after migration


class ActivityEntry(TypedDict, total=False):
    sku: str
    movement_type: MovementType
    quantity_delta: int
    timestamp: Any
    note: str
    source: MovementSource
    warehouse_id: str
    warehouse_ids: list[str]
    event_type: ActivityEventType
    ref_po_id: str
    ref_transfer_id: str


def _require_positive_int(name: str, value: int) -> None:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _normalize_sku(sku: str) -> str:
    s = (sku or "").strip()
    if not s:
        raise ValueError("SKU is required")
    return s


def _resolve_credentials_path(raw: str | None) -> str | None:
    """Expand ~ and resolve relative paths against the project root (next to this file)."""
    if not raw:
        return None
    path = Path(raw.strip().strip('"').strip("'")).expanduser()
    if not path.is_absolute():
        path = (_ROOT / path).resolve()
    return str(path)


def credential_debug_info() -> dict[str, str | bool]:
    """Safe diagnostics for Streamlit (no secret values)."""
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    resolved = _resolve_credentials_path(raw)
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT") or ""
    env_file = _ROOT / ".env"
    return {
        "env_file": str(env_file),
        "env_file_exists": env_file.is_file(),
        "project_id_configured": bool(proj.strip()),
        "credentials_env_set": bool(raw and str(raw).strip()),
        "credentials_file_exists": Path(resolved).is_file() if resolved else False,
        "resolved_credentials_path": resolved or "(not set)",
    }


def _project_id_from_service_account(obj: Mapping[str, Any]) -> str | None:
    pid = obj.get("project_id")
    return str(pid).strip() if pid else None


def _init_firebase_app() -> None:
    if firebase_admin._apps:
        return

    env_project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT") or "").strip()

    json_blob = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if json_blob:
        data = json.loads(json_blob)
        cred = credentials.Certificate(data)
        project_id = env_project or _project_id_from_service_account(data)
        if not project_id:
            raise ValueError(
                "Set GOOGLE_CLOUD_PROJECT in Streamlit secrets (or .env), or use service account JSON that includes project_id."
            )
        firebase_admin.initialize_app(cred, {"projectId": project_id})
        return

    cred_path = _resolve_credentials_path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    if cred_path:
        if not Path(cred_path).is_file():
            raise FileNotFoundError(
                f"Service account file not found: {cred_path}. "
                "Check GOOGLE_APPLICATION_CREDENTIALS in .env (use an absolute path)."
            )
        cred = credentials.Certificate(cred_path)
        if env_project:
            project_id = env_project
        else:
            with Path(cred_path).open(encoding="utf-8") as f:
                file_data = json.load(f)
            project_id = _project_id_from_service_account(file_data)
        if not project_id:
            raise ValueError("Could not determine project ID from credentials file or GOOGLE_CLOUD_PROJECT.")
        firebase_admin.initialize_app(cred, {"projectId": project_id})
        return

    cred = credentials.ApplicationDefault()
    project_id = env_project
    if not project_id:
        raise ValueError(
            "Project ID is required (set GOOGLE_CLOUD_PROJECT in Streamlit secrets or environment)."
        )
    firebase_admin.initialize_app(cred, {"projectId": project_id})


@runtime_checkable
class InventoryWriter(Protocol):
    """Hook for future POS / accounting sync — implement using FirestoreManager."""

    def apply_stock_adjustment(
        self,
        sku: str,
        quantity_delta: int,
        movement_type: MovementType,
        *,
        warehouse_id: str,
        source: MovementSource = "sync",
        note: str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> None: ...


class InventoryRepository(ABC):
    """Abstract base for alternate backends; keeps sync modules decoupled from UI."""

    @abstractmethod
    def create_product(self, product: Mapping[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    def apply_stock_adjustment(
        self,
        sku: str,
        quantity_delta: int,
        movement_type: MovementType,
        *,
        warehouse_id: str,
        source: MovementSource = "sync",
        note: str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError


class FirestoreManager(InventoryRepository):
    """CRUD for inventory master, per-warehouse stock, suppliers, POs, transfers, and activity_log."""

    def __init__(self) -> None:
        _init_firebase_app()
        self._db = fs.client()

    # --- Firebase Auth placeholder (Streamlit session integration later) ---
    @staticmethod
    def ensure_authenticated_user() -> dict[str, Any] | None:
        """
        Placeholder for Firebase Authentication.
        Returns None until wired to ID tokens / Streamlit-Authenticator / custom middleware.
        """
        return None

    @staticmethod
    def barcode_scan_placeholder(payload: Mapping[str, Any]) -> None:
        """
        Placeholder for hardware / camera barcode or QR scanning.
        Route decoded SKU into search or apply_stock_adjustment when implemented.
        """
        _ = payload

    def _warehouse_stock_ref(self, warehouse_id: str, sku: str) -> fs.DocumentReference:
        wid = (warehouse_id or "").strip()
        if not wid:
            raise ValueError("warehouse_id is required")
        return (
            self._db.collection(COLLECTION_WAREHOUSES)
            .document(wid)
            .collection(SUB_STOCK)
            .document(_normalize_sku(sku))
        )

    def has_legacy_flat_quantity(self) -> bool:
        """True if any product document still stores top-level legacy `quantity`."""
        for doc in self._db.collection(COLLECTION_INVENTORY).limit(50).stream():
            data = doc.to_dict() or {}
            if "quantity" in data:
                return True
        return False

    def list_warehouses(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        q = self._db.collection(COLLECTION_WAREHOUSES).order_by("name")
        rows: list[dict[str, Any]] = []
        for doc in q.stream():
            d = dict(doc.to_dict() or {})
            d["id"] = doc.id
            if active_only and not d.get("active", True):
                continue
            rows.append(d)
        return rows

    def create_warehouse(self, name: str, *, active: bool = True) -> str:
        nm = (name or "").strip()
        if not nm:
            raise ValueError("Warehouse name is required")
        ref = self._db.collection(COLLECTION_WAREHOUSES).document()
        ref.set({"name": nm, "active": active, "created_at": SERVER_TIMESTAMP})
        return ref.id

    def update_warehouse(self, warehouse_id: str, updates: Mapping[str, Any]) -> None:
        wid = (warehouse_id or "").strip()
        if not wid:
            raise ValueError("warehouse_id is required")
        ref = self._db.collection(COLLECTION_WAREHOUSES).document(wid)
        if not ref.get().exists:
            raise ValueError(f"Unknown warehouse: {wid}")
        patch: dict[str, Any] = {}
        if "name" in updates:
            patch["name"] = str(updates["name"]).strip()
        if "active" in updates:
            patch["active"] = bool(updates["active"])
        if patch:
            ref.update(patch)

    def get_stock_quantity(self, warehouse_id: str, sku: str) -> int:
        snap = self._warehouse_stock_ref(warehouse_id, sku).get()
        if not snap.exists:
            return 0
        return int((snap.to_dict() or {}).get("quantity", 0))

    def _effective_reorder(self, product: Mapping[str, Any], stock_row: Mapping[str, Any] | None) -> int:
        if stock_row and stock_row.get("reorder_level") is not None:
            return int(stock_row["reorder_level"])
        return int(product.get("reorder_level", 0))

    def migrate_legacy_inventory_to_default_warehouse(self, default_name: str = "Main") -> dict[str, Any]:
        """
        Create a default warehouse and move legacy `inventory.quantity` into warehouses/{id}/stock/{sku}.
        Removes `quantity` from product documents. Idempotent if no legacy quantities remain.
        """
        created_id: str | None = None
        warehouses = self.list_warehouses(active_only=False)
        if not warehouses:
            created_id = self.create_warehouse(default_name, active=True)
            target_wid = created_id
        else:
            target_wid = warehouses[0]["id"]

        moved = 0
        cleared = 0
        batch = self._db.batch()
        batch_count = 0

        for doc in self._db.collection(COLLECTION_INVENTORY).stream():
            data = dict(doc.to_dict() or {})
            if "quantity" not in data:
                continue
            sku = doc.id
            qty = int(data.get("quantity", 0))
            stock_ref = self._warehouse_stock_ref(target_wid, sku)
            prev = stock_ref.get()
            prev_qty = int((prev.to_dict() or {}).get("quantity", 0)) if prev.exists else 0
            batch.set(
                stock_ref,
                {"quantity": prev_qty + qty, "updated_at": SERVER_TIMESTAMP},
                merge=True,
            )
            inv_ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
            batch.update(inv_ref, {"quantity": fs.DELETE_FIELD})
            batch_count += 2
            moved += 1
            cleared += 1
            if batch_count >= 450:
                batch.commit()
                batch = self._db.batch()
                batch_count = 0

        if batch_count:
            batch.commit()

        return {
            "warehouse_id": target_wid,
            "warehouse_created": created_id,
            "products_moved": moved,
            "legacy_quantity_cleared": cleared,
        }

    def create_product(self, product: Mapping[str, Any]) -> str:
        sku = _normalize_sku(str(product.get("sku", "")))
        name = str(product.get("name", "")).strip()
        if not name:
            raise ValueError("Name is required")

        category = str(product.get("category", "")).strip()
        reorder_level = int(product.get("reorder_level", 0))
        _require_positive_int("reorder_level", reorder_level)

        unit_price = product.get("unit_price")
        data: dict[str, Any] = {
            "name": name,
            "category": category,
            "reorder_level": reorder_level,
        }
        if unit_price is not None:
            data["unit_price"] = float(unit_price)

        ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
        snap = ref.get()
        if snap.exists:
            raise ValueError(f"Product with SKU '{sku}' already exists")

        ref.set(data)

        seed_wh = str(product.get("seed_warehouse_id", "") or "").strip()
        seed_qty = int(product.get("seed_quantity", 0) or 0)
        if seed_wh and seed_qty > 0:
            self.apply_stock_adjustment(
                sku,
                seed_qty,
                "IN",
                warehouse_id=seed_wh,
                source="ui",
                note="Initial stock",
                extra_fields={"event_type": "adjustment"},
            )

        return sku

    def get_product(self, sku: str) -> dict[str, Any] | None:
        sku = _normalize_sku(sku)
        snap = self._db.collection(COLLECTION_INVENTORY).document(sku).get()
        if not snap.exists:
            return None
        out = dict(snap.to_dict() or {})
        out["sku"] = sku
        return out

    def update_product(self, sku: str, updates: Mapping[str, Any]) -> None:
        sku = _normalize_sku(sku)
        ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
        if not ref.get().exists:
            raise ValueError(f"Unknown SKU: {sku}")

        patch: dict[str, Any] = {}
        if "name" in updates:
            patch["name"] = str(updates["name"]).strip()
        if "category" in updates:
            patch["category"] = str(updates["category"]).strip()
        if "reorder_level" in updates:
            r = int(updates["reorder_level"])
            _require_positive_int("reorder_level", r)
            patch["reorder_level"] = r
        if "unit_price" in updates and updates["unit_price"] is not None:
            patch["unit_price"] = float(updates["unit_price"])

        if not patch:
            return
        ref.update(patch)

    def delete_product(self, sku: str) -> None:
        sku = _normalize_sku(sku)
        ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
        if not ref.get().exists:
            raise ValueError(f"Unknown SKU: {sku}")

        batch = self._db.batch()
        for wh in self.list_warehouses(active_only=False):
            sref = self._warehouse_stock_ref(str(wh["id"]), sku)
            if sref.get().exists:
                batch.delete(sref)
        batch.delete(ref)
        batch.commit()

    def _aggregate_sku_quantities(self) -> dict[str, int]:
        """Sum qty per SKU across all warehouses; includes legacy flat quantity if present."""
        totals: dict[str, int] = {}
        for inv_doc in self._db.collection(COLLECTION_INVENTORY).stream():
            sku = inv_doc.id
            data = inv_doc.to_dict() or {}
            if "quantity" in data:
                totals[sku] = totals.get(sku, 0) + int(data.get("quantity", 0))

        for wh in self.list_warehouses(active_only=False):
            wid = str(wh["id"])
            sub = (
                self._db.collection(COLLECTION_WAREHOUSES)
                .document(wid)
                .collection(SUB_STOCK)
            )
            for sdoc in sub.stream():
                sku = sdoc.id
                q = int((sdoc.to_dict() or {}).get("quantity", 0))
                totals[sku] = totals.get(sku, 0) + q
        return totals

    def _quantity_for_display(self, sku: str, warehouse_id: str | None, totals_cache: dict[str, int] | None) -> int:
        if totals_cache is not None and warehouse_id is None:
            return int(totals_cache.get(sku, 0))

        if warehouse_id:
            return self.get_stock_quantity(warehouse_id, sku)

        # All warehouses: sum from stock docs only if no legacy (migration done path)
        if not self.has_legacy_flat_quantity():
            t = 0
            for wh in self.list_warehouses(active_only=False):
                t += self.get_stock_quantity(str(wh["id"]), sku)
            return t

        inv = self._db.collection(COLLECTION_INVENTORY).document(sku).get()
        data = inv.to_dict() or {} if inv.exists else {}
        legacy = int(data.get("quantity", 0)) if "quantity" in data else 0
        t = legacy
        for wh in self.list_warehouses(active_only=False):
            t += self.get_stock_quantity(str(wh["id"]), sku)
        return t

    def list_products(
        self,
        limit: int | None = None,
        *,
        warehouse_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = self._db.collection(COLLECTION_INVENTORY).order_by("name")
        if limit is not None:
            query = query.limit(limit)
        totals_cache = self._aggregate_sku_quantities() if warehouse_id is None else None
        rows: list[dict[str, Any]] = []
        for doc in query.stream():
            d = dict(doc.to_dict() or {})
            d["sku"] = doc.id
            d["quantity"] = self._quantity_for_display(doc.id, warehouse_id, totals_cache)
            rows.append(d)
        return rows

    def search_products(self, query: str, *, warehouse_id: str | None = None) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return self.list_products(warehouse_id=warehouse_id)
        all_rows = self.list_products(warehouse_id=warehouse_id)
        return [
            row
            for row in all_rows
            if q in str(row.get("sku", "")).lower() or q in str(row.get("name", "")).lower()
        ]

    def get_dashboard_metrics(self, warehouse_id: str | None = None) -> dict[str, float | int]:
        total_value = 0.0
        low_stock = 0
        totals_cache = self._aggregate_sku_quantities() if warehouse_id is None else None

        for doc in self._db.collection(COLLECTION_INVENTORY).stream():
            sku = doc.id
            data = doc.to_dict() or {}
            price = float(data.get("unit_price", 0) or 0)
            reorder = int(data.get("reorder_level", 0))

            if warehouse_id:
                qty = self.get_stock_quantity(warehouse_id, sku)
                st_snap = self._warehouse_stock_ref(warehouse_id, sku).get()
                st_data = st_snap.to_dict() if st_snap.exists else None
                eff_reorder = self._effective_reorder(data, st_data)
            else:
                qty = int(totals_cache.get(sku, 0)) if totals_cache is not None else 0
                eff_reorder = reorder

            total_value += qty * price
            if qty <= eff_reorder:
                low_stock += 1

        return {"total_stock_value": round(total_value, 2), "low_stock_count": low_stock}

    def list_low_stock_products(self, warehouse_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        totals_cache = self._aggregate_sku_quantities() if warehouse_id is None else None

        for doc in self._db.collection(COLLECTION_INVENTORY).stream():
            sku = doc.id
            data = dict(doc.to_dict() or {})
            if warehouse_id:
                qty = self.get_stock_quantity(warehouse_id, sku)
                st_snap = self._warehouse_stock_ref(warehouse_id, sku).get()
                st_data = st_snap.to_dict() if st_snap.exists else None
                eff_reorder = self._effective_reorder(data, st_data)
            else:
                qty = int(totals_cache.get(sku, 0)) if totals_cache is not None else 0
                eff_reorder = int(data.get("reorder_level", 0))

            if qty <= eff_reorder:
                data["sku"] = sku
                data["quantity"] = qty
                rows.append(data)

        rows.sort(key=lambda r: (str(r.get("name", "")), str(r.get("sku", ""))))
        return rows

    def log_movement(
        self,
        sku: str,
        movement_type: MovementType,
        quantity_delta: int,
        *,
        source: MovementSource = "ui",
        note: str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> None:
        sku = _normalize_sku(sku)
        _require_positive_int("quantity_delta", quantity_delta)
        entry: dict[str, Any] = {
            "sku": sku,
            "movement_type": movement_type,
            "quantity_delta": quantity_delta,
            "timestamp": SERVER_TIMESTAMP,
            "source": source,
        }
        if note:
            entry["note"] = note
        if extra_fields:
            for k, v in extra_fields.items():
                if k not in entry:
                    entry[k] = v
        self._db.collection(COLLECTION_ACTIVITY_LOG).add(entry)

    def list_recent_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        q = (
            self._db.collection(COLLECTION_ACTIVITY_LOG)
            .order_by("timestamp", direction=fs.Query.DESCENDING)
            .limit(limit)
        )
        rows: list[dict[str, Any]] = []
        for doc in q.stream():
            d = dict(doc.to_dict() or {})
            d["id"] = doc.id
            rows.append(d)
        return rows

    def apply_stock_adjustment(
        self,
        sku: str,
        quantity_delta: int,
        movement_type: MovementType,
        *,
        warehouse_id: str,
        source: MovementSource = "ui",
        note: str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Updates per-warehouse stock and appends activity_log."""
        sku = _normalize_sku(sku)
        wid = (warehouse_id or "").strip()
        if not wid:
            raise ValueError("warehouse_id is required")
        _require_positive_int("quantity_delta", quantity_delta)

        wh_ref = self._db.collection(COLLECTION_WAREHOUSES).document(wid)
        if not wh_ref.get().exists:
            raise ValueError(f"Unknown warehouse: {wid}")

        inv_ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
        stock_ref = self._warehouse_stock_ref(wid, sku)

        @fs.transactional
        def _adjust(transaction: fs.Transaction) -> None:
            inv_snap = inv_ref.get(transaction=transaction)
            if not inv_snap.exists:
                raise ValueError(f"Unknown SKU: {sku}")

            st_snap = stock_ref.get(transaction=transaction)
            current = int((st_snap.to_dict() or {}).get("quantity", 0)) if st_snap.exists else 0
            inv_data = inv_snap.to_dict() or {}
            if "quantity" in inv_data:
                current += int(inv_data.get("quantity", 0))

            if movement_type == "IN":
                new_qty = current + quantity_delta
            else:
                new_qty = current - quantity_delta
            if new_qty < 0:
                raise ValueError("Resulting quantity cannot be negative")

            if st_snap.exists:
                transaction.update(stock_ref, {"quantity": new_qty, "updated_at": SERVER_TIMESTAMP})
            else:
                transaction.set(stock_ref, {"quantity": new_qty, "updated_at": SERVER_TIMESTAMP})

            if "quantity" in inv_data:
                transaction.update(inv_ref, {"quantity": fs.DELETE_FIELD})

        _adjust(self._db.transaction())

        merged_extra: dict[str, Any] = {
            "warehouse_id": wid,
            "event_type": "movement",
        }
        if extra_fields:
            merged_extra.update(dict(extra_fields))

        self.log_movement(
            sku,
            movement_type,
            quantity_delta,
            source=source,
            note=note,
            extra_fields=merged_extra,
        )

    # --- Suppliers ---

    def create_supplier(self, payload: Mapping[str, Any]) -> str:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Supplier name is required")
        ref = self._db.collection(COLLECTION_SUPPLIERS).document()
        data: dict[str, Any] = {
            "name": name,
            "created_at": SERVER_TIMESTAMP,
        }
        for key in ("email", "phone", "notes"):
            if key in payload and payload[key]:
                data[key] = str(payload[key]).strip()
        ref.set(data)
        return ref.id

    def list_suppliers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for doc in self._db.collection(COLLECTION_SUPPLIERS).order_by("name").stream():
            d = dict(doc.to_dict() or {})
            d["id"] = doc.id
            rows.append(d)
        return rows

    def update_supplier(self, supplier_id: str, updates: Mapping[str, Any]) -> None:
        sid = (supplier_id or "").strip()
        ref = self._db.collection(COLLECTION_SUPPLIERS).document(sid)
        if not ref.get().exists:
            raise ValueError(f"Unknown supplier: {sid}")
        patch: dict[str, Any] = {}
        if "name" in updates:
            patch["name"] = str(updates["name"]).strip()
        for key in ("email", "phone", "notes"):
            if key in updates:
                patch[key] = str(updates[key]).strip() if updates[key] else ""
        if patch:
            ref.update(patch)

    def delete_supplier(self, supplier_id: str) -> None:
        sid = (supplier_id or "").strip()
        ref = self._db.collection(COLLECTION_SUPPLIERS).document(sid)
        if not ref.get().exists:
            raise ValueError(f"Unknown supplier: {sid}")
        for item in ref.collection(SUB_ITEMS).stream():
            item.reference.delete()
        ref.delete()

    def upsert_supplier_item(self, supplier_id: str, sku: str, *, supplier_sku: str = "", unit_cost: float | None = None) -> None:
        sid = (supplier_id or "").strip()
        sku = _normalize_sku(sku)
        sref = self._db.collection(COLLECTION_SUPPLIERS).document(sid)
        if not sref.get().exists:
            raise ValueError(f"Unknown supplier: {sid}")
        if not self._db.collection(COLLECTION_INVENTORY).document(sku).get().exists:
            raise ValueError(f"Unknown SKU: {sku}")
        iref = sref.collection(SUB_ITEMS).document(sku)
        data: dict[str, Any] = {}
        if supplier_sku:
            data["supplier_sku"] = supplier_sku.strip()
        if unit_cost is not None:
            data["unit_cost"] = float(unit_cost)
        if data:
            iref.set(data, merge=True)
        else:
            iref.set({"sku": sku}, merge=True)

    def list_supplier_items(self, supplier_id: str) -> list[dict[str, Any]]:
        sid = (supplier_id or "").strip()
        sref = self._db.collection(COLLECTION_SUPPLIERS).document(sid)
        if not sref.get().exists:
            raise ValueError(f"Unknown supplier: {sid}")
        rows: list[dict[str, Any]] = []
        for doc in sref.collection(SUB_ITEMS).stream():
            d = dict(doc.to_dict() or {})
            d["sku"] = doc.id
            rows.append(d)
        rows.sort(key=lambda r: str(r.get("sku", "")))
        return rows

    # --- Purchase orders ---

    def create_purchase_order(
        self,
        supplier_id: str,
        destination_warehouse_id: str,
        lines: list[tuple[str, int]],
        *,
        status: PoStatus = "draft",
    ) -> str:
        sid = (supplier_id or "").strip()
        wid = (destination_warehouse_id or "").strip()
        if not self._db.collection(COLLECTION_SUPPLIERS).document(sid).get().exists:
            raise ValueError(f"Unknown supplier: {sid}")
        if not self._db.collection(COLLECTION_WAREHOUSES).document(wid).get().exists:
            raise ValueError(f"Unknown warehouse: {wid}")
        if not lines:
            raise ValueError("At least one line is required")

        pref = self._db.collection(COLLECTION_PURCHASE_ORDERS).document()
        pref.set(
            {
                "supplier_id": sid,
                "destination_warehouse_id": wid,
                "status": status,
                "created_at": SERVER_TIMESTAMP,
                "updated_at": SERVER_TIMESTAMP,
            }
        )
        batch = self._db.batch()
        for sku_raw, qty in lines:
            sku = _normalize_sku(sku_raw)
            if qty <= 0:
                raise ValueError("Line quantities must be positive")
            if not self._db.collection(COLLECTION_INVENTORY).document(sku).get().exists:
                raise ValueError(f"Unknown SKU: {sku}")
            lref = pref.collection(SUB_LINES).document()
            batch.set(
                lref,
                {"sku": sku, "qty_ordered": int(qty), "qty_received": 0},
            )
        batch.commit()
        return pref.id

    def list_purchase_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        q = (
            self._db.collection(COLLECTION_PURCHASE_ORDERS)
            .order_by("created_at", direction=fs.Query.DESCENDING)
            .limit(limit)
        )
        rows: list[dict[str, Any]] = []
        for doc in q.stream():
            d = dict(doc.to_dict() or {})
            d["id"] = doc.id
            rows.append(d)
        return rows

    def get_purchase_order(self, po_id: str) -> dict[str, Any] | None:
        pid = (po_id or "").strip()
        pref = self._db.collection(COLLECTION_PURCHASE_ORDERS).document(pid)
        snap = pref.get()
        if not snap.exists:
            return None
        out = dict(snap.to_dict() or {})
        out["id"] = pid
        lines: list[dict[str, Any]] = []
        for ldoc in pref.collection(SUB_LINES).stream():
            ld = dict(ldoc.to_dict() or {})
            ld["id"] = ldoc.id
            lines.append(ld)
        lines.sort(key=lambda x: str(x.get("sku", "")))
        out["lines"] = lines
        return out

    def update_po_status(self, po_id: str, status: PoStatus) -> None:
        pid = (po_id or "").strip()
        pref = self._db.collection(COLLECTION_PURCHASE_ORDERS).document(pid)
        if not pref.get().exists:
            raise ValueError(f"Unknown PO: {pid}")
        pref.update({"status": status, "updated_at": SERVER_TIMESTAMP})

    def receive_purchase_line(
        self,
        po_id: str,
        line_id: str,
        receive_qty: int,
        *,
        note: str | None = None,
    ) -> None:
        if receive_qty <= 0:
            raise ValueError("receive_qty must be positive")
        pid = (po_id or "").strip()
        lid = (line_id or "").strip()
        pref = self._db.collection(COLLECTION_PURCHASE_ORDERS).document(pid)
        lref = pref.collection(SUB_LINES).document(lid)

        @fs.transactional
        def _receive_txn(transaction: fs.Transaction) -> tuple[str, str]:
            # Firestore transactions: perform all reads before any writes.
            psnap = pref.get(transaction=transaction)
            if not psnap.exists:
                raise ValueError(f"Unknown PO: {pid}")
            pdata = psnap.to_dict() or {}
            dest_wh = str(pdata.get("destination_warehouse_id", ""))
            if str(pdata.get("status")) == "cancelled":
                raise ValueError("Cannot receive on a cancelled PO")
            if not dest_wh:
                raise ValueError("PO missing destination_warehouse_id")

            lsnap = lref.get(transaction=transaction)
            if not lsnap.exists:
                raise ValueError("Unknown PO line")
            ldata = lsnap.to_dict() or {}
            sku = _normalize_sku(str(ldata.get("sku", "")))
            ordered = int(ldata.get("qty_ordered", 0))
            received = int(ldata.get("qty_received", 0))
            if received + receive_qty > ordered:
                raise ValueError("Receive quantity exceeds remaining on line")
            new_received = received + receive_qty

            stock_ref = self._warehouse_stock_ref(dest_wh, sku)
            ssnap = stock_ref.get(transaction=transaction)
            inv_ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
            inv_snap = inv_ref.get(transaction=transaction)

            line_docs = list(pref.collection(SUB_LINES).stream(transaction=transaction))
            tot_ord = 0
            tot_rec = 0
            for ld in line_docs:
                dd = ld.to_dict() or {}
                tot_ord += int(dd.get("qty_ordered", 0))
                rec = int(dd.get("qty_received", 0))
                if ld.id == lid:
                    rec = new_received
                tot_rec += rec

            new_status: PoStatus
            if tot_ord <= 0:
                new_status = "draft"
            elif tot_rec >= tot_ord:
                new_status = "received"
            else:
                new_status = "partially_received"

            transaction.update(lref, {"qty_received": new_received})

            cur = int((ssnap.to_dict() or {}).get("quantity", 0)) if ssnap.exists else 0
            if ssnap.exists:
                transaction.update(
                    stock_ref,
                    {"quantity": cur + receive_qty, "updated_at": SERVER_TIMESTAMP},
                )
            else:
                transaction.set(
                    stock_ref,
                    {"quantity": cur + receive_qty, "updated_at": SERVER_TIMESTAMP},
                )

            if inv_snap.exists and "quantity" in (inv_snap.to_dict() or {}):
                transaction.update(inv_ref, {"quantity": fs.DELETE_FIELD})

            transaction.update(pref, {"status": new_status, "updated_at": SERVER_TIMESTAMP})
            return dest_wh, sku

        dest_wh, log_sku = _receive_txn(self._db.transaction())

        self.log_movement(
            log_sku,
            "IN",
            receive_qty,
            source="ui",
            note=note or f"PO {pid} receipt",
            extra_fields={
                "warehouse_id": dest_wh,
                "event_type": "receipt",
                "ref_po_id": pid,
            },
        )

    # --- Stock transfers ---

    def create_stock_transfer(
        self,
        from_warehouse_id: str,
        to_warehouse_id: str,
        lines: list[tuple[str, int]],
        *,
        status: TransferStatus = "draft",
    ) -> str:
        fw = (from_warehouse_id or "").strip()
        tw = (to_warehouse_id or "").strip()
        if not fw or not tw:
            raise ValueError("from_warehouse_id and to_warehouse_id are required")
        if fw == tw:
            raise ValueError("Source and destination warehouse must differ")
        if not self._db.collection(COLLECTION_WAREHOUSES).document(fw).get().exists:
            raise ValueError(f"Unknown warehouse: {fw}")
        if not self._db.collection(COLLECTION_WAREHOUSES).document(tw).get().exists:
            raise ValueError(f"Unknown warehouse: {tw}")
        if not lines:
            raise ValueError("At least one line is required")

        tref = self._db.collection(COLLECTION_STOCK_TRANSFERS).document()
        tref.set(
            {
                "from_warehouse_id": fw,
                "to_warehouse_id": tw,
                "status": status,
                "created_at": SERVER_TIMESTAMP,
                "updated_at": SERVER_TIMESTAMP,
            }
        )
        batch = self._db.batch()
        for sku_raw, qty in lines:
            sku = _normalize_sku(sku_raw)
            if qty <= 0:
                raise ValueError("Line quantities must be positive")
            if not self._db.collection(COLLECTION_INVENTORY).document(sku).get().exists:
                raise ValueError(f"Unknown SKU: {sku}")
            lref = tref.collection(SUB_LINES).document()
            batch.set(lref, {"sku": sku, "quantity": int(qty)})
        batch.commit()
        return tref.id

    def list_stock_transfers(self, limit: int = 50) -> list[dict[str, Any]]:
        q = (
            self._db.collection(COLLECTION_STOCK_TRANSFERS)
            .order_by("created_at", direction=fs.Query.DESCENDING)
            .limit(limit)
        )
        rows: list[dict[str, Any]] = []
        for doc in q.stream():
            d = dict(doc.to_dict() or {})
            d["id"] = doc.id
            rows.append(d)
        return rows

    def get_stock_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        tid = (transfer_id or "").strip()
        tref = self._db.collection(COLLECTION_STOCK_TRANSFERS).document(tid)
        snap = tref.get()
        if not snap.exists:
            return None
        out = dict(snap.to_dict() or {})
        out["id"] = tid
        lines: list[dict[str, Any]] = []
        for ldoc in tref.collection(SUB_LINES).stream():
            ld = dict(ldoc.to_dict() or {})
            ld["id"] = ldoc.id
            lines.append(ld)
        lines.sort(key=lambda x: str(x.get("sku", "")))
        out["lines"] = lines
        return out

    def complete_stock_transfer(self, transfer_id: str) -> None:
        tid = (transfer_id or "").strip()
        tref = self._db.collection(COLLECTION_STOCK_TRANSFERS).document(tid)

        @fs.transactional
        def _complete(transaction: fs.Transaction) -> tuple[str, str, list[tuple[str, int]]]:
            tsnap = tref.get(transaction=transaction)
            if not tsnap.exists:
                raise ValueError(f"Unknown transfer: {tid}")
            tdata = tsnap.to_dict() or {}
            st = str(tdata.get("status", ""))
            if st == "completed":
                raise ValueError("Transfer already completed")
            if st == "cancelled":
                raise ValueError("Cannot complete a cancelled transfer")
            fw = str(tdata.get("from_warehouse_id", ""))
            tw = str(tdata.get("to_warehouse_id", ""))
            if not fw or not tw:
                raise ValueError("Transfer missing warehouse ids")

            line_payloads: list[tuple[str, int]] = []
            for ldoc in tref.collection(SUB_LINES).stream(transaction=transaction):
                dd = ldoc.to_dict() or {}
                sku = _normalize_sku(str(dd.get("sku", "")))
                qty = int(dd.get("quantity", 0))
                if qty <= 0:
                    continue
                line_payloads.append((sku, qty))

            if not line_payloads:
                raise ValueError("Transfer has no lines")

            snapshots: list[
                tuple[str, int, fs.DocumentReference, fs.DocumentReference, Any, Any, int, int]
            ] = []
            for sku, qty in line_payloads:
                src_ref = self._warehouse_stock_ref(fw, sku)
                dst_ref = self._warehouse_stock_ref(tw, sku)
                ss = src_ref.get(transaction=transaction)
                ds = dst_ref.get(transaction=transaction)
                cur = int((ss.to_dict() or {}).get("quantity", 0)) if ss.exists else 0
                dcur = int((ds.to_dict() or {}).get("quantity", 0)) if ds.exists else 0
                snapshots.append((sku, qty, src_ref, dst_ref, ss, ds, cur, dcur))

            for sku, qty, _, _, _, _, cur, _ in snapshots:
                if cur < qty:
                    raise ValueError(f"Insufficient stock for SKU {sku} at source warehouse")

            for sku, qty, src_ref, dst_ref, ss, ds, cur, dcur in snapshots:
                new_src = cur - qty
                new_dst = dcur + qty
                if ss.exists:
                    transaction.update(
                        src_ref,
                        {"quantity": new_src, "updated_at": SERVER_TIMESTAMP},
                    )
                else:
                    transaction.set(src_ref, {"quantity": new_src, "updated_at": SERVER_TIMESTAMP})

                if ds.exists:
                    transaction.update(
                        dst_ref,
                        {"quantity": new_dst, "updated_at": SERVER_TIMESTAMP},
                    )
                else:
                    transaction.set(
                        dst_ref,
                        {"quantity": new_dst, "updated_at": SERVER_TIMESTAMP},
                    )

            transaction.update(tref, {"status": "completed", "updated_at": SERVER_TIMESTAMP})
            return fw, tw, line_payloads

        fw, tw, payloads = _complete(self._db.transaction())

        for sku, qty in payloads:
            self.log_movement(
                sku,
                "OUT",
                qty,
                source="ui",
                note=f"Transfer {tid} to {tw}",
                extra_fields={
                    "warehouse_id": fw,
                    "event_type": "transfer",
                    "ref_transfer_id": tid,
                    "warehouse_ids": [fw, tw],
                },
            )
            self.log_movement(
                sku,
                "IN",
                qty,
                source="ui",
                note=f"Transfer {tid} from {fw}",
                extra_fields={
                    "warehouse_id": tw,
                    "event_type": "transfer",
                    "ref_transfer_id": tid,
                    "warehouse_ids": [fw, tw],
                },
            )
