import os
import re
import threading

from fastapi import (
    FastAPI,
    Request,
    Form
)

from fastapi.responses import (
    RedirectResponse,
    JSONResponse,
    FileResponse
)

from fastapi.templating import (
    Jinja2Templates
)

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from openpyxl import Workbook

from jobs import (
    create_job,
    update_job,
    get_job
)

from fastapi.staticfiles import StaticFiles

REDIRECT_URI = os.getenv(
    "REDIRECT_URI",
    "http://localhost:8000/auth/callback"
)


os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = FastAPI()

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

templates = Jinja2Templates(
    directory="templates"
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly"
]

CLIENT_SECRET_FILE = "credentials.json"

EMAIL_REGEX = re.compile(
    r'[\w\.-]+@[\w\.-]+\.\w+',
    re.IGNORECASE
)

oauth_state = {}
creds_store = {}


@app.get("/")
def home(request: Request):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )


@app.get("/connect")
def connect():

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )
    )

    oauth_state["state"] = state
    oauth_state["code_verifier"] = (
        flow.code_verifier
    )

    return RedirectResponse(
        authorization_url
    )


@app.get("/auth/callback")
def auth_callback(request: Request):

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=SCOPES,
        state=oauth_state["state"],
        redirect_uri=REDIRECT_URI
    )

    flow.code_verifier = (
        oauth_state["code_verifier"]
    )

    flow.fetch_token(
        authorization_response=str(
            request.url
        )
    )

    creds = flow.credentials

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    profile = (
        service.users()
        .getProfile(userId="me")
        .execute()
    )

    email = profile[
        "emailAddress"
    ]

    creds_store[email] = creds

    return RedirectResponse(
        f"/dashboard?email={email}"
    )


@app.get("/dashboard")
def dashboard(
    request: Request,
    email: str
):

    creds = creds_store[email]

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    labels = (
        service.users()
        .labels()
        .list(userId="me")
        .execute()
        .get("labels", [])
    )

    labels = sorted(
        labels,
        key=lambda x: x["name"]
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "labels": labels,
            "email": email
        }
    )

def run_extraction(
    job_id,
    email,
    selected_labels
):

    creds = creds_store[email]

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    profile = (
        service.users()
        .getProfile(userId="me")
        .execute()
    )

    my_email = (
        profile["emailAddress"]
        .lower()
        .strip()
    )

    labels_response = (
        service.users()
        .labels()
        .list(userId="me")
        .execute()
    )

    all_labels = labels_response.get(
        "labels",
        []
    )

    label_map = {
        label["id"]: label["name"]
        for label in all_labels
    }

    os.makedirs(
        "exports",
        exist_ok=True
    )

    output_file = (
        f"exports/{job_id}.xlsx"
    )

    wb = Workbook()

    ws = wb.active

    ws.title = "Emails"

    ws.append([
        "Email",
        "Domain",
        "Label",
        "Source Header"
    ])

    seen = set()

    total_labels = len(
        selected_labels
    )

    emails_found = 0

    update_job(
        job_id,
        status="running"
    )

    total_messages = 0
    label_totals = {}

    for label_id in selected_labels:

        count = 0
        token = None

        while True:

            result = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=[label_id],
                    maxResults=500,
                    pageToken=token
                )
                .execute()
            )

            count += len(
                result.get(
                    "messages",
                    []
                )
            )

            token = result.get(
                "nextPageToken"
            )

            if not token:
                break

        label_totals[label_id] = count

        total_messages += count

    update_job(
        job_id,
        total_messages=total_messages
    )

    processed_global = 0

    for index, label_id in enumerate(
        selected_labels,
        start=1
    ):

        label_name = label_map.get(
            label_id,
            label_id
        )

        update_job(
            job_id,
            current_label=label_name,
            label_processed=0,
            label_total=label_totals.get(
                label_id,
                0
            )
        )

        page_token = None
        processed = 0

        percent = int(
            processed_global * 100 /
            max(total_messages, 1)
        )

        while True:

            result = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=[label_id],
                    maxResults=500,
                    pageToken=page_token
                )
                .execute()
            )

            messages = result.get(
                "messages",
                []
            )

            for msg in messages:

                try:

                    msg_data = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=msg["id"],
                            format="metadata",
                            metadataHeaders=[
                                "From",
                                "To",
                                "Cc",
                                "Bcc",
                                "Reply-To"
                            ]
                        )
                        .execute()
                    )

                    headers = (
                        msg_data
                        .get("payload", {})
                        .get("headers", [])
                    )

                    for header in headers:

                        header_name = header.get(
                            "name",
                            ""
                        )

                        header_value = header.get(
                            "value",
                            ""
                        )

                        emails = (
                            EMAIL_REGEX.findall(
                                header_value
                            )
                        )

                        for email_addr in emails:

                            email_addr = (
                                email_addr
                                .lower()
                                .strip()
                            )

                            if (
                                email_addr
                                == my_email
                            ):
                                continue

                            if email_addr.startswith(
                                (
                                    "no-reply",
                                    "noreply",
                                    "mailer-daemon",
                                    "notifications"
                                )
                            ):
                                continue

                            if email_addr in seen:
                                continue

                            seen.add(
                                email_addr
                            )

                            domain = email_addr.split("@")[1]

                            ws.append([
                                email_addr,
                                domain,
                                label_name,
                                header_name
                            ])

                            emails_found += 1

                except Exception:
                    pass

                processed += 1
                processed_global += 1

                percent = min(
                    100,
                    int(
                        processed_global
                        * 100
                        /
                        max(total_messages, 1)
                    )
                )

            update_job(
                job_id,
                percent=percent,
                processed=processed,
                label_processed=processed,
                emails_found=emails_found
            )

            page_token = result.get(
                "nextPageToken"
            )

            if not page_token:
                break

    wb.save(
        output_file
    )

    update_job(
        job_id,
        status="completed",
        percent=100,
        processed=processed_global,
        file=output_file
    )


@app.post("/start-job")
def start_job(
    request: Request
):

    form = request._form

    return JSONResponse(
        {
            "error":
            "Use JS fetch()"
        }
    )


@app.post("/start-job/{email}")
async def start_job_email(
    request: Request,
    email: str
):

    form = await request.form()

    selected_labels = (
        form.getlist("labels")
    )

    job_id = create_job()

    thread = threading.Thread(
        target=run_extraction,
        args=(
            job_id,
            email,
            selected_labels
        )
    )

    thread.start()

    return {
        "job_id": job_id
    }


@app.get("/job-status/{job_id}")
def job_status(
    job_id: str
):

    job = get_job(
        job_id
    )

    return job


@app.get("/download/{job_id}")
def download(
    job_id: str
):

    job = get_job(
        job_id
    )

    if not job:
        return {
            "error":
            "Job not found"
        }

    if (
        job["status"]
        != "completed"
    ):
        return {
            "error":
            "Still processing"
        }

    return FileResponse(
        job["file"],
        filename="emails.xlsx"
    )