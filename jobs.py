import uuid

jobs = {}


def create_job():

    job_id = str(uuid.uuid4())

    jobs[job_id] = {
        "status": "waiting",
        "percent": 0,
        "processed": 0,
        "emails_found": 0,
        "current_label": "",
        "label_processed": 0,
        "label_total": 0,
        "total_messages": 0,
        "file": None
    }

    return job_id


def update_job(job_id, **kwargs):

    if job_id not in jobs:
        return

    jobs[job_id].update(kwargs)


def get_job(job_id):

    return jobs.get(job_id)