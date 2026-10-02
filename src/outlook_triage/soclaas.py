from __future__ import annotations

import json
import random
import time
from datetime import datetime

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from pydantic import ValidationError

from .config import Settings
from .models import EmailClassification, EmailForClassification


class SoCLaaSError(RuntimeError):
    pass


SYSTEM_PROMPT = """You are a conservative email-triage classifier.
Return one JSON object only, with exactly these fields:
{
  "requires_action": boolean,
  "urgency": "urgent" | "soon" | "normal" | "none",
  "action_type": "reply" | "submit" | "confirm" | "approve" | "attend" | "schedule" | "other" | "none",
  "task": string | null,
  "deadline": "YYYY-MM-DD" | null,
  "deadline_raw": string | null,
  "summary": string,
  "category": "action" | "information" | "newsletter" | "automated",
  "reason": string,
  "confidence": number
}

Rules:
- Do not invent a task or deadline.
- Set a deadline only when explicit or clearly implied by the email.
- Resolve relative dates from the supplied received timestamp and timezone.
- Requests to reply, submit, confirm, approve, attend, or schedule normally require action.
- Informational importance alone does not imply action.
- If requires_action is false, urgency and action_type must both be "none" and task must be null.
- Keep summary and reason concise.
"""


def _extract_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    if start < 0:
        raise ValueError("response did not contain a JSON object")
    value, _ = json.JSONDecoder().raw_decode(stripped[start:])
    if not isinstance(value, dict):
        raise ValueError("response JSON was not an object")
    return value


class SoCLaaSClient:
    def __init__(self, settings: Settings, *, sleep=time.sleep):
        settings.require_soclaas(require_model=False)
        self.settings = settings
        self.sleep = sleep
        self.client = OpenAI(
            api_key=settings.soclaas_api_key,
            base_url=settings.soclaas_base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )

    def list_models(self) -> list[dict]:
        result = self.client.models.list()
        models: list[dict] = []
        for item in result.data:
            if hasattr(item, "model_dump"):
                models.append(item.model_dump())
            else:
                models.append({"id": getattr(item, "id", str(item))})
        return models

    def verify_model(self) -> None:
        self.settings.require_soclaas(require_model=True)
        available = {str(item.get("id")) for item in self.list_models()}
        if self.settings.model not in available:
            raise SoCLaaSError(
                f"Configured model {self.settings.model!r} is unavailable. Run `outlook-triage models` and update EMAIL_TRIAGE_MODEL."
            )

    def _completion(self, messages: list[dict]) -> str:
        for attempt in range(5):
            try:
                response = self.client.chat.completions.create(
                    model=self.settings.model,
                    messages=messages,
                    temperature=0.1,
                )
                content = response.choices[0].message.content
                if not content:
                    raise SoCLaaSError("SoCLaaS returned an empty classification")
                return content
            except RateLimitError as exc:
                if attempt == 4:
                    raise SoCLaaSError("SoCLaaS rate or budget limit persisted after retries") from exc
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt == 4:
                    raise SoCLaaSError("SoCLaaS was unreachable after retries") from exc
            except APIStatusError as exc:
                if exc.status_code == 401:
                    raise SoCLaaSError("SoCLaaS rejected the API key (HTTP 401)") from exc
                if exc.status_code == 403:
                    raise SoCLaaSError("SoCLaaS rejected the configured model or feature (HTTP 403)") from exc
                if exc.status_code < 500 or attempt == 4:
                    raise SoCLaaSError(f"SoCLaaS returned HTTP {exc.status_code}") from exc
            delay = min(30.0, 2**attempt) + random.uniform(0, 0.5)
            self.sleep(delay)
        raise AssertionError("unreachable")

    def classify(self, email: EmailForClassification, now: datetime) -> EmailClassification:
        user_prompt = (
            f"Current local time: {now.isoformat()}\n"
            f"Sender: {email.sender}\n"
            f"Subject: {email.subject}\n"
            f"Received: {email.received_at}\n\n"
            f"Body:\n{email.body}"
        )
        raw = self._completion(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
        try:
            return EmailClassification.model_validate(_extract_json(raw))
        except (ValueError, ValidationError) as first_error:
            repaired = self._completion(
                [
                    {
                        "role": "system",
                        "content": "Repair the candidate into valid JSON matching the supplied schema. Return JSON only; do not add facts.",
                    },
                    {"role": "user", "content": f"Schema and rules:\n{SYSTEM_PROMPT}\n\nCandidate:\n{raw}"},
                ]
            )
            try:
                return EmailClassification.model_validate(_extract_json(repaired))
            except (ValueError, ValidationError) as exc:
                raise SoCLaaSError(f"SoCLaaS returned invalid structured output: {exc}") from first_error

