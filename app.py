import os
import secrets
import requests
from functools import wraps
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from datetime import datetime, timezone, timedelta

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

    # Fetch deal pipelines
    pipeline_options = []
    resp = requests.get(f"{BASE_URL}/crm/v3/pipelines/deals", headers=HEADERS)
    if resp.ok:
        for p in resp.json().get("results", []):
            pipeline_options.append({"label": p["label"], "value": p["id"]})

    return render_template("index.html", client_options=client_options, pipeline_options=pipeline_options)


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
    start    = request.args.get("start")
    end      = request.args.get("end")
    pipeline = request.args.get("pipeline", "")
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
    ]
    if pipeline:
        filters.append({"propertyName": "pipeline", "operator": "EQ", "value": pipeline})

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


if __name__ == "__main__":
    app.run(debug=True, port=5050)
