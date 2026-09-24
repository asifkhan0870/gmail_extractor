import os
import re
import csv
import threading
import time

from fastapi import FastAPI, Request

from fastapi.responses import (
    RedirectResponse,
    JSONResponse,
    FileResponse
)

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest

from jobs import (
    create_job,
    update_job,
    get_job
)


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

REDIRECT_URI = os.getenv(
    "REDIRECT_URI",
    "http://127.0.0.1:8000/auth/callback"
)

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = FastAPI()

CLIENT_SECRET_FILE = os.path.join(
    BASE_DIR,
    "credentials.json"
)

# Gmail HTTP batch supports up to 100 subrequests.
BATCH_SIZE = 100

# Number of times a failed/missing message will be retried.
MAX_RETRIES = 4

# Wait time between retry attempts.
RETRY_BASE_DELAY = 0.75


# ============================================================
# OAUTH STORAGE
# ============================================================

oauth_state = {}
creds_store = {}


# ============================================================
# STATIC FILES
# ============================================================

app.mount(
    "/static",
    StaticFiles(
        directory=os.path.join(
            BASE_DIR,
            "static"
        )
    ),
    name="static"
)


# ============================================================
# TEMPLATES
# ============================================================

templates = Jinja2Templates(
    directory=os.path.join(
        BASE_DIR,
        "templates"
    )
)


# ============================================================
# GMAIL SCOPES
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly"
]


# ============================================================
# EMAIL REGEX
# ============================================================

# Supports normal emails plus:
#   name+tag@example.com
#   name_tag@example.com
#   name%tag@example.com
#   first.last@example.com
# etc.
EMAIL_REGEX = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"[A-Z0-9-]+"
    r"(?:\.[A-Z0-9-]+)+",
    re.IGNORECASE
)


# ============================================================
# DEFINITELY SYSTEM-GENERATED LOCAL PARTS
# ============================================================

# Do NOT put "noreply" here.
#
# We intentionally allow:
# noreply-dmarc-report@rediffmailpro.com
AUTOMATED_EXACT_LOCAL_PARTS = {
    "mailer-daemon",
    "postmaster",
}


# ============================================================
# EMAIL NORMALIZATION
# ============================================================

def normalize_email(email):
    """
    Normalize an email address.
    """

    if not email:
        return ""

    return email.strip().lower()


# ============================================================
# EMAIL VALIDATION
# ============================================================

def is_valid_email(email):
    """
    Practical validation.

    Rejects obvious malformed addresses without attempting
    overly strict RFC validation.
    """

    if not email:
        return False

    email = normalize_email(
        email
    )

    if len(email) > 320:
        return False

    if "@" not in email:
        return False

    local_part, domain = email.rsplit(
        "@",
        1
    )

    # --------------------------------------------------------
    # LOCAL PART
    # --------------------------------------------------------

    if not local_part:
        return False

    if len(local_part) > 64:
        return False

    if local_part.startswith("."):
        return False

    if local_part.endswith("."):
        return False

    if ".." in local_part:
        return False

    # --------------------------------------------------------
    # DOMAIN
    # --------------------------------------------------------

    if not domain:
        return False

    if "." not in domain:
        return False

    if domain.startswith("."):
        return False

    if domain.endswith("."):
        return False

    if ".." in domain:
        return False

    domain_parts = domain.split(".")

    for part in domain_parts:

        if not part:
            return False

        if part.startswith("-"):
            return False

        if part.endswith("-"):
            return False

    # --------------------------------------------------------
    # TLD
    # --------------------------------------------------------

    tld = domain_parts[-1]

    if len(tld) < 2:
        return False

    if not re.fullmatch(
        r"[a-z]{2,63}",
        tld
    ):
        return False

    return True


# ============================================================
# GODADDY CONVERSATION FILTER
# ============================================================

def is_godaddy_conversation_domain(domain):
    """
    Detect GoDaddy conversation-generated email domains.
    """

    domain = normalize_email(
        domain
    )

    if not domain:
        return False

    # Standard GoDaddy domain
    if domain.endswith(
        ".mail.conversations.godaddy.com"
    ):
        return True

    if domain == (
        "mail.conversations.godaddy.com"
    ):
        return True

    # Truncated/export variant observed in the GMass data
    if domain.endswith(
        ".mail.conversations"
    ):
        return True

    if domain == "mail.conversations":
        return True

    return False


# ============================================================
# AUTOMATED EMAIL FILTER
# ============================================================

def is_automated_email(email):
    """
    Remove only clearly system-generated addresses.

    Generic addresses such as support@ and info@ are NOT
    automatically rejected.
    """

    email = normalize_email(
        email
    )

    if "@" not in email:
        return True

    local_part, domain = email.rsplit(
        "@",
        1
    )

    # Exact system addresses
    if local_part in AUTOMATED_EXACT_LOCAL_PARTS:
        return True

    # GoDaddy generated conversations
    if is_godaddy_conversation_domain(
        domain
    ):
        return True

    return False


# ============================================================
# CLEAN EMAIL
# ============================================================

def clean_email(email):
    """
    Normalize, validate and filter an extracted email.

    Returns:
        normalized email
        or None
    """

    email = normalize_email(
        email
    )

    if not email:
        return None

    if not is_valid_email(
        email
    ):
        return None

    if is_automated_email(
        email
    ):
        return None

    return email


# ============================================================
# PROCESS ONE MESSAGE
# ============================================================

def process_message(
    msg_data,
    my_email,
    label_name,
    seen,
    writer
):
    """
    Process one Gmail message.

    Preserves the original extraction behavior:

        From
        To
        Cc
        Bcc
        Reply-To
    """

    if not isinstance(
        msg_data,
        dict
    ):
        return 0, False

    try:

        payload = msg_data.get(
            "payload"
        )

        # A usable message must have a payload.
        if not isinstance(
            payload,
            dict
        ):
            return 0, False

        headers = payload.get(
            "headers"
        )

        # If headers are absent, the message should be retried.
        if not isinstance(
            headers,
            list
        ):
            return 0, False

        emails_added = 0

        for header in headers:

            if not isinstance(
                header,
                dict
            ):
                continue

            header_name = (
                header.get(
                    "name",
                    ""
                )
                .strip()
            )

            header_value = (
                header.get(
                    "value",
                    ""
                )
            )

            if not header_value:
                continue

            # ------------------------------------------------
            # Extract every email address.
            # ------------------------------------------------

            emails = (
                EMAIL_REGEX.findall(
                    header_value
                )
            )

            for raw_email in emails:

                email_addr = clean_email(
                    raw_email
                )

                if not email_addr:
                    continue

                # --------------------------------------------
                # Ignore own Gmail account.
                # --------------------------------------------

                if email_addr == my_email:
                    continue

                # --------------------------------------------
                # Duplicate protection.
                # --------------------------------------------

                if email_addr in seen:
                    continue

                seen.add(
                    email_addr
                )

                # --------------------------------------------
                # Domain.
                # --------------------------------------------

                domain = (
                    email_addr
                    .split(
                        "@",
                        1
                    )[1]
                )

                # --------------------------------------------
                # Write CSV.
                # --------------------------------------------

                writer.writerow([
                    email_addr,
                    domain,
                    label_name,
                    header_name
                ])

                emails_added += 1

        # The message was successfully received and had a
        # usable header structure, even if it contained zero
        # extractable emails.
        return emails_added, True

    except Exception:
        return 0, False


# ============================================================
# RETRY ONE MESSAGE
# ============================================================

def retry_message(
    service,
    message_id
):
    """
    Retry one message.

    Returns:
        (success, message_data, error)
    """

    last_error = None

    for attempt in range(
        MAX_RETRIES
    ):

        try:

            msg_data = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=[
                        "From",
                        "To",
                        "Cc",
                        "Bcc",
                        "Reply-To"
                    ],
                    fields="id,payload/headers"
                )
                .execute()
            )

            # ------------------------------------------------
            # Confirm we actually received usable metadata.
            # ------------------------------------------------

            if (
                isinstance(
                    msg_data,
                    dict
                )
                and
                isinstance(
                    msg_data.get(
                        "payload"
                    ),
                    dict
                )
                and
                isinstance(
                    msg_data
                    .get(
                        "payload"
                    )
                    .get(
                        "headers"
                    ),
                    list
                )
            ):

                return (
                    True,
                    msg_data,
                    None
                )

            last_error = (
                "Gmail returned incomplete message metadata."
            )

        except Exception as exc:

            last_error = exc

        # ----------------------------------------------------
        # Exponential backoff.
        # ----------------------------------------------------

        if attempt < (
            MAX_RETRIES - 1
        ):

            time.sleep(
                RETRY_BASE_DELAY
                * (
                    2 ** attempt
                )
            )

    return (
        False,
        None,
        last_error
    )


# ============================================================
# BATCH CALLBACK
# ============================================================

def make_batch_callback(
    results,
    errors
):
    """
    Create a callback dedicated to one batch.

    A dedicated callback avoids any shared-state ambiguity.
    """

    def callback(
        request_id,
        response,
        exception
    ):

        if exception is not None:

            errors[
                request_id
            ] = exception

        else:

            results[
                request_id
            ] = response

    return callback


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home(
    request: Request
):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )


# ============================================================
# CONNECT GMAIL
# ============================================================

@app.get("/connect")
def connect():

    if not os.path.exists(
        CLIENT_SECRET_FILE
    ):

        raise FileNotFoundError(
            "credentials.json not found. "
            f"Expected location: {CLIENT_SECRET_FILE}"
        )

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


# ============================================================
# AUTH CALLBACK
# ============================================================

@app.get("/auth/callback")
def auth_callback(
    request: Request
):

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
        credentials=creds,
        cache_discovery=False
    )

    profile = (
        service.users()
        .getProfile(
            userId="me"
        )
        .execute()
    )

    email = profile[
        "emailAddress"
    ]

    creds_store[email] = creds

    return RedirectResponse(
        f"/dashboard?email={email}"
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/dashboard")
def dashboard(
    request: Request,
    email: str
):

    creds = creds_store[email]

    service = build(
        "gmail",
        "v1",
        credentials=creds,
        cache_discovery=False
    )

    labels = (
        service.users()
        .labels()
        .list(
            userId="me"
        )
        .execute()
        .get(
            "labels",
            []
        )
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


# ============================================================
# RUN EXTRACTION
# ============================================================

def run_extraction(
    job_id,
    email,
    selected_labels
):

    csv_file = None

    try:

        # ====================================================
        # CREDENTIALS
        # ====================================================

        creds = creds_store[email]

        if not creds.valid:

            if (
                creds.expired
                and creds.refresh_token
            ):

                creds.refresh(
                    GoogleAuthRequest()
                )

            else:

                raise RuntimeError(
                    "Gmail credentials are no longer valid."
                )

        # ====================================================
        # GMAIL SERVICE
        # ====================================================

        service = build(
            "gmail",
            "v1",
            credentials=creds,
            cache_discovery=False
        )

        # ====================================================
        # CURRENT USER
        # ====================================================

        profile = (
            service.users()
            .getProfile(
                userId="me"
            )
            .execute()
        )

        my_email = normalize_email(
            profile[
                "emailAddress"
            ]
        )

        # ====================================================
        # LABELS
        # ====================================================

        labels_response = (
            service.users()
            .labels()
            .list(
                userId="me"
            )
            .execute()
        )

        all_labels = (
            labels_response.get(
                "labels",
                []
            )
        )

        label_map = {
            label["id"]: label["name"]
            for label in all_labels
        }

        # ====================================================
        # EXPORT DIRECTORY
        # ====================================================

        export_dir = os.path.join(
            BASE_DIR,
            "exports"
        )

        os.makedirs(
            export_dir,
            exist_ok=True
        )

        # ====================================================
        # CSV OUTPUT
        # ====================================================

        output_file = os.path.join(
            export_dir,
            f"{job_id}.csv"
        )

        csv_file = open(
            output_file,
            "w",
            newline="",
            encoding="utf-8-sig"
        )

        writer = csv.writer(
            csv_file
        )

        writer.writerow([
            "Email",
            "Domain",
            "Label",
            "Source Header"
        ])

        # ====================================================
        # TRACKING
        # ====================================================

        seen = set()

        emails_found = 0

        processed_global = 0

        total_messages = 0

        successful_messages = 0

        failed_messages = 0

        # ====================================================
        # INITIAL STATUS
        # ====================================================

        update_job(
            job_id,
            status="running",
            percent=0,
            processed=0,
            emails_found=0,
            total_messages=0
        )

        # ====================================================
        # PROCESS LABELS
        # ====================================================

        for label_index, label_id in enumerate(
            selected_labels,
            start=1
        ):

            label_name = label_map.get(
                label_id,
                label_id
            )

            # =================================================
            # GET MESSAGE IDS ONCE
            # =================================================

            message_ids = []

            page_token = None

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

                for message in messages:

                    message_id = message.get(
                        "id"
                    )

                    if message_id:

                        message_ids.append(
                            message_id
                        )

                page_token = result.get(
                    "nextPageToken"
                )

                if not page_token:
                    break

            label_total = len(
                message_ids
            )

            total_messages += label_total

            # =================================================
            # LABEL STATUS
            # =================================================

            update_job(
                job_id,
                current_label=label_name,
                label_processed=0,
                label_total=label_total,
                total_messages=total_messages,
                processed=processed_global,
                emails_found=emails_found
            )

            # =================================================
            # BATCH PROCESSING
            # =================================================

            for batch_start in range(
                0,
                label_total,
                BATCH_SIZE
            ):

                batch_ids = message_ids[
                    batch_start:
                    batch_start + BATCH_SIZE
                ]

                # =================================================
                # BATCH RESULT STORAGE
                # =================================================

                batch_results = {}
                batch_errors = {}

                # =================================================
                # CALLBACK
                # =================================================

                callback = make_batch_callback(
                    batch_results,
                    batch_errors
                )

                # =================================================
                # CREATE BATCH
                # =================================================

                batch = (
                    service.new_batch_http_request(
                        callback=callback
                    )
                )

                # =================================================
                # ADD ALL MESSAGES
                #
                # EXACT ORIGINAL FIVE HEADERS.
                # =================================================

                for message_id in batch_ids:

                    request = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=message_id,
                            format="metadata",
                            metadataHeaders=[
                                "From",
                                "To",
                                "Cc",
                                "Bcc",
                                "Reply-To"
                            ],
                            fields="id,payload/headers"
                        )
                    )

                    batch.add(
                        request,
                        request_id=message_id
                    )

                # =================================================
                # EXECUTE BATCH
                # =================================================

                batch.execute()

                # =================================================
                # DETECT EVERY MESSAGE THAT DID NOT RETURN
                #
                # This is the important reliability fix.
                # =================================================

                missing_message_ids = []

                for message_id in batch_ids:

                    if message_id not in batch_results:

                        missing_message_ids.append(
                            message_id
                        )

                # =================================================
                # PROCESS SUCCESSFUL RESULTS
                # =================================================

                for message_id in batch_ids:

                    msg_data = batch_results.get(
                        message_id
                    )

                    if not msg_data:

                        continue

                    added, usable = process_message(
                        msg_data,
                        my_email,
                        label_name,
                        seen,
                        writer
                    )

                    if usable:

                        successful_messages += 1
                        emails_found += added

                    else:

                        # The batch technically returned something,
                        # but not usable message metadata.
                        if message_id not in (
                            missing_message_ids
                        ):

                            missing_message_ids.append(
                                message_id
                            )

                # =================================================
                # RETRY ALL MISSING / FAILED MESSAGES
                #
                # No hard-coded emails.
                #
                # We retry MESSAGE IDs automatically.
                # =================================================

                if missing_message_ids:

                    # Remove duplicates while preserving order.
                    missing_message_ids = list(
                        dict.fromkeys(
                            missing_message_ids
                        )
                    )

                    for missing_id in missing_message_ids:

                        (
                            success,
                            retry_data,
                            retry_error
                        ) = retry_message(
                            service,
                            missing_id
                        )

                        if not success:

                            failed_messages += 1

                            continue

                        added, usable = process_message(
                            retry_data,
                            my_email,
                            label_name,
                            seen,
                            writer
                        )

                        if usable:

                            successful_messages += 1
                            emails_found += added

                        else:

                            failed_messages += 1

                # =================================================
                # COUNT PROCESSED MESSAGE IDS
                #
                # Every expected message has now either:
                #
                #   1. succeeded
                #   2. succeeded after retry
                #   3. genuinely failed after retries
                #
                # =================================================

                processed_global += len(
                    batch_ids
                )

                # =================================================
                # FLUSH CSV
                # =================================================

                csv_file.flush()

                # =================================================
                # PROGRESS
                # =================================================

                label_processed = min(
                    batch_start + len(batch_ids),
                    label_total
                )

                if total_messages > 0:

                    percent = int(
                        processed_global
                        * 100
                        /
                        total_messages
                    )

                else:

                    percent = 100

                update_job(
                    job_id,
                    percent=min(
                        99,
                        percent
                    ),
                    processed=processed_global,
                    label_processed=label_processed,
                    label_total=label_total,
                    total_messages=total_messages,
                    emails_found=emails_found,
                    successful_messages=successful_messages,
                    failed_messages=failed_messages
                )

        # ========================================================
        # FINAL SAFETY FLUSH
        # ========================================================

        csv_file.flush()

        # ========================================================
        # CLOSE CSV
        # ========================================================

        csv_file.close()

        csv_file = None

        # ========================================================
        # COMPLETE
        # ========================================================

        update_job(
            job_id,
            status="completed",
            percent=100,
            processed=processed_global,
            total_messages=total_messages,
            emails_found=emails_found,
            successful_messages=successful_messages,
            failed_messages=failed_messages,
            file=output_file
        )

    except Exception as e:

        if csv_file:

            try:
                csv_file.close()

            except Exception:
                pass

        update_job(
            job_id,
            status="failed",
            error=str(e)
        )


# ============================================================
# START JOB
# ============================================================

@app.post("/start-job")
def start_job(
    request: Request
):

    return JSONResponse(
        {
            "error": "Use JS fetch()"
        }
    )


# ============================================================
# START JOB FOR EMAIL
# ============================================================

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
        ),
        daemon=True
    )

    thread.start()

    return {
        "job_id": job_id
    }


# ============================================================
# JOB STATUS
# ============================================================

@app.get("/job-status/{job_id}")
def job_status(
    job_id: str
):

    job = get_job(
        job_id
    )

    return job


# ============================================================
# DOWNLOAD CSV
# ============================================================

@app.get("/download/{job_id}")
def download(
    job_id: str
):

    job = get_job(
        job_id
    )

    if not job:

        return {
            "error": "Job not found"
        }

    if (
        job["status"]
        != "completed"
    ):

        return {
            "error": "Still processing"
        }

    return FileResponse(
        job["file"],
        filename="emails.csv",
        media_type="text/csv"
    )