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

MovementType = Literal["IN", "OUT"]
MovementSource = Literal["ui", "pos", "sync", "api"]


class InventoryProduct(TypedDict, total=False):
    """Firestore shape for an inventory document (SKU is document ID, not stored as field)."""

    name: str
    category: str
    quantity: int
    reorder_level: int
    unit_price: float


class ActivityEntry(TypedDict, total=False):
    sku: str
    movement_type: MovementType
    quantity_delta: int
    timestamp: Any
    note: str
    source: MovementSource


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
        source: MovementSource = "sync",
        note: str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError


class FirestoreManager(InventoryRepository):
    """All CRUD and stock movements for `inventory` and `activity_log` collections."""

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

    def create_product(self, product: Mapping[str, Any]) -> str:
        sku = _normalize_sku(str(product.get("sku", "")))
        name = str(product.get("name", "")).strip()
        if not name:
            raise ValueError("Name is required")

        category = str(product.get("category", "")).strip()
        quantity = int(product.get("quantity", 0))
        reorder_level = int(product.get("reorder_level", 0))
        _require_positive_int("quantity", quantity)
        _require_positive_int("reorder_level", reorder_level)

        unit_price = product.get("unit_price")
        data: dict[str, Any] = {
            "name": name,
            "category": category,
            "quantity": quantity,
            "reorder_level": reorder_level,
        }
        if unit_price is not None:
            data["unit_price"] = float(unit_price)

        ref = self._db.collection(COLLECTION_INVENTORY).document(sku)
        snap = ref.get()
        if snap.exists:
            raise ValueError(f"Product with SKU '{sku}' already exists")

        ref.set(data)
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
        if "quantity" in updates:
            q = int(updates["quantity"])
            _require_positive_int("quantity", q)
            patch["quantity"] = q
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
        ref.delete()

    def list_products(self, limit: int | None = None) -> list[dict[str, Any]]:
        query = self._db.collection(COLLECTION_INVENTORY).order_by("name")
        if limit is not None:
            query = query.limit(limit)
        rows: list[dict[str, Any]] = []
        for doc in query.stream():
            d = dict(doc.to_dict() or {})
            d["sku"] = doc.id
            rows.append(d)
        return rows

    def search_products(self, query: str) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return self.list_products()
        all_rows = self.list_products()
        return [
            row
            for row in all_rows
            if q in str(row.get("sku", "")).lower() or q in str(row.get("name", "")).lower()
        ]

    def get_dashboard_metrics(self) -> dict[str, float | int]:
        total_value = 0.0
        low_stock = 0
        for doc in self._db.collection(COLLECTION_INVENTORY).stream():
            data = doc.to_dict() or {}
            qty = int(data.get("quantity", 0))
            price = float(data.get("unit_price", 0) or 0)
            total_value += qty * price
            reorder = int(data.get("reorder_level", 0))
            if qty <= reorder:
                low_stock += 1
        return {"total_stock_value": round(total_value, 2), "low_stock_count": low_stock}

    def list_low_stock_products(self) -> list[dict[str, Any]]:
        """Products where quantity <= reorder_level (same rule as dashboard low-stock count)."""
        rows: list[dict[str, Any]] = []
        for doc in self._db.collection(COLLECTION_INVENTORY).stream():
            data = doc.to_dict() or {}
            qty = int(data.get("quantity", 0))
            reorder = int(data.get("reorder_level", 0))
            if qty <= reorder:
                d = dict(data)
                d["sku"] = doc.id
                rows.append(d)
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
        source: MovementSource = "ui",
        note: str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> None:
        """
        Single entry point for stock IN/OUT (UI, POS, or sync). Updates inventory and appends activity_log.
        """
        sku = _normalize_sku(sku)
        _require_positive_int("quantity_delta", quantity_delta)

        inv_ref = self._db.collection(COLLECTION_INVENTORY).document(sku)

        @fs.transactional
        def _adjust(transaction: fs.Transaction, ref: fs.DocumentReference) -> None:
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise ValueError(f"Unknown SKU: {sku}")
            data = snap.to_dict() or {}
            current = int(data.get("quantity", 0))
            if movement_type == "IN":
                new_qty = current + quantity_delta
            else:
                new_qty = current - quantity_delta
            if new_qty < 0:
                raise ValueError("Resulting quantity cannot be negative")
            transaction.update(ref, {"quantity": new_qty})

        _adjust(self._db.transaction(), inv_ref)

        self.log_movement(
            sku,
            movement_type,
            quantity_delta,
            source=source,
            note=note,
            extra_fields=extra_fields,
        )
