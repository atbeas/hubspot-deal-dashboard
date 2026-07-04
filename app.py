import os
import sqlite3
import secrets
import requests
from functools import wraps
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash, check_password_hash

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

init_db()

MS_TENANT_ID     = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID     = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
SCHEDULING_EMAIL = "scheduling@10talent.tech"
PREP_EMAIL_FROM  = "info@runwayselling.com"
SEND_CONFIRM_BCC = "info@runwayselling.com"
DEFAULT_EMAIL_TEMPLATE = (
    "Hi there,\n\n"
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
@login_required
def index():
    host = request.host.split(":")[0].lower()
    company = QUICK_NOTES_HOST_MAP.get(host)
    if company:
        return redirect(url_for("quick_notes", company=company))

    # Fetch rs_partner enum options
    client_options = []
    resp = requests.get(
        f"{BASE_URL}/crm/v3/properties/deals/rs_partner",
        headers=HEADERS,
    )
    if resp.ok:
        data = resp.json()
        client_options = [
            {"label": o["label"], "value": o["value"]}
            for o in data.get("options", [])
            if not o.get("hidden")
        ]

    return render_template("index.html", client_options=client_options)


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


def get_deal_meetings():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM deal_meeting").fetchall()
    return {r["deal_id"]: {"id": r["meeting_id"], "label": r["meeting_label"]} for r in rows}


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
                # Only one calendar is wired up today; once more are added, this
                # should reflect whichever calendar the meeting was actually found on.
                "calendar": SCHEDULING_EMAIL,
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
        {"propertyName": "pipeline",   "operator": "EQ",  "value": "default"},
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
            "meeting_id":    meeting.get("id", ""),
            "meeting_label": meeting.get("label", ""),
            "initial_meeting": initial_meeting_times.get(result["id"], {}).get("label", ""),
            "initial_meeting_start": initial_meeting_times.get(result["id"], {}).get("start_utc", ""),
            "initial_meeting_calendar": initial_meeting_times.get(result["id"], {}).get("calendar", ""),
            "prep_email_sent": result["id"] in prep_emails_sent,
            "handoff_email_sent": result["id"] in handoff_emails_sent,
            "archived":    is_archived,
            "archived_at": archived_at,
        })

    return jsonify({"deals": deals, "total": len(deals)})


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
    companies = []
    for key, cfg in QUICK_NOTES_COMPANIES.items():
        companies.append({
            "key": key,
            "label": cfg["label"],
            "has_custom_password": get_company_password_hash(key) is not None,
        })
    return render_template("admin.html", companies=companies)


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

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{SCHEDULING_EMAIL}/calendarView",
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
        return jsonify({"error": resp.text}), resp.status_code

    meetings = []
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
        meetings.append({"id": e["id"], "label": label, "start_utc": start_utc})

    return jsonify({"meetings": meetings})


@app.route("/api/meetings/<path:event_id>/add-attendees", methods=["POST"])
@login_required
def add_meeting_attendees(event_id):
    body   = request.get_json()
    emails = [e for e in body.get("emails", []) if e]
    if not emails:
        return jsonify({"error": "No emails provided"}), 400

    token = get_ms_token()
    ms_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Fetch current attendees so we don't wipe them
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{SCHEDULING_EMAIL}/events/{event_id}",
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
        f"https://graph.microsoft.com/v1.0/users/{SCHEDULING_EMAIL}/events/{event_id}",
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
    meeting_id    = body.get("meeting_id", "")
    meeting_label = body.get("meeting_label", "")
    if not meeting_id:
        return jsonify({"error": "meeting_id required"}), 400

    with get_db() as conn:
        conn.execute("""
            INSERT INTO deal_meeting (deal_id, meeting_id, meeting_label)
            VALUES (?, ?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET meeting_id = excluded.meeting_id,
                                                meeting_label = excluded.meeting_label
        """, [deal_id, meeting_id, meeting_label])
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
    return jsonify({"template": get_setting("email_intro_template", DEFAULT_EMAIL_TEMPLATE)})


@app.route("/api/settings/email-template", methods=["POST"])
@login_required
def set_email_template():
    body = request.get_json()
    set_setting("email_intro_template", body.get("template", ""))
    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/send-prep-email", methods=["POST"])
@login_required
def send_prep_email(deal_id):
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
    return jsonify({"template": get_setting("handoff_email_template", DEFAULT_HANDOFF_EMAIL_TEMPLATE)})


@app.route("/api/settings/handoff-email-template", methods=["POST"])
@login_required
def set_handoff_email_template():
    body = request.get_json()
    set_setting("handoff_email_template", body.get("template", ""))
    return jsonify({"ok": True})


@app.route("/api/deals/<deal_id>/send-handoff-email", methods=["POST"])
@login_required
def send_handoff_email(deal_id):
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
        f"https://graph.microsoft.com/v1.0/users/{SCHEDULING_EMAIL}/sendMail",
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

    for c in client_options:
        saved = rows.get(c["value"], {})
        c["email1"] = saved.get("email1", "")
        c["email2"] = saved.get("email2", "")
        c["email3"] = saved.get("email3", "")

    owners = fetch_hubspot_owners()
    eligibility = get_owner_eligibility()
    for o in owners:
        e = eligibility.get(o["id"], {"am": True, "se": True, "sd": True})
        o["eligible_am"] = e["am"]
        o["eligible_se"] = e["se"]
        o["eligible_sd"] = e["sd"]

    email_template = get_setting("email_intro_template", DEFAULT_EMAIL_TEMPLATE)
    handoff_email_template = get_setting("handoff_email_template", DEFAULT_HANDOFF_EMAIL_TEMPLATE)

    return render_template(
        "settings.html",
        client_options=client_options,
        owners=owners,
        email_template=email_template,
        handoff_email_template=handoff_email_template,
    )


@app.route("/api/clients/<client_value>", methods=["POST"])
@login_required
def save_client(client_value):
    body = request.get_json()
    emails = [body.get(f"email{i}", "").strip() for i in range(1, 4)]
    with get_db() as conn:
        conn.execute("""
            INSERT INTO client_contacts (client_value, email1, email2, email3)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(client_value) DO UPDATE SET
                email1 = excluded.email1,
                email2 = excluded.email2,
                email3 = excluded.email3
        """, [client_value] + emails)
    return jsonify({"ok": True})


@app.route("/api/clients/<client_value>", methods=["GET"])
@login_required
def get_client(client_value):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM client_contacts WHERE client_value = ?", [client_value]
        ).fetchone()
    if row:
        return jsonify({"emails": [row["email1"], row["email2"], row["email3"]]})
    return jsonify({"emails": ["", "", ""]})


CAMERON_OWNER_ID = "93829264"

def _day_range_ms(date_str):
    """Return (start_ms, end_ms) for a YYYY-MM-DD date in UTC."""
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(day.timestamp() * 1000)
    end_ms   = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    return start_ms, end_ms


@app.route("/cameron")
@login_required
def cameron():
    return render_template("cameron.html")


@app.route("/api/cameron/trends")
@login_required
def cameron_trends():
    days = int(request.args.get("days", 30))
    now = datetime.now(timezone.utc)
    start_day = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start_day.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # Use browser's timezone offset so day boundaries match the user's local time
    tz_offset_minutes = int(request.args.get("tz_offset", 0))
    local_tz = timezone(timedelta(minutes=tz_offset_minutes))

    def fetch_by_day(obj_type, ts_prop, extra_filters=None):
        filters = [
            {"propertyName": "hubspot_owner_id", "operator": "EQ",  "value": CAMERON_OWNER_ID},
            {"propertyName": ts_prop,            "operator": "GTE", "value": str(start_ms)},
            {"propertyName": ts_prop,            "operator": "LTE", "value": str(end_ms)},
        ]
        if extra_filters:
            filters.extend(extra_filters)
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": [ts_prop],
            "limit": 200,
        }
        results = []
        after = None
        while True:
            if after:
                payload["after"] = after
            resp = requests.post(f"{BASE_URL}/crm/v3/objects/{obj_type}/search", headers=HEADERS, json=payload)
            if not resp.ok:
                break
            data = resp.json()
            results.extend(data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after or len(results) >= 2000:
                break
        counts = {}
        for r in results:
            ts = r.get("properties", {}).get(ts_prop)
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                day = dt.astimezone(local_tz).strftime("%Y-%m-%d")
                counts[day] = counts.get(day, 0) + 1
            except Exception:
                pass
        return counts

    call_counts  = fetch_by_day("calls",  "hs_timestamp")
    email_counts = fetch_by_day("emails", "hs_timestamp")

    # LinkedIn: native LINKEDIN_MESSAGE communications, grouped by timestamp
    linkedin_counts = fetch_by_day("communications", "hs_timestamp",
                                   extra_filters=[{"propertyName": "hs_communication_channel_type",
                                                   "operator": "EQ", "value": "LINKEDIN_MESSAGE"}])

    now_local = now.astimezone(local_tz)
    labels, call_data, email_data, linkedin_data = [], [], [], []
    for i in range(days, -1, -1):
        day = (now_local - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(day)
        call_data.append(call_counts.get(day, 0))
        email_data.append(email_counts.get(day, 0))
        linkedin_data.append(linkedin_counts.get(day, 0))

    return jsonify({"labels": labels, "calls": call_data, "emails": email_data, "linkedin": linkedin_data})


@app.route("/api/cameron/calls")
@login_required
def cameron_calls():
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        start_ms, end_ms = _day_range_ms(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id", "operator": "EQ",  "value": CAMERON_OWNER_ID},
            {"propertyName": "hs_timestamp",     "operator": "GTE", "value": str(start_ms)},
            {"propertyName": "hs_timestamp",     "operator": "LTE", "value": str(end_ms)},
        ]}],
        "properties": [
            "hs_call_title", "hs_call_duration", "hs_call_status",
            "hs_call_direction", "hs_timestamp",
        ],
        "sorts": [{"propertyName": "hs_timestamp", "direction": "DESCENDING"}],
        "limit": 100,
    }

    resp = requests.post(f"{BASE_URL}/crm/v3/objects/calls/search", headers=HEADERS, json=payload)
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    calls = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        ts = p.get("hs_timestamp")
        try:
            time_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%-I:%M %p")
        except Exception:
            time_str = ""

        dur_ms = p.get("hs_call_duration")
        try:
            secs = int(dur_ms) // 1000
            duration = f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"
        except Exception:
            duration = ""

        calls.append({
            "id":        r["id"],
            "time":      time_str,
            "title":     p.get("hs_call_title") or "",
            "direction": p.get("hs_call_direction") or "",
            "duration":  duration,
            "status":    p.get("hs_call_status") or "",
        })

    return jsonify({"calls": calls, "total": len(calls)})


@app.route("/api/cameron/emails")
@login_required
def cameron_emails():
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        start_ms, end_ms = _day_range_ms(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id", "operator": "EQ",  "value": CAMERON_OWNER_ID},
            {"propertyName": "hs_timestamp",     "operator": "GTE", "value": str(start_ms)},
            {"propertyName": "hs_timestamp",     "operator": "LTE", "value": str(end_ms)},
        ]}],
        "properties": [
            "hs_email_subject", "hs_email_direction", "hs_email_status", "hs_timestamp",
        ],
        "sorts": [{"propertyName": "hs_timestamp", "direction": "DESCENDING"}],
        "limit": 100,
    }

    resp = requests.post(f"{BASE_URL}/crm/v3/objects/emails/search", headers=HEADERS, json=payload)
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    emails = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        ts = p.get("hs_timestamp")
        try:
            time_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%-I:%M %p")
        except Exception:
            time_str = ""

        emails.append({
            "id":        r["id"],
            "time":      time_str,
            "subject":   p.get("hs_email_subject") or "(No subject)",
            "direction": p.get("hs_email_direction") or "",
            "status":    p.get("hs_email_status") or "",
        })

    return jsonify({"emails": emails, "total": len(emails)})


@app.route("/api/cameron/linkedin")
@login_required
def cameron_linkedin():
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        start_ms, end_ms = _day_range_ms(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id",              "operator": "EQ",  "value": CAMERON_OWNER_ID},
            {"propertyName": "hs_communication_channel_type", "operator": "EQ",  "value": "LINKEDIN_MESSAGE"},
            {"propertyName": "hs_timestamp",                  "operator": "GTE", "value": str(start_ms)},
            {"propertyName": "hs_timestamp",                  "operator": "LTE", "value": str(end_ms)},
        ]}],
        "properties": [
            "hs_communication_channel_type", "hs_timestamp",
            "hs_communication_body", "hs_body_preview",
        ],
        "sorts": [{"propertyName": "hs_timestamp", "direction": "DESCENDING"}],
        "limit": 200,
    }

    resp = requests.post(f"{BASE_URL}/crm/v3/objects/communications/search", headers=HEADERS, json=payload)
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    messages = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        ts = p.get("hs_timestamp")
        try:
            time_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%-I:%M %p")
        except Exception:
            time_str = ""
        messages.append({
            "id":      r["id"],
            "time":    time_str,
            "preview": p.get("hs_body_preview") or p.get("hs_communication_body") or "—",
        })

    return jsonify({"messages": messages, "total": len(messages)})


# ── Roslyn Yee dashboard ──────────────────────────────────────────────────────

ROSLYN_OWNER_ID = "88244907"


def _roslyn_activity(obj_type, ts_prop, date_str, extra_props=None, extra_filters=None):
    """Fetch activity records for Roslyn for a given day."""
    start_ms, end_ms = _day_range_ms(date_str)
    filters = [
        {"propertyName": "hubspot_owner_id", "operator": "EQ",  "value": ROSLYN_OWNER_ID},
        {"propertyName": ts_prop,            "operator": "GTE", "value": str(start_ms)},
        {"propertyName": ts_prop,            "operator": "LTE", "value": str(end_ms)},
    ]
    if extra_filters:
        filters.extend(extra_filters)
    payload = {
        "filterGroups": [{"filters": filters}],
        "properties": [ts_prop] + (extra_props or []),
        "sorts": [{"propertyName": ts_prop, "direction": "DESCENDING"}],
        "limit": 200,
    }
    resp = requests.post(f"{BASE_URL}/crm/v3/objects/{obj_type}/search", headers=HEADERS, json=payload)
    return resp


@app.route("/roslyn")
@login_required
def roslyn():
    return render_template("roslyn.html")


@app.route("/api/roslyn/calls")
@login_required
def roslyn_calls():
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        resp = _roslyn_activity("calls", "hs_timestamp", date_str,
                                extra_props=["hs_call_title", "hs_call_duration", "hs_call_status", "hs_call_direction"])
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    calls = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        ts = p.get("hs_timestamp")
        try:
            time_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%-I:%M %p")
        except Exception:
            time_str = ""
        dur_ms = p.get("hs_call_duration")
        try:
            secs = int(dur_ms) // 1000
            duration = f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"
        except Exception:
            duration = ""
        calls.append({
            "id": r["id"], "time": time_str,
            "title": p.get("hs_call_title") or "",
            "direction": p.get("hs_call_direction") or "",
            "duration": duration,
            "status": p.get("hs_call_status") or "",
        })
    return jsonify({"calls": calls, "total": len(calls)})


@app.route("/api/roslyn/emails")
@login_required
def roslyn_emails():
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        resp = _roslyn_activity("emails", "hs_timestamp", date_str,
                                extra_props=["hs_email_subject", "hs_email_direction", "hs_email_status"])
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    emails = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        ts = p.get("hs_timestamp")
        try:
            time_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%-I:%M %p")
        except Exception:
            time_str = ""
        emails.append({
            "id": r["id"], "time": time_str,
            "subject": p.get("hs_email_subject") or "(No subject)",
            "direction": p.get("hs_email_direction") or "",
            "status": p.get("hs_email_status") or "",
        })
    return jsonify({"emails": emails, "total": len(emails)})


@app.route("/api/roslyn/linkedin")
@login_required
def roslyn_linkedin():
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        resp = _roslyn_activity(
            "communications", "hs_timestamp", date_str,
            extra_props=["hs_communication_channel_type", "hs_communication_body", "hs_body_preview"],
            extra_filters=[{"propertyName": "hs_communication_channel_type",
                            "operator": "EQ", "value": "LINKEDIN_MESSAGE"}],
        )
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    if not resp.ok:
        return jsonify({"error": resp.text}), resp.status_code

    messages = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        ts = p.get("hs_timestamp")
        try:
            time_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%-I:%M %p")
        except Exception:
            time_str = ""
        messages.append({
            "id": r["id"], "time": time_str,
            "preview": p.get("hs_body_preview") or p.get("hs_communication_body") or "—",
        })
    return jsonify({"messages": messages, "total": len(messages)})


@app.route("/api/roslyn/trends")
@login_required
def roslyn_trends():
    days = int(request.args.get("days", 30))
    now = datetime.now(timezone.utc)
    start_day = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start_day.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    tz_offset_minutes = int(request.args.get("tz_offset", 0))
    local_tz = timezone(timedelta(minutes=tz_offset_minutes))

    def fetch_by_day(obj_type, ts_prop, extra_filters=None):
        filters = [
            {"propertyName": "hubspot_owner_id", "operator": "EQ",  "value": ROSLYN_OWNER_ID},
            {"propertyName": ts_prop,            "operator": "GTE", "value": str(start_ms)},
            {"propertyName": ts_prop,            "operator": "LTE", "value": str(end_ms)},
        ]
        if extra_filters:
            filters.extend(extra_filters)
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": [ts_prop],
            "limit": 200,
        }
        results = []
        after = None
        while True:
            if after:
                payload["after"] = after
            resp = requests.post(f"{BASE_URL}/crm/v3/objects/{obj_type}/search", headers=HEADERS, json=payload)
            if not resp.ok:
                break
            data = resp.json()
            results.extend(data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after or len(results) >= 2000:
                break
        counts = {}
        for r in results:
            ts = r.get("properties", {}).get(ts_prop)
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                day = dt.astimezone(local_tz).strftime("%Y-%m-%d")
                counts[day] = counts.get(day, 0) + 1
            except Exception:
                pass
        return counts

    call_counts    = fetch_by_day("calls",          "hs_timestamp")
    email_counts   = fetch_by_day("emails",         "hs_timestamp")
    linkedin_counts = fetch_by_day("communications", "hs_timestamp",
                                   extra_filters=[{"propertyName": "hs_communication_channel_type",
                                                   "operator": "EQ", "value": "LINKEDIN_MESSAGE"}])

    now_local = now.astimezone(local_tz)
    labels, call_data, email_data, linkedin_data = [], [], [], []
    for i in range(days, -1, -1):
        day = (now_local - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(day)
        call_data.append(call_counts.get(day, 0))
        email_data.append(email_counts.get(day, 0))
        linkedin_data.append(linkedin_counts.get(day, 0))

    return jsonify({"labels": labels, "calls": call_data, "emails": email_data, "linkedin": linkedin_data})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
