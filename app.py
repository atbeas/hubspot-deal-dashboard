import os
import sqlite3
import secrets
import requests
from functools import wraps
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from datetime import datetime, timezone, timedelta

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

init_db()

MS_TENANT_ID     = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID     = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
SCHEDULING_EMAIL = "scheduling@10talent.tech"

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
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

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
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
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


@app.route("/api/owners")
@login_required
def get_owners():
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
                name = f"{o.get('firstName','')} {o.get('lastName','')}".strip()
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
    return jsonify({"owners": owners})


@app.route("/api/deals")
@login_required
def get_deals():
    start = request.args.get("start")
    end   = request.args.get("end")
    if not start or not end:
        return jsonify({"error": "start and end required"}), 400

    start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
    end_ms   = int(datetime.fromisoformat(end).timestamp() * 1000) + 86399999

    properties = [
        "dealname", "createdate", "dealstage", "pipeline", "amount", "hubspot_owner_id",
        "business_needs",
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
        })

    return jsonify({"deals": deals, "total": data.get("total", 0)})


@app.route("/api/deals/<deal_id>", methods=["PATCH"])
@login_required
def update_deal(deal_id):
    body = request.get_json()
    properties = {}
    for key, prop_name in ROLE_PROPS.items():
        if key in body:
            properties[prop_name] = body[key]
    if "business_needs" in body:
        properties["business_needs"] = body["business_needs"]

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
        try:
            dt = datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00"))
            label = f"{e['subject']} — {dt.strftime('%b %d, %Y %-I:%M %p')}"
        except Exception:
            label = e.get("subject", "(No subject)")
        meetings.append({"id": e["id"], "label": label})

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

    return render_template("settings.html", client_options=client_options)


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
