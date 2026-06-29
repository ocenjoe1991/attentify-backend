import base64
import mimetypes
from typing import Any


def extract_gmail_attachments(
    payload: dict[str, Any] | None,
    *,
    gmail_message_id: str,
    account_email: str | None = None,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    def walk(part: dict[str, Any] | None) -> None:
        if not part:
            return

        filename = (part.get("filename") or "").strip()
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        mime_type = part.get("mimeType") or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        if filename and attachment_id:
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": body.get("size") or 0,
                    "gmail_message_id": gmail_message_id,
                    "attachment_id": attachment_id,
                    "account_email": account_email,
                }
            )

        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return attachments


def decode_gmail_attachment_data(data: str) -> bytes:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("utf-8"))
