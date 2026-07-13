"""Apollo -> HubSpot lead pipeline: ICP search, enrichment, and push.

Every call pattern here (endpoints, payload shapes, gotchas) was validated
against the live Apollo + HubSpot APIs during the "PL MSPs" pilot batch.
See Apollo_HubSpot_Pipeline_SOP.docx for the narrative version.
"""
import os
import re
import requests

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY")
APOLLO_BASE = "https://api.apollo.io"
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY")
HUBSPOT_BASE = "https://api.hubapi.com"


def _apollo_headers():
    return {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}


def _hubspot_headers():
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


# ---------------------------------------------------------------- search ----

def search_apollo(criteria, page=1, per_page=25):
    """ICP search against Apollo's live database. Returns preview-only results
    (obfuscated name, no email/phone) -- browsing does not spend reveal credits.
    """
    payload = {"per_page": per_page, "page": page}
    if criteria.get("locations"):
        payload["organization_locations"] = criteria["locations"]
    if criteria.get("employee_min") is not None and criteria.get("employee_max") is not None:
        payload["organization_num_employees_ranges"] = [f"{criteria['employee_min']},{criteria['employee_max']}"]
    if criteria.get("titles"):
        payload["person_titles"] = criteria["titles"]
    if criteria.get("seniorities"):
        payload["person_seniorities"] = criteria["seniorities"]
    if criteria.get("keyword_tags"):
        payload["q_organization_keyword_tags"] = criteria["keyword_tags"]

    resp = requests.post(f"{APOLLO_BASE}/api/v1/mixed_people/api_search",
                          headers=_apollo_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    candidates = []
    for p in data.get("people", []):
        org = p.get("organization") or {}
        candidates.append({
            "apollo_person_id": p.get("id"),
            "first_name": p.get("first_name", ""),
            "last_name": p.get("last_name_obfuscated", ""),
            "title": p.get("title", ""),
            "company_name": org.get("name", ""),
            "has_email": bool(p.get("has_email")),
            "has_direct_phone": p.get("has_direct_phone") in ("Yes", True),
        })
    return {"total_entries": data.get("total_entries", 0), "candidates": candidates}


# Apollo caps mixed_people/api_search at 100 results per page. Browsing this
# preview-only stage doesn't spend reveal credits, so it's safe to page through
# everything -- capped here just to keep a single batch from ballooning into
# an unbounded pull on a very wide (e.g. nationwide, no keyword) search.
APOLLO_SEARCH_PAGE_SIZE = 100
MAX_SEARCH_RESULTS = 1000


def search_apollo_all(criteria, max_results=MAX_SEARCH_RESULTS):
    """Pages through Apollo's ICP search until every match is fetched (or
    max_results is hit) instead of returning just the first page."""
    first = search_apollo(criteria, page=1, per_page=APOLLO_SEARCH_PAGE_SIZE)
    total_entries = first["total_entries"]
    candidates = list(first["candidates"])
    to_fetch = min(total_entries, max_results)

    page = 2
    while len(candidates) < to_fetch:
        result = search_apollo(criteria, page=page, per_page=APOLLO_SEARCH_PAGE_SIZE)
        if not result["candidates"]:
            break
        candidates.extend(result["candidates"])
        page += 1

    truncated = total_entries > len(candidates)
    return {"total_entries": total_entries, "candidates": candidates[:max_results], "truncated": truncated}


# --------------------------------------------------------------- reveal -----

def reveal_email(apollo_person_id):
    """Synchronous email + core profile reveal. Works with an id from a fresh
    search (search_apollo above). NOTE: does NOT work with a saved Apollo
    contact's contact_id -- use linkedin_url in that case instead.
    """
    resp = requests.post(f"{APOLLO_BASE}/v1/people/match",
                          headers=_apollo_headers(),
                          json={"id": apollo_person_id, "reveal_personal_emails": True},
                          timeout=30)
    resp.raise_for_status()
    data = resp.json()
    person = data.get("person") or {}
    return {
        "email": person.get("email", ""),
        "email_status": person.get("email_status", ""),
        "linkedin_url": person.get("linkedin_url", ""),
        "apollo_contact_id": person.get("contact_id", ""),
        "organization_id": person.get("organization_id", ""),
        "time_zone": person.get("time_zone", ""),
        "first_name": person.get("first_name", ""),
        "last_name": person.get("last_name", ""),
    }


def submit_phone_reveal(apollo_person_id, webhook_url):
    """Async mobile phone reveal. Apollo POSTs the result to webhook_url some
    seconds later -- correlate it back using the person id embedded in that
    payload (payload['people'][x]['id']), not any value returned here.
    Costs ~8 Apollo credits per attempt, win or lose.
    """
    resp = requests.post(f"{APOLLO_BASE}/v1/people/match",
                          headers=_apollo_headers(),
                          json={"id": apollo_person_id, "reveal_phone_number": True,
                                "webhook_url": webhook_url},
                          timeout=30)
    resp.raise_for_status()
    return resp.json()


def enrich_orgs_bulk(domains):
    """Up to 10 domains per call -- hard Apollo limit, confirmed by testing."""
    results = {}
    for i in range(0, len(domains), 10):
        batch = [d for d in domains[i:i + 10] if d]
        if not batch:
            continue
        resp = requests.post(f"{APOLLO_BASE}/v1/organizations/bulk_enrich",
                              headers=_apollo_headers(), json={"domains": batch}, timeout=30)
        if not resp.ok:
            continue
        data = resp.json()
        if data.get("status") != "success":
            continue
        for org in data.get("organizations") or []:
            domain = org.get("primary_domain")
            if domain:
                results[domain] = org
    return results


def enrich_org_by_id(org_id):
    resp = requests.get(f"{APOLLO_BASE}/v1/organizations/{org_id}",
                         headers=_apollo_headers(), timeout=30)
    if not resp.ok:
        return None
    return resp.json().get("organization")


def persist_apollo_phone(apollo_contact_id, phone_number):
    """Write a revealed phone back onto the saved Apollo contact record.
    Must be a TOP-LEVEL 'phone_numbers' key -- nesting under 'contact' or a
    flat 'mobile_phone' field silently succeeds without actually persisting.
    """
    resp = requests.put(f"{APOLLO_BASE}/v1/contacts/{apollo_contact_id}",
                         headers=_apollo_headers(),
                         json={"phone_numbers": [{"raw_number": phone_number, "type": "mobile"}]},
                         timeout=30)
    return resp.ok


# ---------------------------------------------------------- HubSpot push ----

C_LEVEL_KEYWORDS = ['ceo', 'chief executive', 'president', 'owner', 'founder',
                     'cio', 'chief information', 'cto', 'chief technology',
                     'coo', 'chief operating', 'cfo', 'chief financial']
MID_MGMT_KEYWORDS = ['director', 'vp ', ' vp', 'vice president', 'manager']

EMAIL_STATUS_MAP = {'verified': 'valid', 'unavailable': 'Unavailable', 'extrapolated': 'Unverified'}


def decision_maker_level(title):
    t = (title or '').lower()
    if any(k in t for k in C_LEVEL_KEYWORDS):
        return 'C-Level'
    if any(k in t for k in MID_MGMT_KEYWORDS):
        return 'Mid-Mgmt'
    return 'Other'


def employee_bucket(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ''
    if n <= 5: return '1-5'
    if n <= 25: return '5-25'
    if n <= 50: return '25-50'
    if n <= 100: return '50-100'
    if n <= 500: return '100-500'
    if n <= 1000: return '500-1000'
    return '1000+'


def tz_to_hubspot(tz):
    if not tz:
        return ''
    return tz.lower().replace('/', '_slash_').replace(' ', '_')


def digits_only(phone):
    return re.sub(r'\D', '', phone or '')


def build_hubspot_properties(candidate, sales_focus, lead_source, custom_industry=''):
    props = {
        'firstname': candidate.get('first_name', ''),
        'lastname': candidate.get('last_name', ''),
        'jobtitle': candidate.get('title', ''),
        'decision_maker': decision_maker_level(candidate.get('title')),
        'company': candidate.get('company_name', ''),
        'email': candidate.get('email', ''),
        'email_status': EMAIL_STATUS_MAP.get(candidate.get('email_status'), ''),
        'hs_linkedin_url': candidate.get('linkedin_url', ''),
        'website': candidate.get('website', ''),
        'company_linkedin_url': candidate.get('company_linkedin_url', ''),
        'company_address': candidate.get('company_address', ''),
        'state': candidate.get('state', ''),
        'city': candidate.get('city', ''),
        'sales_focus': sales_focus,
        'lead_source': lead_source,
        'hs_timezone': tz_to_hubspot(candidate.get('time_zone')),
        'industry': candidate.get('industry', ''),
        'custom_industry': custom_industry,
    }
    if candidate.get('mobile_phone'):
        props['mobilephone'] = candidate['mobile_phone']
    if candidate.get('company_phone'):
        props['company_phone'] = digits_only(candidate['company_phone'])
    if candidate.get('employees') not in (None, ''):
        props['employees'] = str(candidate['employees'])
        bucket = employee_bucket(candidate['employees'])
        if bucket:
            props['numemployees'] = bucket
    if candidate.get('annual_revenue') not in (None, ''):
        try:
            props['annualrevenue'] = str(int(candidate['annual_revenue']))
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in props.items() if v not in ('', None)}


def push_candidates_to_hubspot(candidates, sales_focus, lead_source, owner_id=None, custom_industry=''):
    """Batch upsert by email. Dedupes by email first -- HubSpot rejects the
    whole batch if any id (email) repeats.
    """
    seen = set()
    inputs = []
    order = []
    for c in candidates:
        email = c.get('email')
        if not email or email in seen:
            continue
        seen.add(email)
        props = build_hubspot_properties(c, sales_focus, lead_source, custom_industry)
        if owner_id:
            props['hubspot_owner_id'] = owner_id
        inputs.append({"idProperty": "email", "id": email, "properties": props})
        order.append(c)

    results = []
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        resp = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/upsert",
                              headers=_hubspot_headers(), json={"inputs": batch}, timeout=60)
        if resp.ok:
            results.extend(resp.json().get("results", []))
        else:
            results.extend([{"error": resp.text[:300]}] * len(batch))
    return results


def create_call_tasks(contact_ids_with_state, owner_id, due_timestamp_iso, task_label="Task 1"):
    """One CALL task per contact: 'MSP | {State} | {task_label}', associated
    to the contact at creation time (associationTypeId 204).
    """
    created = []
    for contact_id, state in contact_ids_with_state:
        subject = f"MSP | {state or 'Unknown State'} | {task_label}"
        payload = {
            "properties": {
                "hs_task_subject": subject,
                "hs_task_type": "CALL",
                "hs_task_status": "NOT_STARTED",
                "hs_timestamp": due_timestamp_iso,
                "hubspot_owner_id": owner_id,
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]
            }]
        }
        resp = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/tasks",
                              headers=_hubspot_headers(), json=payload, timeout=30)
        if resp.ok:
            created.append(resp.json().get("id"))
    return created
