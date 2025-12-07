import os
from typing import Any, Dict, Optional

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration


SCRUB_FIELDS = {
    "password",
    "pass",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "set-cookie",
    "inn",
    "ogrn",
    "name",
    "region",
    "email",
    "phone",
}


def _scrub_event(event: Dict[str, Any], hints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # type: ignore[override]
    def scrub_mapping(mapping: Optional[Dict[str, Any]]) -> None:
        if not mapping:
            return
        for key in list(mapping.keys()):
            if key.lower() in SCRUB_FIELDS:
                mapping[key] = "[REDACTED]"

    request = event.get("request")
    if isinstance(request, dict):
        scrub_mapping(request.get("headers"))
        scrub_mapping(request.get("cookies"))
        if "data" in request and isinstance(request["data"], dict):
            scrub_mapping(request["data"])

    user = event.get("user")
    if isinstance(user, dict):
        scrub_mapping(user)

    extra = event.get("extra")
    if isinstance(extra, dict):
        scrub_mapping(extra)

    return event


def _before_breadcrumb(crumb, hint):  # type: ignore[override]
    data = crumb.get("data")
    if isinstance(data, dict):
        for key in list(data.keys()):
            if key.lower() in SCRUB_FIELDS:
                data[key] = "[REDACTED]"
    return crumb


def init_sentry() -> Optional[str]:
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return None

    traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))
    send_default_pii = os.getenv("SENTRY_SEND_DEFAULT_PII", "false").lower() == "true"
    max_breadcrumbs = int(os.getenv("SENTRY_MAX_BREADCRUMBS", "100"))

    sentry_logging = LoggingIntegration(level=None, event_level=None)

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENV", "dev"),
        release=os.getenv("SENTRY_RELEASE"),
        traces_sample_rate=traces_sample_rate,
        send_default_pii=send_default_pii,
        before_send=_scrub_event,
        before_breadcrumb=_before_breadcrumb,
        integrations=[sentry_logging],
        max_breadcrumbs=max_breadcrumbs,
        attach_stacktrace=os.getenv("SENTRY_ATTACH_STACKTRACE", "false").lower() == "true",
    )
    return dsn

