"""HMAC-SHA256 Signed Outbound Webhook Dispatcher & Tier-Deduplicated Alert Policy Engine."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.crypto import resolve_secret
from tweettracker.models import AlertDelivery, Post, TrackedAccount, Workspace
from tweettracker.radar.scorer import ScoredPostResult
from tweettracker.radar.sse import RadarSSEHub, radar_sse_hub

SIGNATURE_HEADER_NAME = "X-TweetTracker-Signature-256"
EVENT_HEADER_NAME = "X-TweetTracker-Event"
DELIVERY_ID_HEADER_NAME = "X-TweetTracker-Delivery-Id"
MAX_WEBHOOK_URLS_PER_WORKSPACE = 10


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _allow_local_webhooks() -> bool:
    return os.environ.get("TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _is_disallowed_ip_address(raw_ip: str, *, allow_local: bool = False) -> bool:
    cleaned = raw_ip.strip().split("%", 1)[0]
    ip_obj = ipaddress.ip_address(cleaned)
    candidates = [ip_obj]
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        candidates.append(ip_obj.ipv4_mapped)

    for ip_val in candidates:
        # Cloud metadata / link-local, multicast, unspecified, and reserved remain blocked always
        if (
            ip_val.is_link_local
            or ip_val.is_multicast
            or ip_val.is_reserved
            or ip_val.is_unspecified
        ):
            return True
        if not allow_local and (
            ip_val.is_private
            or ip_val.is_loopback
            or (not ip_val.is_global)
        ):
            return True
    return False


def _is_rfc2606_example_domain(host: str) -> bool:
    return host in ("example.com", "example.org", "example.net") or host.endswith(
        (".example.com", ".example.org", ".example.net", ".example")
    )


def _validate_resolved_addrinfos(
    host: str,
    infos: Sequence[Any],
    *,
    allow_local: bool = False,
) -> None:
    if not infos:
        raise ValueError(f"Webhook hostname '{host}' did not resolve to any address.")
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addr_str = str(sockaddr[0])
        try:
            disallowed = _is_disallowed_ip_address(addr_str, allow_local=allow_local)
        except ValueError as exc:
            raise ValueError(
                f"Webhook hostname '{host}' resolved to invalid IP '{addr_str}'."
            ) from exc
        if disallowed:
            raise ValueError(
                f"Webhook target '{host}' resolved to disallowed private/internal IP '{addr_str}'."
            )


def validate_outbound_webhook_url(url: str, *, resolve_dns: bool = False) -> str:
    """Validates an outbound webhook URL against SSRF, embedded credentials, and
    loopback/RFC1918/link-local/metadata IP literals (with `TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS=true`
    opt-in for local single-operator tools like local n8n).
    """
    if not isinstance(url, str):
        raise ValueError("Webhook URL must be a string.")
    cleaned = url.strip()
    if not cleaned or len(cleaned) > 512 or any(ch.isspace() for ch in cleaned):
        raise ValueError("Webhook URL must be a valid non-empty URL (max 512 chars).")

    parsed = urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()
    allow_local = _allow_local_webhooks()
    allow_http = allow_local or (
        os.environ.get("TWEETTRACKER_ALLOW_HTTP_WEBHOOKS", "").strip().lower()
        in ("1", "true", "yes")
    )
    if scheme not in ("http", "https"):
        raise ValueError("Webhook URL scheme must be https.")
    if scheme == "http" and not allow_http:
        raise ValueError(
            "Webhook URL must use https:// (http:// is disallowed unless "
            "TWEETTRACKER_ALLOW_HTTP_WEBHOOKS=true or TWEETTRACKER_ALLOW_LOCAL_WEBHOOKS=true)."
        )

    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise ValueError("Webhook URL must not contain embedded userinfo credentials.")
    if parsed.fragment:
        raise ValueError("Webhook URL must not contain a fragment identifier.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Webhook URL contains an invalid port.") from exc

    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ValueError("Webhook URL must specify a valid hostname.")

    if not allow_local and (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
        or host.endswith(".home.arpa")
        or host.endswith(".test")
        or host.endswith(".invalid")
    ):
        raise ValueError(f"Webhook target host '{host}' is disallowed.")

    ip_candidate = host.split("%", 1)[0]
    is_ip_literal = False
    try:
        ipaddress.ip_address(ip_candidate)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal:
        if _is_disallowed_ip_address(ip_candidate, allow_local=allow_local):
            raise ValueError(
                f"Webhook target IP '{host}' is disallowed (private, loopback, link-local, or reserved)."
            )
    else:
        if "." not in host and not (allow_local and host == "localhost"):
            raise ValueError(f"Webhook target host '{host}' must be a fully-qualified public domain.")
        labels = host.split(".")
        if all(lbl.isdigit() or lbl.startswith("0x") for lbl in labels):
            raise ValueError(f"Webhook target host '{host}' uses disallowed numeric IP notation.")

    if resolve_dns and not is_ip_literal:
        eff_port = port or (443 if scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, eff_port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            if not (_is_rfc2606_example_domain(host) or (allow_local and host == "localhost")):
                raise ValueError(f"Webhook DNS resolution failed for '{host}'.") from exc
        else:
            _validate_resolved_addrinfos(host, infos, allow_local=allow_local)

    return cleaned


async def validate_and_resolve_webhook_target(url: str) -> str:
    """Async pre-dispatch SSRF guard performing static validation + live DNS resolution
    via the running event loop immediately before opening any outbound HTTP connection.
    """
    allow_local = _allow_local_webhooks()
    cleaned = validate_outbound_webhook_url(url, resolve_dns=False)
    parsed = urlsplit(cleaned)
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    ip_candidate = host.split("%", 1)[0]
    try:
        ipaddress.ip_address(ip_candidate)
        return cleaned
    except ValueError:
        pass

    eff_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, eff_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if _is_rfc2606_example_domain(host) or (allow_local and host == "localhost"):
            return cleaned
        raise ValueError(f"Webhook DNS resolution failed for '{host}'.") from exc

    _validate_resolved_addrinfos(host, infos, allow_local=allow_local)
    return cleaned


def compute_hmac_signature(raw_body: bytes, *, secret: str | None = None) -> str:
    """Computes `sha256=<hex>` HMAC-SHA256 over `raw_body` using
    `resolve_secret("TWEETTRACKER_WEBHOOK_SECRET")` unless overridden.
    """
    resolved_secret = (
        resolve_secret("TWEETTRACKER_WEBHOOK_SECRET") if secret is None else secret
    )
    digest = hmac.new(
        resolved_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_hmac_signature(
    raw_body: bytes,
    signature_header: str,
    *,
    secret: str | None = None,
) -> bool:
    """Constant-time verification of `X-TweetTracker-Signature-256: sha256=<hex>`."""
    resolved_secret = (
        resolve_secret("TWEETTRACKER_WEBHOOK_SECRET") if secret is None else secret
    )
    if not resolved_secret:
        return False
    expected = compute_hmac_signature(raw_body, secret=resolved_secret)
    supplied = signature_header.strip()
    if not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def _parse_hhmm(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned or ":" not in cleaned:
        return None
    parts = cleaned.split(":")
    if len(parts) != 2:
        return None
    if len(parts[0]) != 2 or len(parts[1]) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def is_within_quiet_hours(
    now_dt: datetime,
    quiet_hours_start: str | None,
    quiet_hours_end: str | None,
) -> bool:
    """Evaluates whether `now_dt` falls inside `[quiet_hours_start, quiet_hours_end)`,
    cleanly handling overnight spans such as `"23:00"` -> `"06:30"`.
    """
    start_m = _parse_hhmm(quiet_hours_start)
    end_m = _parse_hhmm(quiet_hours_end)
    if start_m is None or end_m is None or start_m == end_m:
        return False

    cur_m = now_dt.hour * 60 + now_dt.minute
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def should_dispatch_tier(delivered_tiers: set[str], candidate_tier: str) -> bool:
    """Enforces per-post tier deduplication:
    - Never re-fires `watch` if `watch` or `surge` was already delivered.
    - Upgrades `watch -> surge` at most once when score crosses `surge_threshold`.
    - Never downgrades `surge -> watch`.
    """
    if candidate_tier not in ("watch", "surge"):
        return False
    if "surge" in delivered_tiers:
        return False
    if candidate_tier == "watch" and "watch" in delivered_tiers:
        return False
    return True


@dataclass(slots=True)
class WebhookDispatchOutcome:
    delivery: AlertDelivery
    delivery_uuid: str
    signature: str
    raw_body: bytes
    signed: bool = False


@dataclass(slots=True)
class BreakoutAlertDispatchSummary:
    sse_delivered: bool = False
    webhook_outcomes: list[WebhookDispatchOutcome] = field(default_factory=list)
    suppressed_deliveries: list[AlertDelivery] = field(default_factory=list)
    deduplicated_webhook_urls: list[str] = field(default_factory=list)

    @property
    def new_alerts_count(self) -> int:
        delivered_wh = sum(1 for o in self.webhook_outcomes if o.delivery.status == "delivered")
        return (1 if self.sse_delivered else 0) + delivered_wh


@dataclass(slots=True)
class TestWebhookSummary:
    workspace_id: int
    event_type: str
    signature_header: str
    signature_preview: str
    webhook_secret_configured: bool
    payload: dict[str, Any]
    outcomes: list[WebhookDispatchOutcome]
    signed: bool = False


async def dispatch_signed_webhook(
    session: AsyncSession,
    *,
    workspace_id: int,
    post_id: int | None,
    target_url: str,
    event_type: str,
    alert_tier: str,
    payload: dict[str, Any],
    now: datetime | None = None,
    max_attempts: int = 3,
    retry_delays: Sequence[float] | None = None,
    timeout_seconds: float = 8.0,
    http_client: httpx.AsyncClient | None = None,
    allow_private_targets: bool = False,
) -> WebhookDispatchOutcome:
    """Signs `payload` with `HMAC-SHA256`, enforces outbound SSRF & DNS-rebinding guards
    with `follow_redirects=False`, dispatches with bounded exponential backoff, and
    records the result in `AlertDelivery`.
    """
    if not allow_private_targets:
        if http_client is None:
            validated_url = await validate_and_resolve_webhook_target(target_url)
        else:
            validated_url = validate_outbound_webhook_url(target_url, resolve_dns=False)
    else:
        validated_url = target_url.strip()

    now_utc = _ensure_utc(now)
    raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode(
        "utf-8"
    )
    secret_val = resolve_secret("TWEETTRACKER_WEBHOOK_SECRET")
    is_signed = bool(secret_val)
    signature = compute_hmac_signature(raw_body, secret=secret_val)
    delivery_uuid = uuid.uuid4().hex

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "TweetTracker-Webhook/0.1",
        SIGNATURE_HEADER_NAME: signature,
        EVENT_HEADER_NAME: event_type,
        DELIVERY_ID_HEADER_NAME: delivery_uuid,
    }

    delays = list(retry_delays) if retry_delays is not None else [0.2, 0.6]
    tries = max(1, int(max_attempts))

    final_status = "failed"
    final_http_status: int | None = None
    final_error: str | None = None
    attempts_made = 0

    async def _run_attempts(client: httpx.AsyncClient) -> None:
        nonlocal final_status, final_http_status, final_error, attempts_made
        for attempt in range(1, tries + 1):
            attempts_made = attempt
            try:
                resp = await client.post(validated_url, content=raw_body, headers=headers)
                final_http_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    final_status = "delivered"
                    final_error = None
                    break

                if 300 <= resp.status_code < 400:
                    final_error = f"HTTP {resp.status_code} (redirects disallowed)"
                    break

                final_error = f"HTTP {resp.status_code}"
                retryable = resp.status_code >= 500 or resp.status_code == 429
                if (not retryable) or attempt >= tries:
                    break
            except httpx.HTTPError as exc:
                final_http_status = None
                final_error = f"Webhook transport error ({exc.__class__.__name__})"
                if attempt >= tries:
                    break

            delay_sec = float(delays[min(attempt - 1, len(delays) - 1)]) if delays else 0.0
            if delay_sec > 0.0:
                await asyncio.sleep(delay_sec)

    if http_client is not None:
        await _run_attempts(http_client)
    else:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as client:
            await _run_attempts(client)

    delivery = AlertDelivery(
        workspace_id=workspace_id,
        post_id=post_id,
        event_type=event_type,
        channel="webhook",
        target_url=validated_url,
        alert_tier=alert_tier,
        payload_json=payload,
        status=final_status,
        http_status=final_http_status,
        attempts=attempts_made,
        error_message=final_error,
        created_at=now_utc,
    )
    session.add(delivery)
    await session.flush()

    return WebhookDispatchOutcome(
        delivery=delivery,
        delivery_uuid=delivery_uuid,
        signature=signature,
        raw_body=raw_body,
        signed=is_signed,
    )


def build_breakout_alert_payload(
    workspace: Workspace,
    post: Post,
    author: TrackedAccount,
    scored: ScoredPostResult,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _ensure_utc(now)
    event_type = f"breakout.{scored.alert_tier}"
    return {
        "event_type": event_type,
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "workspace_slug": workspace.slug,
        "my_x_handle": workspace.my_x_handle,
        "post_id": post.id,
        "x_post_id": post.x_post_id,
        "author_handle": author.handle,
        "author_display_name": author.display_name,
        "author_followers": author.followers_count,
        "is_mutual": bool(
            workspace.my_x_handle
            and workspace.my_x_handle.strip()
            and author.follows_me
            and author.followed_by_me
        ),
        "text": post.text,
        "age_minutes": scored.age_minutes,
        "views": post.latest_views,
        "breakout_score": round(float(scored.breakout_score), 2),
        "breakout_probability": round(float(scored.breakout_probability), 4),
        "velocity_z_score": scored.velocity.velocity_z_score,
        "velocity_multiple": round(scored.velocity.velocity_multiple, 2),
        "seismic_infectiousness": scored.seismic.infectiousness,
        "value_model_score": round(scored.value_model_score, 4),
        "dpp_novelty_multiplier": round(float(scored.dpp_novelty_multiplier), 4),
        "catalysis_boost": round(float(scored.catalysis_boost), 4),
        "recommended_action": scored.recommended_action,
        "tactical_rationale": scored.tactical_rationale,
        "alert_tier": scored.alert_tier,
        "x_post_url": f"https://x.com/{author.handle}/status/{post.x_post_id}",
        "x_reply_intent_url": f"https://x.com/intent/tweet?in_reply_to={post.x_post_id}",
        "timestamp": now_utc.isoformat(),
    }


async def _count_webhook_deliveries_last_hour(
    session: AsyncSession,
    workspace_id: int,
    now_utc: datetime,
) -> int:
    window_start = now_utc - timedelta(hours=1)
    cnt = (
        await session.execute(
            select(func.count(AlertDelivery.id)).where(
                AlertDelivery.workspace_id == workspace_id,
                AlertDelivery.channel == "webhook",
                AlertDelivery.status == "delivered",
                AlertDelivery.event_type != "webhook.test",
                AlertDelivery.created_at >= window_start,
            )
        )
    ).scalar_one()
    return int(cnt or 0)


async def dispatch_breakout_alert(
    session: AsyncSession,
    workspace: Workspace,
    post: Post,
    author: TrackedAccount,
    scored: ScoredPostResult,
    *,
    now: datetime | None = None,
    sse_hub: RadarSSEHub | None = None,
    retry_delays: Sequence[float] | None = None,
    max_attempts: int = 3,
) -> BreakoutAlertDispatchSummary:
    """Evaluates SSE + outbound HMAC-SHA256 webhook alert policy for a scored post."""
    summary = BreakoutAlertDispatchSummary()
    if scored.alert_tier not in ("watch", "surge") or post.id is None:
        return summary

    now_utc = _ensure_utc(now)
    hub = sse_hub or radar_sse_hub
    event_type = f"breakout.{scored.alert_tier}"
    payload = build_breakout_alert_payload(workspace, post, author, scored, now=now_utc)

    prior_rows = (
        await session.execute(
            select(AlertDelivery.channel, AlertDelivery.target_url, AlertDelivery.alert_tier).where(
                AlertDelivery.workspace_id == workspace.id,
                AlertDelivery.post_id == post.id,
                AlertDelivery.status == "delivered",
            )
        )
    ).all()

    sse_tiers = {str(tier) for ch, _, tier in prior_rows if ch == "sse" and tier}
    if should_dispatch_tier(sse_tiers, scored.alert_tier):
        hub.publish(workspace.id, event_type, payload)
        sse_row = AlertDelivery(
            workspace_id=workspace.id,
            post_id=post.id,
            event_type=event_type,
            channel="sse",
            target_url=None,
            alert_tier=scored.alert_tier,
            payload_json=payload,
            status="delivered",
            http_status=None,
            attempts=1,
            error_message=None,
            created_at=now_utc,
        )
        session.add(sse_row)
        await session.flush()
        summary.sse_delivered = True

    webhook_urls = [
        u.strip() for u in (workspace.webhook_urls_json or []) if u and u.strip()
    ][:MAX_WEBHOOK_URLS_PER_WORKSPACE]
    if not webhook_urls:
        return summary

    in_quiet = is_within_quiet_hours(
        now_utc,
        workspace.quiet_hours_start,
        workspace.quiet_hours_end,
    )

    for url in webhook_urls:
        wh_tiers = {
            str(tier)
            for ch, tgt, tier in prior_rows
            if ch == "webhook" and (tgt == url or tgt is None) and tier
        }
        if not should_dispatch_tier(wh_tiers, scored.alert_tier):
            summary.deduplicated_webhook_urls.append(url)
            continue

        if in_quiet:
            sup_q = AlertDelivery(
                workspace_id=workspace.id,
                post_id=post.id,
                event_type=event_type,
                channel="webhook",
                target_url=url,
                alert_tier=scored.alert_tier,
                payload_json=payload,
                status="suppressed_quiet_hours",
                http_status=None,
                attempts=0,
                error_message=(
                    f"Suppressed during workspace quiet hours "
                    f"({workspace.quiet_hours_start}–{workspace.quiet_hours_end} UTC)."
                ),
                created_at=now_utc,
            )
            session.add(sup_q)
            await session.flush()
            summary.suppressed_deliveries.append(sup_q)
            continue

        delivered_1h = await _count_webhook_deliveries_last_hour(session, workspace.id, now_utc)
        if delivered_1h >= int(workspace.max_alerts_per_hour):
            sup_r = AlertDelivery(
                workspace_id=workspace.id,
                post_id=post.id,
                event_type=event_type,
                channel="webhook",
                target_url=url,
                alert_tier=scored.alert_tier,
                payload_json=payload,
                status="suppressed_rate_limit",
                http_status=None,
                attempts=0,
                error_message=(
                    f"Suppressed by max_alerts_per_hour cap "
                    f"({delivered_1h}/{workspace.max_alerts_per_hour} in trailing 60m)."
                ),
                created_at=now_utc,
            )
            session.add(sup_r)
            await session.flush()
            summary.suppressed_deliveries.append(sup_r)
            continue

        try:
            outcome = await dispatch_signed_webhook(
                session,
                workspace_id=workspace.id,
                post_id=post.id,
                target_url=url,
                event_type=event_type,
                alert_tier=scored.alert_tier,
                payload=payload,
                now=now_utc,
                max_attempts=max_attempts,
                retry_delays=retry_delays,
            )
            summary.webhook_outcomes.append(outcome)
        except ValueError as exc:
            blocked = AlertDelivery(
                workspace_id=workspace.id,
                post_id=post.id,
                event_type=event_type,
                channel="webhook",
                target_url=url,
                alert_tier=scored.alert_tier,
                payload_json=payload,
                status="failed",
                http_status=None,
                attempts=0,
                error_message=f"Blocked unsafe webhook target: {exc}",
                created_at=now_utc,
            )
            session.add(blocked)
            await session.flush()
            summary.webhook_outcomes.append(
                WebhookDispatchOutcome(
                    delivery=blocked,
                    delivery_uuid="",
                    signature="",
                    raw_body=b"",
                    signed=False,
                )
            )

    return summary


async def send_test_webhook(
    session: AsyncSession,
    workspace: Workspace,
    *,
    target_url: str | None = None,
    custom_note: str | None = None,
    now: datetime | None = None,
    sse_hub: RadarSSEHub | None = None,
    retry_delays: Sequence[float] | None = None,
    max_attempts: int = 2,
    http_client: httpx.AsyncClient | None = None,
    allow_private_targets: bool = False,
) -> TestWebhookSummary:
    """Sends a signed `HMAC-SHA256` test webhook (`event_type="webhook.test"`, `is_test=True`
    bypassing quiet hours & hourly caps) and logs `AlertDelivery`.
    """
    now_utc = _ensure_utc(now)
    hub = sse_hub or radar_sse_hub

    if target_url and target_url.strip():
        urls = [target_url.strip()]
    else:
        urls = [
            u.strip() for u in (workspace.webhook_urls_json or []) if u and u.strip()
        ][:MAX_WEBHOOK_URLS_PER_WORKSPACE]

    if not urls:
        raise ValueError(
            "No webhook target URL provided and workspace.webhook_urls_json is empty."
        )

    if not allow_private_targets:
        urls = [validate_outbound_webhook_url(u, resolve_dns=False) for u in urls]

    sample_handle = (
        await session.execute(
            select(TrackedAccount.handle)
            .where(
                TrackedAccount.workspace_id == workspace.id,
                TrackedAccount.is_tracked.is_(True),
            )
            .order_by(TrackedAccount.heavy_hitter_score.desc(), TrackedAccount.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none() or "example_voice_1"

    payload: dict[str, Any] = {
        "event_type": "webhook.test",
        "is_test": True,
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "workspace_slug": workspace.slug,
        "my_x_handle": workspace.my_x_handle,
        "note": custom_note or "TweetTracker HMAC-SHA256 verification ping.",
        "sample_breakout": {
            "x_post_id": "1895001000",
            "author_handle": str(sample_handle),
            "breakout_score": 89.4,
            "alert_tier": "surge",
            "recommended_action": (
                "reply_mutual_20x" if workspace.my_x_handle else "quote_oon_safe"
            ),
            "velocity_multiple": 3.9,
            "dpp_novelty_multiplier": 0.96,
        },
        "timestamp": now_utc.isoformat(),
    }

    outcomes: list[WebhookDispatchOutcome] = []
    for url in urls:
        outcome = await dispatch_signed_webhook(
            session,
            workspace_id=workspace.id,
            post_id=None,
            target_url=url,
            event_type="webhook.test",
            alert_tier="test",
            payload=payload,
            now=now_utc,
            max_attempts=max_attempts,
            retry_delays=retry_delays,
            http_client=http_client,
            allow_private_targets=allow_private_targets,
        )
        outcomes.append(outcome)

    secret_configured = bool(resolve_secret("TWEETTRACKER_WEBHOOK_SECRET"))
    raw_sig = (
        outcomes[0].signature
        if outcomes
        else compute_hmac_signature(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
    )
    sig_preview = f"{raw_sig[:23]}..." if secret_configured else "unsigned"

    hub.publish(
        workspace.id,
        "webhook.tested",
        {
            "targets": urls,
            "statuses": [o.delivery.status for o in outcomes],
            "signed": secret_configured,
            "signature_preview": sig_preview,
        },
    )

    return TestWebhookSummary(
        workspace_id=workspace.id,
        event_type="webhook.test",
        signature_header=SIGNATURE_HEADER_NAME,
        signature_preview=sig_preview,
        webhook_secret_configured=secret_configured,
        payload=payload,
        outcomes=outcomes,
        signed=secret_configured,
    )
