"""Turn a stored submission into the Notify personalisation for the response email."""
import datetime
import json


def personalisation(submission_id, item):
    form = json.loads(item["FormData"])
    language = item.get("FormSubmissionLanguage", "en")
    delivery = form.get("deliveryOption") or {}
    titles = form.get("form") or {}
    if language == "fr":
        subject = delivery.get("emailSubjectFr") or titles.get("titleFr") or "Réponse au formulaire"
    else:
        subject = delivery.get("emailSubjectEn") or titles.get("titleEn") or "Form response"
    created = item.get("CreatedAt")
    stamp = (
        datetime.datetime.fromtimestamp(created / 1000, datetime.timezone.utc).isoformat()
        if isinstance(created, (int, float))
        else "n/a"
    )
    lines = [f"# {subject}", "", f"Submission ID: {submission_id}", f"Submitted: {stamp}", ""]
    for question, answer in sorted((form.get("responses") or {}).items()):
        lines.append(f"**{question}**")
        lines.append(str(answer))
        lines.append("")
    return {"subject": subject, "formResponse": "\n".join(lines)}


def recipient(item):
    form = json.loads(item["FormData"])
    address = (form.get("deliveryOption") or {}).get("emailAddress")
    if not address:
        raise ValueError("Email address is missing or empty.")
    return address
