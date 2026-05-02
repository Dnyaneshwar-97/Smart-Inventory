# Smart-Inventory

Modular **Streamlit** retail dashboard with **Cloud Firestore** via `firebase-admin`.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Fill in the project root [`.env`](.env) file (gitignored): set **`GOOGLE_APPLICATION_CREDENTIALS`** to the absolute path of your service account JSON and **`GOOGLE_CLOUD_PROJECT`** to your GCP/Firebase project ID. The app loads these automatically via `python-dotenv` when `database.py` imports.

Alternatively, export the same variables in your shell instead of using `.env`:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/your-service-account.json
export GOOGLE_CLOUD_PROJECT=your-gcp-project-id
# optional alternative for containers / Secret Manager:
# export FIREBASE_CREDENTIALS_JSON='{"type":"service_account",...}'
```

```bash
streamlit run app.py
```

Open `http://localhost:8501`.

## Docker

```bash
docker build -t smart-inventory .
docker run --rm -p 8501:8501 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa.json \
  -v /path/to/your-service-account.json:/secrets/sa.json:ro \
  -e GOOGLE_CLOUD_PROJECT=your-project-id \
  smart-inventory
```

## Deploy

Use [app.yaml](app.yaml) as a **Cloud Run** (Knative) service template and push the image to Artifact Registry. Firebase App Hosting is not aimed at Streamlit; run this container on Cloud Run in the same GCP project as Firebase.

## Collections

- `inventory` — document ID = SKU; fields: `name`, `category`, `quantity`, `reorder_level`, optional `unit_price`.
- `activity_log` — stock IN/OUT audit rows written by `FirestoreManager.apply_stock_adjustment`.
