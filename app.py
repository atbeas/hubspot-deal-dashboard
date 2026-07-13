import os
import json
import sqlite3
import secrets
import threading
import requests
import concurrent.futures
from functools import wraps
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash, check_password_hash
import apollo_pipeline as apollo

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

DB_PATH = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), "client_contacts.db")

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_contacts (
                client_value TEXT PRIMARY KEY,
                email1 TEXT DEFAULT '',
                email2 TEXT DEFAULT '',
                email3 TEXT DEFAULT ''
            )
        """)
        for col in ("name1", "name2", "name3"):
            try:
                conn.execute(f"ALTER TABLE client_contacts ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE client_contacts ADD COLUMN meeting_quota INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS owner_eligibility (
                owner_id TEXT PRIMARY KEY,
                eligible_am INTEGER DEFAULT 1,
                eligible_se INTEGER DEFAULT 1,
                eligible_sd INTEGER DEFAULT 1
            )
        """)
        try:
            conn.execute("ALTER TABLE owner_eligibility ADD COLUMN eligible_sd INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_passwords (
                company_key TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quick_notes_submitted (
                deal_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deal_meeting (
                deal_id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL,
                meeting_label TEXT NOT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE deal_meeting ADD COLUMN meeting_calendar TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deal_prep_email (
                deal_id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deal_handoff_email (
                deal_id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deal_archive (
                deal_id TEXT PRIMARY KEY,
                archived_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_eligibility (
                client_value TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                criteria_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pull_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_name TEXT,
                criteria_json TEXT,
                stage TEXT NOT NULL DEFAULT 'scrubbing',
                created_at TEXT NOT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE pull_batches ADD COLUMN total_entries INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pull_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                apollo_person_id TEXT NOT NULL,
                apollo_contact_id TEXT,
                organization_id TEXT,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                title TEXT DEFAULT '',
                company_name TEXT DEFAULT '',
                keep INTEGER NOT NULL DEFAULT 1,
                enrich_status TEXT NOT NULL DEFAULT 'pending',
                email TEXT DEFAULT '',
                email_status TEXT DEFAULT '',
                linkedin_url TEXT DEFAULT '',
                mobile_phone TEXT DEFAULT '',
                mobile_status TEXT NOT NULL DEFAULT 'pending',
                company_phone TEXT DEFAULT '',
                website TEXT DEFAULT '',
                company_linkedin_url TEXT DEFAULT '',
                company_address TEXT DEFAULT '',
                state TEXT DEFAULT '',
                city TEXT DEFAULT '',
                employees INTEGER,
                annual_revenue REAL,
                industry TEXT DEFAULT '',
                time_zone TEXT DEFAULT '',
                hubspot_contact_id TEXT,
                pushed_at TEXT
            )
        """)
        existing = conn.execute("SELECT COUNT(*) c FROM saved_searches").fetchone()["c"]
        if existing == 0:
            conn.execute(
                "INSERT INTO saved_searches (name, criteria_json, created_at) VALUES (?, ?, ?)",
                ["MSPs", json.dumps({
                    "locations": [],
                    "employee_min": 1,
                    "employee_max": 50,
                    "titles": ["Owner", "President", "CEO"],
                    "seniorities": [],
                    "keyword_tags": ["managed service provider"],
                }), datetime.now(timezone.utc).isoformat()]
            )

init_db()

MS_TENANT_ID     = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID     = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
SCHEDULING_EMAIL = "scheduling@10talent.tech"
# Every calendar mailbox the app pulls meetings from / books invites on.
# Add an entry here when a new calendar is wired up.
CONNECTED_CALENDARS = [
    {"email": SCHEDULING_EMAIL, "label": "10talent Tech"},
    {"email": "info@runwayselling.com", "label": "Runway Selling"},
]
PREP_EMAIL_FROM  = "info@runwayselling.com"
SEND_CONFIRM_BCC = "info@runwayselling.com"
DEFAULT_EMAIL_TEMPLATE = (
    "Hi [[client_name]],\n\n"
    "Thanks for taking the time to meet with us. Below are some notes to help "
    "you prepare for our upcoming conversation:\n"
)
DEFAULT_HANDOFF_EMAIL_TEMPLATE = (
    "Hi [[first_name]],\n\n"
    "It was great learning more about your business. As we move forward, we wanted to "
    "introduce you to your dedicated team:\n\n"
    "[[account_manager]] — Account Manager\n"
    "[[sales_executive]] — Sales Executive\n\n"
    "They'll be your main points of contact moving forward and are happy to answer any "
    "questions as we continue working together.\n"
)
DEFAULT_EMAIL_SUBJECT_TEMPLATE = "Runway Selling Meeting Notes | [[company]] | [[client]]"
DEFAULT_HANDOFF_SUBJECT_TEMPLATE = "[[brand]] | Introducing your Team | [[company]]"

def get_setting(key, default=""):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", [key]).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, [key, value])

def prep_template_for(calendar_email):
    # Falls back to the old single global template (pre-multi-calendar) for
    # the original 10talent calendar only, so nothing already written is lost.
    legacy = get_setting("email_intro_template", DEFAULT_EMAIL_TEMPLATE) if calendar_email == SCHEDULING_EMAIL else DEFAULT_EMAIL_TEMPLATE
    return get_setting(f"email_intro_template::{calendar_email}", legacy)

def handoff_template_for(calendar_email):
    legacy = get_setting("handoff_email_template", DEFAULT_HANDOFF_EMAIL_TEMPLATE) if calendar_email == SCHEDULING_EMAIL else DEFAULT_HANDOFF_EMAIL_TEMPLATE
    return get_setting(f"handoff_email_template::{calendar_email}", legacy)

def prep_subject_for(calendar_email):
    return get_setting(f"email_intro_subject::{calendar_email}", DEFAULT_EMAIL_SUBJECT_TEMPLATE)

def handoff_subject_for(calendar_email):
    return get_setting(f"handoff_email_subject::{calendar_email}", DEFAULT_HANDOFF_SUBJECT_TEMPLATE)

def get_confirmed_meeting(deal_id):
    # A deal has "completed Step 3" only once a meeting has been explicitly
    # confirmed via Add to Invite — an auto-suggested match doesn't count,
    # since that's exactly the kind of unconfirmed guess that shouldn't
    # decide which mailbox a customer-facing email gets sent from.
    with get_db() as conn:
        row = conn.execute(
            "SELECT meeting_id, meeting_calendar FROM deal_meeting WHERE deal_id = ?", [deal_id]
        ).fetchone()
    if not row or not row["meeting_id"]:
        return None
    return {"meeting_id": row["meeting_id"], "calendar": row["meeting_calendar"] or ""}

def get_ms_token():
    resp = requests.post(
        f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     MS_CLIENT_ID,
            "client_secret": MS_CLIENT_SECRET,
            "scope":         "https://graph.microsoft.com/.default",
        },
    )
    return resp.json().get("access_token", "")

def get_calendar_status(email):
    # Hits the /calendar endpoint (not /users/{email}) because the app's
    # Graph permissions are scoped to Calendars.* only, not User.Read.All —
    # a directory lookup would 403 even when the calendar itself is fine.
    try:
        token = get_ms_token()
        if not token:
            return {"connected": False, "detail": "Could not authenticate with Microsoft Graph"}
        resp = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{email}/calendar?$select=name,owner",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.ok:
            owner_name = resp.json().get("owner", {}).get("name", "")
            return {"connected": True, "detail": f"Owner: {owner_name}" if owner_name else ""}
        return {"connected": False, "detail": f"Graph API error ({resp.status_code})"}
    except Exception as e:
        return {"connected": False, "detail": str(e)}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY")
APP_PASSWORD     = os.environ.get("APP_PASSWORD", "")
BASE_URL = "https://api.hubapi.com"
HEADERS = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}

def login_required(f):
    """Admin-only: the main dashboard password."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("scope") != "admin":
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def quick_notes_login_required(f):
    """Admin OR that specific company's own password can access."""
    @wraps(f)
    def decorated(*args, **kwargs):
        company = kwargs.get("company")
        scope = session.get("scope")
        if scope == "admin" or scope == f"company:{company}":
            return f(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return decorated


def any_login_required(f):
    """Admin OR any company session can access (shared write endpoint)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        scope = session.get("scope")
        if scope == "admin" or (scope and scope.startswith("company:")):
            return f(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return decorated


def get_company_password_hash(company):
    with get_db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM company_passwords WHERE company_key = ?", [company]
        ).fetchone()
    return row["password_hash"] if row else None


def check_company_password(company, password):
    stored_hash = get_company_password_hash(company)
    if stored_hash:
        return check_password_hash(stored_hash, password)
    # No custom password set yet — fall back to the main dashboard password
    return password == APP_PASSWORD


def set_company_password(company, password):
    password_hash = generate_password_hash(password)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO company_passwords (company_key, password_hash)
            VALUES (?, ?)
            ON CONFLICT(company_key) DO UPDATE SET password_hash = excluded.password_hash
        """, [company, password_hash])

# Pipelines the main dashboard pulls deals from: the standard SD pipeline
# plus Runway Selling's own new-client pipeline (own brand's deals don't
# funnel through the outsourced-SD "1 SD New Deal Pipeline").
DASHBOARD_PIPELINES = ["default", "77097563"]

# Real HubSpot deal property names
ROLE_PROPS = {
    "client": "rs_partner",                # enumeration — client/partner name
    "sd":     "sales_development__new_",   # owner ID
    "am":     "account_manager",           # owner ID
    "se":     "sales_executive__new_",     # owner ID
}

# Owner-ID-based roles (dropdown shows HubSpot users)
OWNER_ROLES = {"sd", "am", "se"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_url = request.values.get("next") or url_for("index")
    locked_company = _locked_company_for_host()
    if request.method == "POST":
        password = request.form.get("password", "")
        if locked_company:
            if check_company_password(locked_company, password):
                session["scope"] = f"company:{locked_company}"
                return redirect(next_url)
        else:
            if password == APP_PASSWORD:
                session["scope"] = "admin"
                return redirect(next_url)
            # Also allow a company password to log straight into its own
            # quick-notes page even from the main domain.
            for key in QUICK_NOTES_COMPANIES:
                if get_company_password_hash(key) and check_company_password(key, password):
                    session["scope"] = f"company:{key}"
                    return redirect(url_for("quick_notes", company=key))
        error = "Incorrect password."
    return render_template("login.html", error=error, next=next_url)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Custom domains that should land directly on a company's quick-notes page
# instead of the full dashboard.
QUICK_NOTES_HOST_MAP = {
    "zap.runwayselling.app": "zap",
    "biz.runwayselling.app": "biznatron",
}


@app.route("/")
def index():
    host = request.host.split(":")[0].lower()
    company = QUICK_NOTES_HOST_MAP.get(host)
    if company:
        # This host is locked to one company's quick-notes page — redirect
        # regardless of auth scope. A company-scoped login is valid here
        # even though it isn't "admin", and quick_notes() below enforces
        # its own login check for the unauthenticated case.
        return redirect(url_for("quick_notes", company=company))

    if session.get("scope") != "admin":
        return redirect(url_for("login", next=request.path))

    # Fetch rs_partner enum options
    client_options = []
    resp = requests.get(
        f"{BASE_URL}/crm/v3/properties/deals/rs_partner",
        headers=HEADERS,
    )
    if resp.ok:
        data = resp.json()
        client_eligibility = get_client_eligibility()
        client_quotas = get_client_quotas()
        client_options = [
            {
                "label": o["label"],
                "value": o["value"],
                "enabled": client_eligibility.get(o["value"], True),
                "meeting_quota": client_quotas.get(o["value"], 0),
            }
            for o in data.get("options", [])
            if not o.get("hidden")
        ]

    return render_template("index.html", client_options=client_options, connected_calendars=CONNECTED_CALENDARS)


def fetch_hubspot_owners():
    owners = []
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        resp = requests.get(f"{BASE_URL}/crm/v3/owners", headers=HEADERS, params=params)
        if not resp.ok:
            break
        data = resp.json()
        for o in data.get("results", []):
            if not o.get("archived"):
                name = f"{o.get('firstName','')} {o.get('lastName','')}".strip() or o.get("email", "")
                if name:
                    owners.append({
                        "id":    str(o["id"]),
                        "name":  name,
                        "email": o.get("email", ""),
                    })
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    owners.sort(key=lambda x: x["name"].lower())
    return owners


def get_owner_eligibility():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM owner_eligibility").fetchall()
    return {
        r["owner_id"]: {"am": bool(r["eligible_am"]), "se": bool(r["eligible_se"]), "sd": bool(r["eligible_sd"])}
        for r in rows
    }


def get_client_eligibility():
    with get_db() as conn:
        rows = conn.execute("SELECT client_value, enabled FROM client_eligibility").fetchall()
    return {r["client_value"]: bool(r["enabled"]) for r in rows}


def get_client_quotas():
    with get_db() as conn:
        rows = conn.execute("SELECT client_value, meeting_quota FROM client_contacts").fetchall()
    return {r["client_value"]: (r["meeting_quota"] or 0) for r in rows}


def get_deal_meetings():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM deal_meeting").fetchall()
    return {
        r["deal_id"]: {"id": r["meeting_id"], "label": r["meeting_label"], "calendar": r["meeting_calendar"] or ""}
        for r in rows
    }


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_initial_meeting_times(deal_ids):
    if not deal_ids:
        return {}

    deal_to_contact = {}
    for chunk in _chunks(deal_ids, 100):
        resp = requests.post(
            f"{BASE_URL}/crm/v4/associations/deals/contacts/batch/read",
            headers=HEADERS,
            json={"inputs": [{"id": d} for d in chunk]},
        )
        if not resp.ok:
            continue
        for r in resp.json().get("results", []):
            to = r.get("to", [])
            if to:
                deal_to_contact[r["from"]["id"]] = str(to[0]["toObjectId"])

    contact_ids = list(set(deal_to_contact.values()))
    if not contact_ids:
        return {}

    contact_booking_links = {}
    for chunk in _chunks(contact_ids, 100):
        resp = requests.post(
            f"{BASE_URL}/crm/v3/objects/contacts/batch/read",
            headers=HEADERS,
            json={
                "properties": ["first_conversion_event_name", "recent_conversion_event_name"],
                "inputs": [{"id": c} for c in chunk],
            },
        )
        if not resp.ok:
            continue
        for r in resp.json().get("results", []):
            props = r.get("properties", {})
            for field in ("first_conversion_event_name", "recent_conversion_event_name"):
                val = props.get(field) or ""
                if val.startswith("Meetings Link: "):
                    slug = val[len("Meetings Link: "):]
                    contact_booking_links[r["id"]] = f"https://meetings.hubspot.com/{slug}"
                    break

    contact_to_meetings = {}
    for chunk in _chunks(contact_ids, 100):
        resp = requests.post(
            f"{BASE_URL}/crm/v4/associations/contacts/meetings/batch/read",
            headers=HEADERS,
            json={"inputs": [{"id": c} for c in chunk]},
        )
        if not resp.ok:
            continue
        for r in resp.json().get("results", []):
            contact_to_meetings[r["from"]["id"]] = [str(t["toObjectId"]) for t in r.get("to", [])]

    meeting_ids = list({m for ms in contact_to_meetings.values() for m in ms})
    if not meeting_ids:
        return {}

    meeting_times = {}
    for chunk in _chunks(meeting_ids, 100):
        resp = requests.post(
            f"{BASE_URL}/crm/v3/objects/meetings/batch/read",
            headers=HEADERS,
            json={"properties": ["hs_meeting_start_time"], "inputs": [{"id": m} for m in chunk]},
        )
        if not resp.ok:
            continue
        for r in resp.json().get("results", []):
            start = r.get("properties", {}).get("hs_meeting_start_time")
            if start:
                meeting_times[r["id"]] = start

    result = {}
    for deal_id, contact_id in deal_to_contact.items():
        times = [meeting_times[m] for m in contact_to_meetings.get(contact_id, []) if m in meeting_times]
        if not times:
            continue
        earliest = min(times)
        try:
            dt_utc = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
            result[deal_id] = {
                "label": dt_utc.astimezone(PACIFIC_TZ).strftime("%b %d, %Y %-I:%M %p PT"),
                "start_utc": dt_utc.isoformat(),
                # Which live calendar mailbox actually hosts this meeting isn't
                # recorded in HubSpot — the frontend resolves that by matching
                # this start_utc against the merged /api/meetings result.
                "booking_link": contact_booking_links.get(contact_id, ""),
            }
        except Exception:
            continue

    return result


def get_deal_prep_emails():
    with get_db() as conn:
        rows = conn.execute("SELECT deal_id FROM deal_prep_email").fetchall()
    return {r["deal_id"] for r in rows}


def get_deal_handoff_emails():
    with get_db() as conn:
        rows = conn.execute("SELECT deal_id FROM deal_handoff_email").fetchall()
    return {r["deal_id"] for r in rows}


def get_deal_archive():
    with get_db() as conn:
        rows = conn.execute("SELECT deal_id, archived_at FROM deal_archive").fetchall()
    return {r["deal_id"]: r["archived_at"] for r in rows}


@app.route("/api/owners")
@login_required
def get_owners():
    owners = fetch_hubspot_owners()
    eligibility = get_owner_eligibility()
    for o in owners:
        e = eligibility.get(o["id"], {"am": True, "se": True, "sd": True})
        o["eligible_am"] = e["am"]
        o["eligible_se"] = e["se"]
        o["eligible_sd"] = e["sd"]
    return jsonify({"owners": owners})


ELIGIBILITY_COLUMNS = {"am": "eligible_am", "se": "eligible_se", "sd": "eligible_sd"}


@app.route("/api/owners/<owner_id>/eligibility", methods=["POST"])
@login_required
def set_owner_eligibility(owner_id):
    body = request.get_json()
    role = body.get("role")
    eligible = 1 if body.get("eligible") else 0
    if role not in ELIGIBILITY_COLUMNS:
        return jsonify({"error": "role must be 'am', 'se', or 'sd'"}), 400

    column = ELIGIBILITY_COLUMNS[role]
    with get_db() as conn:
        conn.execute(f"""
            INSERT INTO owner_eligibility (owner_id, {column})
            VALUES (?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET {column} = excluded.{column}
        """, [owner_id, eligible])
    return jsonify({"ok": True})


@app.route("/api/deals")
@login_required
def get_deals():
    start = request.args.get("start")
    end   = request.args.get("end")
    show_archived = request.args.get("archived", "false").lower() == "true"
    if not start or not end:
        return jsonify({"error": "start and end required"}), 400

    start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
    end_ms   = int(datetime.fromisoformat(end).timestamp() * 1000) + 86399999

    properties = [
        "dealname", "createdate", "dealstage", "pipeline", "amount", "hubspot_owner_id",
        "business_needs", "client_facing_notes",
    ] + list(ROLE_PROPS.values())

    filters = [
        {"propertyName": "createdate", "operator": "GTE", "value": str(start_ms)},
        {"propertyName": "createdate", "operator": "LTE", "value": str(end_ms)},
        {"propertyName": "pipeline",   "operator": "IN",  "values": DASHBOARD_PIPELINES},
    ]

    payload = {
        "filterGroups": [{"filters": filters}],
        "properties": properties,
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
        "limit": 200,
    }

    resp = requests.post(
        f"{BASE_URL}/crm/v3/objects/deals/search",
        headers=HEADERS,
        json=payload,
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    data = resp.json()
    owner_cache = {}
    deal_meetings = get_deal_meetings()
    prep_emails_sent = get_deal_prep_emails()
    handoff_emails_sent = get_deal_handoff_emails()
    deal_archive = get_deal_archive()
    initial_meeting_times = get_initial_meeting_times([r["id"] for r in data.get("results", [])])

    def resolve_owner(owner_id):
        if not owner_id:
            return ""
        if owner_id not in owner_cache:
            o = requests.get(f"{BASE_URL}/crm/v3/owners/{owner_id}", headers=HEADERS)
            owner_cache[owner_id] = (
                f"{o.json().get('firstName','')} {o.json().get('lastName','')}".strip()
                if o.ok else ""
            )
        return owner_cache[owner_id]

    deals = []
    for result in data.get("results", []):
        props = result.get("properties", {})

        create_ts = props.get("createdate", "")
        try:
            create_date = datetime.fromisoformat(
                create_ts.replace("Z", "+00:00")
            ).strftime("%b %d, %Y")
        except Exception:
            create_date = create_ts

        amount = props.get("amount") or ""
        try:
            amount = f"${float(amount):,.0f}" if amount else ""
        except Exception:
            pass

        meeting = deal_meetings.get(result["id"], {})

        archived_at_raw = deal_archive.get(result["id"])
        is_archived = archived_at_raw is not None
        if is_archived != show_archived:
            continue
        archived_at = ""
        if archived_at_raw:
            try:
                archived_at = datetime.fromisoformat(archived_at_raw).strftime("%b %d, %Y")
            except Exception:
                archived_at = archived_at_raw

        deals.append({
            "id":      result["id"],
            "name":    props.get("dealname") or "(No name)",
            "created": create_date,
            "stage":   props.get("dealstage") or "",
            "amount":  amount,
            "owner":   resolve_owner(props.get("hubspot_owner_id")),
            # Values: client is a string, sd/am/se are owner IDs
            "client":        props.get(ROLE_PROPS["client"]) or "",
            "sd":            props.get(ROLE_PROPS["sd"])     or "",
            "am":            props.get(ROLE_PROPS["am"])     or "",
            "se":            props.get(ROLE_PROPS["se"])     or "",
            "business_needs": props.get("business_needs")   or "",
            "client_facing_notes": props.get("client_facing_notes") or "",
            "meeting_id":       meeting.get("id", ""),
            "meeting_label":    meeting.get("label", ""),
            "meeting_calendar": meeting.get("calendar", ""),
            "initial_meeting": initial_meeting_times.get(result["id"], {}).get("label", ""),
            "initial_meeting_start": initial_meeting_times.get(result["id"], {}).get("start_utc", ""),
            "initial_meeting_booking_link": initial_meeting_times.get(result["id"], {}).get("booking_link", ""),
            "prep_email_sent": result["id"] in prep_emails_sent,
            "handoff_email_sent": result["id"] in handoff_emails_sent,
            "archived":    is_archived,
            "archived_at": archived_at,
        })

    return jsonify({"deals": deals, "total": len(deals)})


# "Reschedule Meeting 1 (No Show, Declined, Canceled)" stage ID in the
# "1 SD New Deal Pipeline" (DASHBOARD_PIPELINES[0], "default") — the only
# pipeline that has this exact stage. The client sidebar's no-show count
# only matches this stage, by design.
NO_SHOW_STAGE_ID = "1072720995"

_STAGE_LABEL_CACHE = {"data": None, "ts": 0}
STAGE_LABEL_CACHE_TTL = 600  # seconds


def get_deal_stage_labels():
    # stage IDs are unique within a pipeline, and the dashboard's two
    # pipelines don't share any IDs, so a flat id -> label map is safe here.
    # Cached briefly since this is fetched on every dashboard page load.
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _STAGE_LABEL_CACHE["data"]
    if cached and (now_ts - _STAGE_LABEL_CACHE["ts"] < STAGE_LABEL_CACHE_TTL):
        return cached

    labels = {}
    for pipeline_id in DASHBOARD_PIPELINES:
        resp = requests.get(f"{BASE_URL}/crm/v3/pipelines/deals/{pipeline_id}", headers=HEADERS)
        if not resp.ok:
            continue
        for stage in resp.json().get("stages", []):
            labels[stage["id"]] = stage.get("label", stage["id"])

    _STAGE_LABEL_CACHE["data"] = labels
    _STAGE_LABEL_CACHE["ts"] = now_ts
    return labels


@app.route("/api/client-deal-counts")
@login_required
def get_client_deal_counts():
    # Powers the dashboard's client sidebar — how many deals landed for each
    # client in the last 30 days, and which ones, so hovering a name shows
    # the actual deal names (plus created date + current stage) without a
    # separate lookup per client.
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
    stage_labels = get_deal_stage_labels()

    filters = [
        {"propertyName": "createdate", "operator": "GTE", "value": str(since_ms)},
        {"propertyName": "pipeline",   "operator": "IN",  "values": DASHBOARD_PIPELINES},
    ]

    clients = {}
    after = None
    while True:
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": ["dealname", "dealstage", "createdate", ROLE_PROPS["client"]],
            "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
            "limit": 100,
        }
        if after:
            payload["after"] = after

        resp = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=payload)
        if not resp.ok:
            return jsonify({"error": resp.text}), resp.status_code

        data = resp.json()
        for result in data.get("results", []):
            props = result.get("properties", {})
            client_value = props.get(ROLE_PROPS["client"]) or ""
            if not client_value:
                continue

            stage_id = props.get("dealstage") or ""
            create_ts = props.get("createdate", "")
            try:
                created = datetime.fromisoformat(create_ts.replace("Z", "+00:00")).strftime("%b %d, %Y")
            except Exception:
                created = create_ts

            entry = clients.setdefault(client_value, {"count": 0, "no_show": 0, "deals": []})
            entry["count"] += 1
            if stage_id == NO_SHOW_STAGE_ID:
                entry["no_show"] += 1
            entry["deals"].append({
                "id": result["id"],
                "name": props.get("dealname") or "(No name)",
                "created": created,
                "stage": stage_labels.get(stage_id, stage_id or "—"),
            })

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return jsonify({"clients": clients})


@app.route("/api/deals/<deal_id>", methods=["PATCH"])
@any_login_required
def update_deal(deal_id):
    body = request.get_json()
    properties = {}
    for key, prop_name in ROLE_PROPS.items():
        if key in body:
            properties[prop_name] = body[key]
    if "business_needs" in body:
        properties["business_needs"] = body["business_needs"]
    if "client_facing_notes" in body:
        properties["client_facing_notes"] = body["client_facing_notes"]

    if not properties:
        return jsonify({"error": "No properties to update"}), 400

    resp = requests.patch(
        f"{BASE_URL}/crm/v3/objects/deals/{deal_id}",
        headers=HEADERS,
        json={"properties": properties},
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    return jsonify({"ok": True})


# Each outsourced appointment-setting company books through its own dedicated
# meeting link / Workflow 1 branch. Deals from each branch are uniquely
# fingerprinted by this owner + sales-dev-owner combo (static values set in
# the branch's deal-creation action) — that's how we scope each company to
# only their own bookings.
QUICK_NOTES_COMPANIES = {
    "zap": {
        "label": "ZAP",
        "owner_id": "89539474",
        "sd_id": "84551230",
        "booking_link": "https://meetings.hubspot.com/10talent",
    },
    "biznatron": {
        "label": "Biznatron",
        "owner_id": "89539474",
        "sd_id": "91889388",
        "booking_link": "https://meetings.hubspot.com/10talent/b",
    },
}


def _locked_company_for_host():
    """If this request came in on a company-specific custom domain, that
    domain's company is the ONLY one it's allowed to view — regardless of
    what company slug is in the URL path."""
    host = request.host.split(":")[0].lower()
    return QUICK_NOTES_HOST_MAP.get(host)


@app.route("/quick-notes/<company>")
@quick_notes_login_required
def quick_notes(company):
    locked = _locked_company_for_host()
    if locked and locked != company:
        return redirect(url_for("quick_notes", company=locked))

    cfg = QUICK_NOTES_COMPANIES.get(company)
    if not cfg:
        return "Unknown company", 404
    return render_template(
        "quick_notes.html",
        company=company,
        company_label=cfg["label"],
        booking_link=cfg.get("booking_link", ""),
    )


@app.route("/api/quick-notes/<company>")
@quick_notes_login_required
def get_quick_notes(company):
    locked = _locked_company_for_host()
    if locked and locked != company:
        return jsonify({"error": "This domain is only allowed to view its own company"}), 403

    cfg = QUICK_NOTES_COMPANIES.get(company)
    if not cfg:
        return jsonify({"error": "Unknown company"}), 404

    hours = int(request.args.get("hours", 168))
    since_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)

    payload = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "createdate", "operator": "GTE", "value": str(since_ms)},
                {"propertyName": "pipeline", "operator": "EQ", "value": "default"},
                {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": cfg["owner_id"]},
                {"propertyName": "sales_development__new_", "operator": "EQ", "value": cfg["sd_id"]},
            ]
        }],
        "properties": ["dealname", "createdate", "business_needs"],
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
        "limit": 25,
    }

    resp = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=payload)
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    with get_db() as conn:
        submitted_ids = {
            r["deal_id"] for r in
            conn.execute("SELECT deal_id FROM quick_notes_submitted WHERE company = ?", [company]).fetchall()
        }

    now = datetime.now(timezone.utc)
    deals = []
    for result in resp.json().get("results", []):
        if result["id"] in submitted_ids:
            continue
        props = result.get("properties", {})
        created_dt = datetime.fromisoformat(props["createdate"].replace("Z", "+00:00"))
        minutes_ago = int((now - created_dt).total_seconds() // 60)
        if minutes_ago < 60:
            created_label = f"{minutes_ago} min ago" if minutes_ago != 1 else "1 min ago"
        elif minutes_ago < 1440:
            hours_ago = minutes_ago // 60
            created_label = f"{hours_ago} hr ago" if hours_ago == 1 else f"{hours_ago} hrs ago"
        else:
            days_ago = minutes_ago // 1440
            created_label = f"{days_ago} day ago" if days_ago == 1 else f"{days_ago} days ago"

        deals.append({
            "id": result["id"],
            "name": props.get("dealname") or "(No name)",
            "created_label": created_label,
            "business_needs": props.get("business_needs") or "",
        })

    return jsonify({"deals": deals})


@app.route("/api/quick-notes/<company>/<deal_id>/submit", methods=["POST"])
@quick_notes_login_required
def submit_quick_note(company, deal_id):
    locked = _locked_company_for_host()
    if locked and locked != company:
        return jsonify({"error": "This domain is only allowed to view its own company"}), 403

    if company not in QUICK_NOTES_COMPANIES:
        return jsonify({"error": "Unknown company"}), 404

    body = request.get_json() or {}
    resp = requests.patch(
        f"{BASE_URL}/crm/v3/objects/deals/{deal_id}",
        headers=HEADERS,
        json={"properties": {"business_needs": body.get("business_needs", "")}},
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    with get_db() as conn:
        conn.execute("""
            INSERT INTO quick_notes_submitted (deal_id, company, submitted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(deal_id) DO NOTHING
        """, [deal_id, company, datetime.now(timezone.utc).isoformat()])

    return jsonify({"ok": True})


@app.route("/admin")
@login_required
def admin():
    company_domains = {v: k for k, v in QUICK_NOTES_HOST_MAP.items()}
    companies = []
    for key, cfg in QUICK_NOTES_COMPANIES.items():
        domain = company_domains.get(key)
        companies.append({
            "key": key,
            "label": cfg["label"],
            "has_custom_password": get_company_password_hash(key) is not None,
            "page_url": f"https://{domain}" if domain else url_for("quick_notes", company=key),
        })

    calendars = []
    for cfg in CONNECTED_CALENDARS:
        status = get_calendar_status(cfg["email"])
        calendars.append({**cfg, **status})

    return render_template("admin.html", companies=companies, calendars=calendars)


@app.route("/api/admin/companies/<company>/password", methods=["POST"])
@login_required
def set_company_password_route(company):
    if company not in QUICK_NOTES_COMPANIES:
        return jsonify({"error": "Unknown company"}), 404
    body = request.get_json() or {}
    password = (body.get("password") or "").strip()
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400
    set_company_password(company, password)
    return jsonify({"ok": True})


@app.route("/api/meetings")
@login_required
def get_meetings():
    token = get_ms_token()
    if not token:
        return jsonify({"error": "Could not get Microsoft token"}), 500

    ms_headers = {"Authorization": f"Bearer {token}"}
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pull from every connected calendar so Step 3 and the conflict check can
    # see meetings regardless of which mailbox they're actually booked on.
    meetings = []
    for cal in CONNECTED_CALENDARS:
        resp = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{cal['email']}/calendarView",
            headers=ms_headers,
            params={
                "startDateTime": start,
                "endDateTime":   end,
                "$select":       "id,subject,start,end,attendees",
                "$orderby":      "start/dateTime",
                "$top":          100,
            },
        )
        if not resp.ok:
            continue

        for e in resp.json().get("value", []):
            start_utc = ""
            try:
                # Graph calendarView returns UTC dateTimes without a "Z" suffix
                # (confirmed via start.timeZone == "UTC") unless a Prefer header
                # requests otherwise, so treat the raw value as UTC.
                dt_utc = datetime.fromisoformat(e["start"]["dateTime"].rstrip("Z")).replace(tzinfo=timezone.utc)
                start_utc = dt_utc.isoformat()
                label = f"{e['subject']} — {dt_utc.astimezone(PACIFIC_TZ).strftime('%b %d, %Y %-I:%M %p')} PT"
            except Exception:
                label = e.get("subject", "(No subject)")
            meetings.append({
                "id": e["id"],
                "label": label,
                "start_utc": start_utc,
                "calendar": cal["email"],
                "calendar_label": cal["label"],
            })

    meetings.sort(key=lambda m: m["start_utc"])
    return jsonify({"meetings": meetings})


@app.route("/api/meetings/<path:event_id>/add-attendees", methods=["POST"])
@login_required
def add_meeting_attendees(event_id):
    body     = request.get_json()
    emails   = [e for e in body.get("emails", []) if e]
    calendar = body.get("calendar", "")
    if not emails:
        return jsonify({"error": "No emails provided"}), 400
    # Event IDs are scoped to a single mailbox, so we need to know which
    # connected calendar this event lives on before we can look it up.
    if calendar not in {c["email"] for c in CONNECTED_CALENDARS}:
        return jsonify({"error": "Unknown or missing calendar"}), 400

    token = get_ms_token()
    ms_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Fetch current attendees so we don't wipe them
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{calendar}/events/{event_id}",
        headers=ms_headers,
        params={"$select": "attendees"},
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    current = resp.json().get("attendees", [])
    existing = {a["emailAddress"]["address"].lower() for a in current}

    for email in emails:
        if email.lower() not in existing:
            current.append({"emailAddress": {"address": email}, "type": "required"})
            existing.add(email.lower())

    patch = requests.patch(
        f"https://graph.microsoft.com/v1.0/users/{calendar}/events/{event_id}",
        headers=ms_headers,
        json={"attendees": current},
    )
    if not patch.ok:
        return jsonify({"error": patch.text}), patch.status_code

    return jsonify({"ok": True, "added": len(emails)})


@app.route("/api/deals/<deal_id>/meeting", methods=["POST"])
@login_required
def set_deal_meeting(deal_id):
    body = request.get_json()
    meeting_id       = body.get("meeting_id", "")
    meeting_label    = body.get("meeting_label", "")
    meeting_calendar = body.get("meeting_calendar", "")
    if not meeting_id:
        return jsonify({"error": "meeting_id required"}), 400

    with get_db() as conn:
        conn.execute("""
            INSERT INTO deal_meeting (deal_id, meeting_id, meeting_label, meeting_calendar)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET meeting_id = excluded.meeting_id,
                                                meeting_label = excluded.meeting_label,
                                                meeting_calendar = excluded.meeting_calendar
        """, [deal_id, meeting_id, meeting_label, meeting_calendar])
    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/archive", methods=["POST"])
@login_required
def archive_deal(deal_id):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO deal_archive (deal_id, archived_at) VALUES (?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET archived_at = excluded.archived_at
        """, [deal_id, datetime.now(timezone.utc).isoformat()])
    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/unarchive", methods=["POST"])
@login_required
def unarchive_deal(deal_id):
    with get_db() as conn:
        conn.execute("DELETE FROM deal_archive WHERE deal_id = ?", [deal_id])
    return jsonify({"ok": True})


@app.route("/api/settings/email-template")
@login_required
def get_email_template():
    calendar = request.args.get("calendar", SCHEDULING_EMAIL)
    return jsonify({"template": prep_template_for(calendar)})


@app.route("/api/settings/email-template", methods=["POST"])
@login_required
def set_email_template():
    body = request.get_json()
    calendar = body.get("calendar", "")
    if calendar not in {c["email"] for c in CONNECTED_CALENDARS}:
        return jsonify({"error": "Unknown or missing calendar"}), 400
    set_setting(f"email_intro_template::{calendar}", body.get("template", ""))
    return jsonify({"ok": True})


@app.route("/api/settings/email-subject")
@login_required
def get_email_subject():
    calendar = request.args.get("calendar", SCHEDULING_EMAIL)
    return jsonify({"subject": prep_subject_for(calendar)})


@app.route("/api/settings/email-subject", methods=["POST"])
@login_required
def set_email_subject():
    body = request.get_json()
    calendar = body.get("calendar", "")
    if calendar not in {c["email"] for c in CONNECTED_CALENDARS}:
        return jsonify({"error": "Unknown or missing calendar"}), 400
    set_setting(f"email_intro_subject::{calendar}", body.get("subject", ""))
    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/send-prep-email", methods=["POST"])
@login_required
def send_prep_email(deal_id):
    if not get_confirmed_meeting(deal_id):
        return jsonify({"error": "Complete Step 3 (add the team to the meeting invite) before sending the prep email"}), 400

    body = request.get_json()
    to      = [e for e in body.get("to", []) if e]
    cc      = [e for e in body.get("cc", []) if e]
    subject = body.get("subject", "").strip()
    content = body.get("body", "")
    if not to:
        return jsonify({"error": "At least one recipient required"}), 400
    if not subject:
        return jsonify({"error": "Subject required"}), 400

    token = get_ms_token()
    if not token:
        return jsonify({"error": "Could not get Microsoft token"}), 500

    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": content},
        "toRecipients": [{"emailAddress": {"address": e}} for e in to],
        "bccRecipients": [{"emailAddress": {"address": SEND_CONFIRM_BCC}}],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": e}} for e in cc]

    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{PREP_EMAIL_FROM}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": "true"},
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    with get_db() as conn:
        conn.execute("""
            INSERT INTO deal_prep_email (deal_id, sent_at) VALUES (?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET sent_at = excluded.sent_at
        """, [deal_id, datetime.now(timezone.utc).isoformat()])

    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/contact")
@login_required
def get_deal_contact(deal_id):
    resp = requests.get(
        f"{BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts",
        headers=HEADERS,
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    results = resp.json().get("results", [])
    if not results:
        return jsonify({"email": "", "name": ""})

    contact_id = results[0]["toObjectId"]
    contact_resp = requests.get(
        f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}",
        headers=HEADERS,
        params={"properties": "email,firstname,lastname"},
    )
    if not contact_resp.ok:
        return jsonify({"error": contact_resp.text}), contact_resp.status_code

    props = contact_resp.json().get("properties", {})
    name = f"{props.get('firstname','')} {props.get('lastname','')}".strip()
    return jsonify({
        "email": props.get("email", "") or "",
        "name": name,
        "first_name": props.get("firstname", "") or "",
    })


@app.route("/api/settings/handoff-email-template")
@login_required
def get_handoff_email_template():
    calendar = request.args.get("calendar", SCHEDULING_EMAIL)
    return jsonify({"template": handoff_template_for(calendar)})


@app.route("/api/settings/handoff-email-template", methods=["POST"])
@login_required
def set_handoff_email_template():
    body = request.get_json()
    calendar = body.get("calendar", "")
    if calendar not in {c["email"] for c in CONNECTED_CALENDARS}:
        return jsonify({"error": "Unknown or missing calendar"}), 400
    set_setting(f"handoff_email_template::{calendar}", body.get("template", ""))
    return jsonify({"ok": True})


@app.route("/api/settings/handoff-email-subject")
@login_required
def get_handoff_email_subject():
    calendar = request.args.get("calendar", SCHEDULING_EMAIL)
    return jsonify({"subject": handoff_subject_for(calendar)})


@app.route("/api/settings/handoff-email-subject", methods=["POST"])
@login_required
def set_handoff_email_subject():
    body = request.get_json()
    calendar = body.get("calendar", "")
    if calendar not in {c["email"] for c in CONNECTED_CALENDARS}:
        return jsonify({"error": "Unknown or missing calendar"}), 400
    set_setting(f"handoff_email_subject::{calendar}", body.get("subject", ""))
    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/send-handoff-email", methods=["POST"])
@login_required
def send_handoff_email(deal_id):
    meeting = get_confirmed_meeting(deal_id)
    if not meeting:
        return jsonify({"error": "Complete Step 3 (add the team to the meeting invite) before sending the handoff email"}), 400
    # Customer-facing, so it needs to come from whichever calendar mailbox
    # the meeting was actually booked on — not a fixed address — to match
    # the brand the customer already booked/received a calendar invite from.
    from_calendar = meeting["calendar"]
    if from_calendar not in {c["email"] for c in CONNECTED_CALENDARS}:
        return jsonify({"error": "Could not determine which calendar this meeting is on"}), 400

    body = request.get_json()
    to      = [e for e in body.get("to", []) if e]
    cc      = [e for e in body.get("cc", []) if e]
    subject = body.get("subject", "").strip()
    content = body.get("body", "")
    if not to:
        return jsonify({"error": "At least one recipient required"}), 400
    if not subject:
        return jsonify({"error": "Subject required"}), 400

    token = get_ms_token()
    if not token:
        return jsonify({"error": "Could not get Microsoft token"}), 500

    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": content},
        "toRecipients": [{"emailAddress": {"address": e}} for e in to],
        "bccRecipients": [{"emailAddress": {"address": SEND_CONFIRM_BCC}}],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": e}} for e in cc]

    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{from_calendar}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": "true"},
    )
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    with get_db() as conn:
        conn.execute("""
            INSERT INTO deal_handoff_email (deal_id, sent_at) VALUES (?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET sent_at = excluded.sent_at
        """, [deal_id, datetime.now(timezone.utc).isoformat()])

    return jsonify({"ok": True})


@app.route("/settings")
@login_required
def settings():
    # Fetch rs_partner options from HubSpot
    client_options = []
    resp = requests.get(f"{BASE_URL}/crm/v3/properties/deals/rs_partner", headers=HEADERS)
    if resp.ok:
        client_options = [
            {"label": o["label"], "value": o["value"]}
            for o in resp.json().get("options", [])
            if not o.get("hidden")
        ]
    client_options.sort(key=lambda x: x["label"].lower())

    # Load saved contacts from DB
    with get_db() as conn:
        rows = {r["client_value"]: dict(r) for r in conn.execute("SELECT * FROM client_contacts")}

    client_eligibility = get_client_eligibility()
    for c in client_options:
        saved = rows.get(c["value"], {})
        c["email1"] = saved.get("email1", "")
        c["email2"] = saved.get("email2", "")
        c["email3"] = saved.get("email3", "")
        c["name1"]  = saved.get("name1", "")
        c["name2"]  = saved.get("name2", "")
        c["name3"]  = saved.get("name3", "")
        c["meeting_quota"] = saved.get("meeting_quota") or ""
        c["enabled"] = client_eligibility.get(c["value"], True)

    owners = fetch_hubspot_owners()
    eligibility = get_owner_eligibility()
    for o in owners:
        e = eligibility.get(o["id"], {"am": True, "se": True, "sd": True})
        o["eligible_am"] = e["am"]
        o["eligible_se"] = e["se"]
        o["eligible_sd"] = e["sd"]

    default_calendar = CONNECTED_CALENDARS[0]["email"] if CONNECTED_CALENDARS else ""
    email_template = prep_template_for(default_calendar) if default_calendar else DEFAULT_EMAIL_TEMPLATE
    handoff_email_template = handoff_template_for(default_calendar) if default_calendar else DEFAULT_HANDOFF_EMAIL_TEMPLATE
    email_subject = prep_subject_for(default_calendar) if default_calendar else DEFAULT_EMAIL_SUBJECT_TEMPLATE
    handoff_email_subject = handoff_subject_for(default_calendar) if default_calendar else DEFAULT_HANDOFF_SUBJECT_TEMPLATE

    return render_template(
        "settings.html",
        client_options=client_options,
        owners=owners,
        connected_calendars=CONNECTED_CALENDARS,
        default_calendar=default_calendar,
        email_template=email_template,
        handoff_email_template=handoff_email_template,
        email_subject=email_subject,
        handoff_email_subject=handoff_email_subject,
    )


@app.route("/api/clients/<client_value>", methods=["POST"])
@login_required
def save_client(client_value):
    body = request.get_json()
    emails = [body.get(f"email{i}", "").strip() for i in range(1, 4)]
    names  = [body.get(f"name{i}", "").strip() for i in range(1, 4)]
    try:
        meeting_quota = int(body.get("meeting_quota") or 0)
    except (TypeError, ValueError):
        meeting_quota = 0
    with get_db() as conn:
        conn.execute("""
            INSERT INTO client_contacts (client_value, email1, email2, email3, name1, name2, name3, meeting_quota)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_value) DO UPDATE SET
                email1 = excluded.email1,
                email2 = excluded.email2,
                email3 = excluded.email3,
                name1 = excluded.name1,
                name2 = excluded.name2,
                name3 = excluded.name3,
                meeting_quota = excluded.meeting_quota
        """, [client_value] + emails + names + [meeting_quota])
    return jsonify({"ok": True})


@app.route("/api/clients/<client_value>/eligibility", methods=["POST"])
@login_required
def set_client_eligibility(client_value):
    body = request.get_json()
    enabled = 1 if body.get("enabled") else 0
    with get_db() as conn:
        conn.execute("""
            INSERT INTO client_eligibility (client_value, enabled)
            VALUES (?, ?)
            ON CONFLICT(client_value) DO UPDATE SET enabled = excluded.enabled
        """, [client_value, enabled])
    return jsonify({"ok": True})


@app.route("/api/clients/<client_value>", methods=["GET"])
@login_required
def get_client(client_value):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM client_contacts WHERE client_value = ?", [client_value]
        ).fetchone()
    if row:
        return jsonify({
            "emails": [row["email1"], row["email2"], row["email3"]],
            "names":  [row["name1"], row["name2"], row["name3"]],
        })
    return jsonify({"emails": ["", "", ""], "names": ["", "", ""]})



# ---------------------------------------------------------------------------
# HS Workflows tab — read-only visual map of every HubSpot automation flow
# (Automation v4 API), plus best-effort structural issue detection. Nothing
# here is cached to disk; it's rebuilt from HubSpot on each cache expiry.
# ---------------------------------------------------------------------------

OBJECT_TYPE_LABELS = {"0-1": "Contact", "0-2": "Company", "0-3": "Deal", "0-5": "Ticket"}

# Short reference numbers Andrew uses when discussing these workflows day to
# day — not a HubSpot concept, just this app's own naming so "WF1"/"WF4"/etc.
# mean the same thing in conversation as they do here. Only the actively
# maintained deal-creation workflows get one; everything else in the portal
# (old drip campaigns, per-client outreach clones) is unlabeled.
WF_REFERENCE = {
    "616273312": {
        "label": "WF1",
        "description": "Main deal-creation router. Fires on any meeting booked via a HubSpot meetings link, "
                        "then routes across 17 branches (one per known booking link) to set the right owner, "
                        "deal stage, and brand, and sends internal email + Slack notifications.",
    },
    "1669757204": {
        "label": "WF2",
        "description": "New Lead > Create Deal. Broken — its trigger form was deleted at some point, so this "
                        "workflow has never actually fired (0 enrollments).",
    },
    "1834480158": {
        "label": "WF3",
        "description": "Safety net for contacts who already have a deal and book a second meeting — notifies "
                        "the team without creating a duplicate deal.",
    },
    "1835860399": {
        "label": "WF4",
        "description": "Roslyn Yee's dedicated workflow. Creates a Runway Selling deal when a contact books "
                        "via either of her two meeting links (Call/Email or LinkedIn).",
    },
    "1835954567": {
        "label": "WF5",
        "description": "Cameron Whitmore's dedicated workflow. Creates a 10talent Tech deal (rs_partner = "
                        "\"Pending Assignment\") when a contact books via his meeting link.",
    },
    "1846895500": {
        "label": "WF6",
        "description": "Patrick Leddy's dedicated workflow. Creates a Runway Selling deal when a contact "
                        "books via his LinkedIn meeting link.",
    },
}

# Custom behavioral event IDs seen in this portal's meeting-booking triggers
# (see project_hubspot_workflows memory) — used only to make trigger text
# readable and to flag the known WF4-style duplicate-enrollment bug pattern.
KNOWN_EVENT_TYPES = {
    "4-1720599": "Meeting booked via meetings link",
    "4-1639801": "Meeting associated with contact",
}

_HS_WORKFLOWS_CACHE = {"data": None, "ts": 0}
HS_WORKFLOWS_CACHE_TTL = 600  # seconds


def get_all_flows():
    flows = []
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        resp = requests.get(f"{BASE_URL}/automation/v4/flows", headers=HEADERS, params=params)
        if not resp.ok:
            break
        data = resp.json()
        flows.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return flows


def get_flow_detail(flow_id):
    resp = requests.get(f"{BASE_URL}/automation/v4/flows/{flow_id}", headers=HEADERS)
    if not resp.ok:
        return None
    return resp.json()


def _describe_filter(f):
    ftype = f.get("filterType")
    if ftype == "FORM_SUBMISSION":
        form_id = f.get("formId", "")
        return f"form submitted ({form_id[:8]}…)"
    if ftype == "PROPERTY":
        prop = f.get("property", "?")
        op = f.get("operation", {}) or {}
        operator = op.get("operator", "")
        values = op.get("values") or []
        if values:
            return f"{prop} {operator} {', '.join(str(v) for v in values)}"
        return f"{prop} {operator}".strip()
    return ftype or "filter"


def _describe_filter_branch(fb):
    if not fb:
        return ""
    parts = [_describe_filter(f) for f in fb.get("filters", [])]
    for nested in fb.get("filterBranches", []):
        text = _describe_filter_branch(nested)
        if text:
            parts.append(f"({text})")
    joiner = f" {fb.get('filterBranchOperator', 'AND')} "
    return joiner.join(p for p in parts if p)


def _describe_trigger(criteria):
    if not criteria:
        return "(no enrollment trigger data)", []
    issues = []
    parts = []

    event_branches = criteria.get("eventFilterBranches") or []
    for fb in event_branches:
        event_id = fb.get("eventTypeId")
        event_label = KNOWN_EVENT_TYPES.get(event_id, event_id or "event")
        op = fb.get("operator", "")
        cond_text = _describe_filter_branch(fb)
        piece = event_label + (f" ({op})" if op else "")
        if cond_text:
            piece += f" — where {cond_text}"
        parts.append(piece)

    if criteria.get("listFilterBranch"):
        text = _describe_filter_branch(criteria["listFilterBranch"])
        if text:
            parts.append(text)

    if not parts:
        parts.append((criteria.get("type") or "Unknown trigger").replace("_", " ").title())

    summary = " OR ".join(p for p in parts if p)
    if criteria.get("shouldReEnroll"):
        summary += " · re-enrolls every time"

    event_ids = {fb.get("eventTypeId") for fb in event_branches}
    if {"4-1639801", "4-1720599"}.issubset(event_ids):
        issues.append(
            "Enrollment trigger fires on both 'Meeting booked' and 'Meeting associated with "
            "contact' — known bug pattern that causes duplicate enrollments per meeting"
        )

    return summary, issues


def _describe_action(a):
    atid = a.get("actionTypeId") or ""
    fields = a.get("fields", {}) or {}
    issues = []
    if atid == "0-1":
        delta = fields.get("delta", "?")
        unit = (fields.get("time_unit") or "").lower()
        return f"Delay {delta} {unit}".strip(), "", issues
    if atid == "0-8":
        subject = fields.get("subject", "")
        n = len(fields.get("user_ids") or [])
        if n == 0:
            issues.append(f"Send-email action \"{subject or a.get('actionId')}\" has no recipients configured")
        return "Send internal email", f'"{subject}" to {n} user(s)', issues
    if atid == "0-14":
        obj = fields.get("object_type_id", "")
        obj_label = OBJECT_TYPE_LABELS.get(obj, obj)
        props = fields.get("properties") or []
        return f"Create {obj_label.lower()} record", f"sets {len(props)} propert{'y' if len(props) == 1 else 'ies'}", issues
    if atid == "0-5":
        prop = fields.get("property_name", "?")
        return "Set property", prop, issues
    if atid.startswith("1-"):
        if "slackChannelIds" in fields or "slackUserIds" in fields:
            chans = fields.get("slackChannelIds") or []
            users = fields.get("slackUserIds") or []
            if not chans and not users:
                issues.append("Slack notification action has no channel or user configured")
            return "Send Slack message", f"{len(chans)} channel(s), {len(users)} user(s)", issues
        return "Custom action", f"type {atid}", issues
    return "Action", f"type {atid or a.get('type', '?')}", issues


def _walk_flow(actions, start_id):
    by_id = {a["actionId"]: a for a in actions if a.get("actionId")}
    issues = []

    def walk(action_id, visited):
        if not action_id:
            return None
        if action_id in visited:
            issues.append(f"Loop detected — action {action_id} connects back to an earlier step")
            return None
        if action_id not in by_id:
            issues.append(f"Broken link — step references action {action_id}, which does not exist")
            return None
        visited = visited | {action_id}
        a = by_id[action_id]
        node = {"id": action_id}
        if a.get("type") == "LIST_BRANCH":
            branch_list = a.get("listBranches", []) or []
            branches = []
            for b in branch_list:
                condition = _describe_filter_branch(b.get("filterBranch") or {})
                child = walk((b.get("connection") or {}).get("nextActionId"), visited)
                branches.append({
                    "name": b.get("branchName") or "(unnamed branch)",
                    "condition": condition,
                    "step": child,
                })
            node.update({
                "label": "Branch",
                "detail": f"{len(branch_list)} branch(es)",
                "branches": branches,
                "next": None,
            })
            if len(branch_list) >= 17:
                issues.append(f"Branch step ({action_id}) has {len(branch_list)} branches — at HubSpot's 17-branch hard limit")
            elif len(branch_list) >= 15:
                issues.append(f"Branch step ({action_id}) has {len(branch_list)} branches — approaching the 17-branch limit")
        else:
            label, detail, extra_issues = _describe_action(a)
            issues.extend(extra_issues)
            node.update({
                "label": label,
                "detail": detail,
                "branches": None,
                "next": walk((a.get("connection") or {}).get("nextActionId"), visited),
            })
        return node

    root = walk(start_id, set())
    return root, issues


def build_flow_summary(flow_entry, detail):
    wf_ref = WF_REFERENCE.get(flow_entry["id"], {})

    if not detail:
        return {
            "id": flow_entry["id"],
            "name": flow_entry.get("name") or "(unnamed)",
            "wf_label": wf_ref.get("label"),
            "wf_description": wf_ref.get("description"),
            "enabled": flow_entry.get("isEnabled", False),
            "object_type": OBJECT_TYPE_LABELS.get(flow_entry.get("objectTypeId"), flow_entry.get("objectTypeId", "")),
            "updated_at": flow_entry.get("updatedAt", ""),
            "trigger": "(could not load workflow detail)",
            "steps": None,
            "issues": ["Could not load this workflow's detail from HubSpot"],
            "issue_count": 1,
        }

    trigger_text, trigger_issues = _describe_trigger(detail.get("enrollmentCriteria"))
    issues = list(trigger_issues)

    actions = detail.get("actions", []) or []
    start_id = detail.get("startActionId")
    by_id = {a["actionId"]: a for a in actions if a.get("actionId")}
    if start_id and start_id not in by_id:
        issues.append(f"Start action {start_id} is missing — this workflow cannot run")

    steps, walk_issues = _walk_flow(actions, start_id)
    issues.extend(walk_issues)

    return {
        "id": flow_entry["id"],
        "name": flow_entry.get("name") or "(unnamed)",
        "wf_label": wf_ref.get("label"),
        "wf_description": wf_ref.get("description"),
        "enabled": flow_entry.get("isEnabled", False),
        "object_type": OBJECT_TYPE_LABELS.get(flow_entry.get("objectTypeId"), flow_entry.get("objectTypeId", "")),
        "updated_at": flow_entry.get("updatedAt", ""),
        "trigger": trigger_text,
        "steps": steps,
        "issues": issues,
        "issue_count": len(issues),
    }


@app.route("/hs-workflows")
@login_required
def hs_workflows_page():
    return render_template("hs_workflows.html")


@app.route("/api/hs-workflows")
@login_required
def api_hs_workflows():
    force = request.args.get("refresh") == "1"
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _HS_WORKFLOWS_CACHE["data"]
    if not force and cached and (now_ts - _HS_WORKFLOWS_CACHE["ts"] < HS_WORKFLOWS_CACHE_TTL):
        return jsonify(cached)

    flows = get_all_flows()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        details = list(ex.map(lambda f: get_flow_detail(f["id"]), flows))

    summaries = [build_flow_summary(f, d) for f, d in zip(flows, details)]

    def sort_key(s):
        wf_num = int(s["wf_label"][2:]) if s["wf_label"] else 999
        return (wf_num, not s["enabled"], s["name"].lower())

    summaries.sort(key=sort_key)

    payload = {"workflows": summaries, "generated_at": datetime.now(timezone.utc).isoformat()}
    _HS_WORKFLOWS_CACHE["data"] = payload
    _HS_WORKFLOWS_CACHE["ts"] = now_ts
    return jsonify(payload)


# ============================================================================
# Pull Contact Data — Apollo ICP search -> enrich -> push to HubSpot
# ============================================================================

APOLLO_WEBHOOK_SECRET = os.environ.get("APOLLO_WEBHOOK_SECRET", secrets.token_hex(16))
# Hardcoded rather than read from RAILWAY_PUBLIC_DOMAIN -- that env var can
# report one of this service's other custom domains (e.g. zap.runwayselling.app)
# depending on deploy order, which would be a confusing thing to see in a
# webhook URL even though it would still work (same app, any hostname).
PULL_CONTACTS_HOST = os.environ.get("PULL_CONTACTS_HOST", "https://dealallocation.runwayselling.app")

_HUBSPOT_OPTIONS_CACHE = {"data": None, "ts": 0}
_HUBSPOT_OPTIONS_CACHE_TTL = 3600


@app.route("/api/pull-contacts/hubspot-options")
@login_required
def pull_contacts_hubspot_options():
    now_ts = datetime.now(timezone.utc).timestamp()
    if _HUBSPOT_OPTIONS_CACHE["data"] and (now_ts - _HUBSPOT_OPTIONS_CACHE["ts"] < _HUBSPOT_OPTIONS_CACHE_TTL):
        return jsonify(_HUBSPOT_OPTIONS_CACHE["data"])

    result = {}
    for prop in ("sales_focus", "lead_source", "custom_industry"):
        resp = requests.get(f"{BASE_URL}/crm/v3/properties/contacts/{prop}", headers=HEADERS)
        options = resp.json().get("options", []) if resp.ok else []
        result[prop] = [{"label": o["label"], "value": o["value"]} for o in options]

    _HUBSPOT_OPTIONS_CACHE["data"] = result
    _HUBSPOT_OPTIONS_CACHE["ts"] = now_ts
    return jsonify(result)


def _candidate_row_to_dict(row):
    return {
        "id": row["id"],
        "apollo_person_id": row["apollo_person_id"],
        "apollo_contact_id": row["apollo_contact_id"],
        "organization_id": row["organization_id"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "title": row["title"],
        "company_name": row["company_name"],
        "keep": bool(row["keep"]),
        "enrich_status": row["enrich_status"],
        "email": row["email"],
        "email_status": row["email_status"],
        "linkedin_url": row["linkedin_url"],
        "mobile_phone": row["mobile_phone"],
        "mobile_status": row["mobile_status"],
        "company_phone": row["company_phone"],
        "website": row["website"],
        "company_linkedin_url": row["company_linkedin_url"],
        "company_address": row["company_address"],
        "state": row["state"],
        "city": row["city"],
        "employees": row["employees"],
        "annual_revenue": row["annual_revenue"],
        "industry": row["industry"],
        "time_zone": row["time_zone"],
        "hubspot_contact_id": row["hubspot_contact_id"],
        "pushed_at": row["pushed_at"],
    }


@app.route("/pull-contacts")
@login_required
def pull_contacts_page():
    return render_template("pull_contacts.html")


@app.route("/api/pull-contacts/searches")
@login_required
def list_saved_searches():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM saved_searches ORDER BY name").fetchall()
    return jsonify({"searches": [
        {"id": r["id"], "name": r["name"], "criteria": json.loads(r["criteria_json"])}
        for r in rows
    ]})


@app.route("/api/pull-contacts/searches", methods=["POST"])
@login_required
def save_search():
    body = request.get_json()
    name = (body.get("name") or "").strip()
    criteria = body.get("criteria") or {}
    if not name:
        return jsonify({"error": "name is required"}), 400
    with get_db() as conn:
        conn.execute("""
            INSERT INTO saved_searches (name, criteria_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET criteria_json = excluded.criteria_json
        """, [name, json.dumps(criteria), datetime.now(timezone.utc).isoformat()])
    return jsonify({"ok": True})


@app.route("/api/pull-contacts/searches/<int:search_id>", methods=["DELETE"])
@login_required
def delete_saved_search(search_id):
    with get_db() as conn:
        conn.execute("DELETE FROM saved_searches WHERE id = ?", [search_id])
    return jsonify({"ok": True})


@app.route("/api/pull-contacts/run", methods=["POST"])
@login_required
def run_pull_search():
    body = request.get_json()
    criteria = body.get("criteria") or {}
    search_name = body.get("search_name", "")

    result = apollo.search_apollo_all(criteria)

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO pull_batches (search_name, criteria_json, stage, created_at, total_entries) VALUES (?, ?, 'scrubbing', ?, ?)",
            [search_name, json.dumps(criteria), datetime.now(timezone.utc).isoformat(), result["total_entries"]]
        )
        batch_id = cur.lastrowid
        for c in result["candidates"]:
            conn.execute("""
                INSERT INTO pull_candidates
                    (batch_id, apollo_person_id, first_name, last_name, title, company_name)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [batch_id, c["apollo_person_id"], c["first_name"], c["last_name"],
                  c["title"], c["company_name"]])
        rows = conn.execute("SELECT * FROM pull_candidates WHERE batch_id = ?", [batch_id]).fetchall()

    return jsonify({
        "batch_id": batch_id,
        "total_entries": result["total_entries"],
        "truncated": result.get("truncated", False),
        "candidates": [_candidate_row_to_dict(r) for r in rows],
    })


@app.route("/api/pull-contacts/batches")
@login_required
def list_pull_batches():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.id, b.search_name, b.stage, b.created_at, b.total_entries,
                   COUNT(c.id) AS total,
                   SUM(CASE WHEN c.keep = 1 THEN 1 ELSE 0 END) AS kept,
                   SUM(CASE WHEN c.pushed_at IS NOT NULL THEN 1 ELSE 0 END) AS pushed
            FROM pull_batches b
            LEFT JOIN pull_candidates c ON c.batch_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 25
        """).fetchall()
    return jsonify({"batches": [
        {
            "id": r["id"],
            "search_name": r["search_name"] or "Untitled pull",
            "stage": r["stage"],
            "created_at": r["created_at"],
            "total_entries": r["total_entries"] or 0,
            "total": r["total"] or 0,
            "kept": r["kept"] or 0,
            "pushed": r["pushed"] or 0,
        } for r in rows
    ]})


@app.route("/api/pull-contacts/batches/<int:batch_id>")
@login_required
def get_pull_batch(batch_id):
    with get_db() as conn:
        batch = conn.execute("SELECT * FROM pull_batches WHERE id = ?", [batch_id]).fetchone()
        if not batch:
            return jsonify({"error": "not found"}), 404
        rows = conn.execute("SELECT * FROM pull_candidates WHERE batch_id = ?", [batch_id]).fetchall()
    return jsonify({
        "batch_id": batch_id,
        "stage": batch["stage"],
        "search_name": batch["search_name"],
        "criteria": json.loads(batch["criteria_json"]) if batch["criteria_json"] else {},
        "total_entries": batch["total_entries"] or 0,
        "candidates": [_candidate_row_to_dict(r) for r in rows],
    })


@app.route("/api/pull-contacts/batches/<int:batch_id>/toggle", methods=["POST"])
@login_required
def toggle_pull_candidates(batch_id):
    body = request.get_json()
    candidate_ids = body.get("candidate_ids") or []
    keep = 1 if body.get("keep") else 0
    with get_db() as conn:
        conn.executemany(
            "UPDATE pull_candidates SET keep = ? WHERE id = ? AND batch_id = ?",
            [(keep, cid, batch_id) for cid in candidate_ids]
        )
    return jsonify({"ok": True})


def _run_enrich_batch(batch_id):
    """Does the actual Apollo reveal + org enrichment work. Runs in a
    background thread (kicked off by enrich_pull_batch below) so a batch of
    dozens/hundreds of candidates can't blow past the gunicorn request
    timeout -- that previously killed the worker mid-loop and, since the
    whole loop shared one uncommitted transaction, silently discarded every
    row it had already enriched. Each row now commits on its own connection
    as soon as it's done, so a crash here only loses the one row in flight.
    """
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pull_candidates WHERE batch_id = ? AND keep = 1", [batch_id]
        ).fetchall()]

        webhook_url = f"{PULL_CONTACTS_HOST}/api/pull-contacts/apollo-webhook?key={APOLLO_WEBHOOK_SECRET}"

        def reveal_one(r):
            try:
                revealed = apollo.reveal_email(r["apollo_person_id"])
            except requests.RequestException:
                return (r["id"], r, None)
            try:
                apollo.submit_phone_reveal(r["apollo_person_id"], webhook_url)
            except requests.RequestException:
                pass
            return (r["id"], r, revealed)

        org_ids_needed = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for cand_id, r, revealed in pool.map(reveal_one, rows):
                if revealed is None:
                    conn.execute("UPDATE pull_candidates SET enrich_status = 'failed' WHERE id = ?", [cand_id])
                    conn.commit()
                    continue
                conn.execute("""
                    UPDATE pull_candidates SET
                        email = ?, email_status = ?, linkedin_url = ?, apollo_contact_id = ?,
                        organization_id = ?, time_zone = ?, first_name = ?, last_name = ?,
                        enrich_status = 'enriching'
                    WHERE id = ?
                """, [revealed["email"], revealed["email_status"], revealed["linkedin_url"],
                      revealed["apollo_contact_id"], revealed["organization_id"], revealed["time_zone"],
                      revealed["first_name"] or r["first_name"], revealed["last_name"] or r["last_name"],
                      cand_id])
                conn.commit()
                if revealed["organization_id"]:
                    org_ids_needed.add(revealed["organization_id"])

        # Company enrichment, one call per org (bulk_enrich needs a domain we
        # don't have yet at this point, so use the per-id lookup)
        for org_id in org_ids_needed:
            org = apollo.enrich_org_by_id(org_id)
            if not org:
                continue
            conn.execute("""
                UPDATE pull_candidates SET
                    company_phone = ?, website = ?, company_linkedin_url = ?,
                    company_address = ?, state = ?, city = ?, employees = ?,
                    annual_revenue = ?, industry = ?
                WHERE batch_id = ? AND organization_id = ?
            """, [org.get("sanitized_phone", ""), org.get("website_url", ""),
                  org.get("linkedin_url", ""), org.get("raw_address", "") or org.get("street_address", ""),
                  org.get("state", ""), org.get("city", ""), org.get("estimated_num_employees"),
                  org.get("organization_revenue"), org.get("industry", ""),
                  batch_id, org_id])
            conn.commit()

        conn.execute(
            "UPDATE pull_candidates SET enrich_status = 'done' WHERE batch_id = ? AND keep = 1 AND enrich_status = 'enriching'",
            [batch_id]
        )
        conn.commit()
    finally:
        conn.close()


@app.route("/api/pull-contacts/batches/<int:batch_id>/enrich", methods=["POST"])
@login_required
def enrich_pull_batch(batch_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE pull_candidates SET enrich_status = 'enriching' WHERE batch_id = ? AND keep = 1",
            [batch_id]
        )
        conn.execute("UPDATE pull_batches SET stage = 'enriching' WHERE id = ?", [batch_id])
        rows = conn.execute("SELECT * FROM pull_candidates WHERE batch_id = ?", [batch_id]).fetchall()

    threading.Thread(target=_run_enrich_batch, args=(batch_id,), daemon=True).start()

    return jsonify({"candidates": [_candidate_row_to_dict(r) for r in rows]})


@app.route("/api/pull-contacts/apollo-webhook", methods=["POST"])
def apollo_phone_webhook():
    if request.args.get("key") != APOLLO_WEBHOOK_SECRET:
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    with get_db() as conn:
        for person in payload.get("people", []):
            person_id = person.get("id")
            if not person_id:
                continue
            mobiles = [p for p in (person.get("phone_numbers") or []) if p.get("type_cd") == "mobile"]
            if mobiles:
                number = mobiles[0].get("sanitized_number") or mobiles[0].get("raw_number")
                row = conn.execute(
                    "SELECT id, apollo_contact_id FROM pull_candidates WHERE apollo_person_id = ?", [person_id]
                ).fetchone()
                conn.execute(
                    "UPDATE pull_candidates SET mobile_phone = ?, mobile_status = 'found' WHERE apollo_person_id = ?",
                    [number, person_id]
                )
                if row and row["apollo_contact_id"]:
                    try:
                        apollo.persist_apollo_phone(row["apollo_contact_id"], number)
                    except requests.RequestException:
                        pass
            else:
                conn.execute(
                    "UPDATE pull_candidates SET mobile_status = 'not_found' WHERE apollo_person_id = ?",
                    [person_id]
                )
    return jsonify({"ok": True})


@app.route("/api/pull-contacts/batches/<int:batch_id>/filter-incomplete", methods=["POST"])
@login_required
def filter_incomplete_candidates(batch_id):
    body = request.get_json()
    required = body.get("require") or []  # subset of: email, mobile_phone, linkedin_url
    column_map = {"email": "email", "mobile_phone": "mobile_phone", "linkedin_url": "linkedin_url"}
    columns = [column_map[f] for f in required if f in column_map]
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM pull_candidates WHERE batch_id = ? AND keep = 1", [batch_id]).fetchall()
        for r in rows:
            complete = all(r[col] for col in columns)
            if not complete:
                conn.execute("UPDATE pull_candidates SET keep = 0 WHERE id = ?", [r["id"]])
        conn.execute("UPDATE pull_batches SET stage = 'ready' WHERE id = ?", [batch_id])
        updated_rows = conn.execute("SELECT * FROM pull_candidates WHERE batch_id = ?", [batch_id]).fetchall()
    return jsonify({"candidates": [_candidate_row_to_dict(r) for r in updated_rows]})


@app.route("/api/pull-contacts/batches/<int:batch_id>/push", methods=["POST"])
@login_required
def push_pull_batch(batch_id):
    body = request.get_json()
    sales_focus = body.get("sales_focus", "")
    lead_source = body.get("lead_source", "")
    custom_industry = body.get("custom_industry", "")
    owner_id = body.get("owner_id") or None
    create_tasks = bool(body.get("create_tasks"))
    task_due_iso = body.get("task_due_iso") or datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM pull_candidates WHERE batch_id = ? AND keep = 1", [batch_id]
        ).fetchall()
        candidates = [_candidate_row_to_dict(r) for r in rows]

        results = apollo.push_candidates_to_hubspot(candidates, sales_focus, lead_source, owner_id, custom_industry)

        pushed_at = datetime.now(timezone.utc).isoformat()
        contact_ids_with_state = []
        for c, result in zip([c for c in candidates if c["email"]], results):
            hs_id = result.get("id")
            if hs_id:
                conn.execute(
                    "UPDATE pull_candidates SET hubspot_contact_id = ?, pushed_at = ? WHERE id = ?",
                    [hs_id, pushed_at, c["id"]]
                )
                contact_ids_with_state.append((hs_id, c["state"]))

        task_ids = []
        if create_tasks and owner_id and contact_ids_with_state:
            task_ids = apollo.create_call_tasks(contact_ids_with_state, owner_id, task_due_iso)

        conn.execute("UPDATE pull_batches SET stage = 'pushed' WHERE id = ?", [batch_id])

    return jsonify({
        "pushed": len(contact_ids_with_state),
        "tasks_created": len(task_ids),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5050)
