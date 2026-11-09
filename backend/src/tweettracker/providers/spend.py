"""API spend guardrail ledger enforcement and summary reporting."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.config import Settings
from tweettracker.models import ApiCreditLedger, Post, TrackedAccount, Workspace
from tweettracker.providers.base import SpendCapExceededError


# LLM calls log tokens as "units"; they cost dollars but never count against the X-data
# provider's monthly call plan (e.g. TweetAPI's 100k units).
LLM_PROVIDER_NAMES: tuple[str, ...] = ("google_gemini", "xai_grok", "openai", "anthropic", "deepinfra")


def _ensure_utc(dt: datetime | None = None) -> datetime:
    current = dt or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _utc_day_and_month_starts(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = _ensure_utc(now)
    day_start = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
    month_start = datetime(current.year, current.month, 1, tzinfo=timezone.utc)
    return day_start, month_start


async def get_workspace_spend_totals(
    session: AsyncSession,
    workspace_id: int,
    now: datetime | None = None,
) -> tuple[float, float, float, float]:
    """Returns (daily_usd, monthly_usd, daily_units, monthly_units) for a workspace.

    USD covers every provider; units cover X-data provider calls only (LLM tokens excluded).
    """
    day_start, month_start = _utc_day_and_month_starts(now)
    x_units = case(
        (ApiCreditLedger.provider.in_(LLM_PROVIDER_NAMES), 0.0),
        else_=ApiCreditLedger.units_consumed,
    )

    day_stmt = select(
        func.coalesce(func.sum(ApiCreditLedger.estimated_usd), 0.0),
        func.coalesce(func.sum(x_units), 0.0),
    ).where(
        ApiCreditLedger.workspace_id == workspace_id,
        ApiCreditLedger.created_at >= day_start,
    )
    month_stmt = select(
        func.coalesce(func.sum(ApiCreditLedger.estimated_usd), 0.0),
        func.coalesce(func.sum(x_units), 0.0),
    ).where(
        ApiCreditLedger.workspace_id == workspace_id,
        ApiCreditLedger.created_at >= month_start,
    )

    day_row = (await session.execute(day_stmt)).one()
    month_row = (await session.execute(month_stmt)).one()
    return (
        float(day_row[0] or 0.0),
        float(month_row[0] or 0.0),
        float(day_row[1] or 0.0),
        float(month_row[1] or 0.0),
    )


async def enforce_spend_caps(
    session: AsyncSession,
    workspace: Workspace,
    projected_usd: float = 0.00025,
    projected_units: float = 1.0,
    *,
    now: datetime | None = None,
) -> None:
    """Blocks paid external API calls before dispatch if USD or monthly unit budget would be exceeded."""
    daily_usd, monthly_usd, _, monthly_units = await get_workspace_spend_totals(
        session, workspace.id, now=now
    )

    if daily_usd >= workspace.daily_spend_cap_usd or (
        projected_usd > 0.0 and (daily_usd + projected_usd) > workspace.daily_spend_cap_usd + 1e-9
    ):
        raise SpendCapExceededError(
            f"Workspace '{workspace.slug}' daily spend cap (${workspace.daily_spend_cap_usd:.2f}) "
            f"reached (spent=${daily_usd:.4f}, next_call=${projected_usd:.4f})."
        )

    if monthly_usd >= workspace.monthly_spend_cap_usd or (
        projected_usd > 0.0
        and (monthly_usd + projected_usd) > workspace.monthly_spend_cap_usd + 1e-9
    ):
        raise SpendCapExceededError(
            f"Workspace '{workspace.slug}' monthly spend cap (${workspace.monthly_spend_cap_usd:.2f}) "
            f"reached (spent=${monthly_usd:.4f}, next_call=${projected_usd:.4f})."
        )

    unit_budget = int(getattr(workspace, "monthly_unit_budget", 100_000) or 0)
    if unit_budget > 0 and projected_usd > 0.0 and projected_units > 0.0:
        if monthly_units >= unit_budget or (monthly_units + projected_units) > unit_budget + 1e-9:
            raise SpendCapExceededError(
                f"Workspace '{workspace.slug}' monthly unit budget ({unit_budget:,} units) "
                f"reached (used={monthly_units:.1f}, next_call={projected_units:.1f})."
            )


async def log_api_credit(
    session: AsyncSession,
    *,
    workspace_id: int | None,
    provider: str,
    endpoint: str,
    units_consumed: float,
    estimated_usd: float,
    reconciled_balance_units: float | None = None,
) -> ApiCreditLedger:
    entry = ApiCreditLedger(
        workspace_id=workspace_id,
        provider=provider,
        endpoint=endpoint,
        units_consumed=float(units_consumed),
        estimated_usd=float(estimated_usd),
        reconciled_balance_units=reconciled_balance_units,
    )
    session.add(entry)
    await session.flush()
    return entry


async def estimate_workspace_monthly_units(
    session: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Projects full-month API unit consumption and returns discovery pacing tier ('normal' | 'throttled' | 'paused')."""
    current = _ensure_utc(now)
    if current.month == 12:
        next_month_start = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month_start = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    remaining_days = max(0.0, (next_month_start - current).total_seconds() / 86_400.0)

    _, _, _, monthly_units = await get_workspace_spend_totals(
        session, workspace.id, now=current
    )

    reconciled_row = (
        await session.execute(
            select(ApiCreditLedger.reconciled_balance_units)
            .where(
                ((ApiCreditLedger.workspace_id == workspace.id) | (ApiCreditLedger.workspace_id.is_(None))),
                ApiCreditLedger.reconciled_balance_units.is_not(None),
            )
            .order_by(ApiCreditLedger.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    reconciled_balance = float(reconciled_row) if reconciled_row is not None else None

    budget = max(0, int(getattr(workspace, "monthly_unit_budget", 100_000) or 100_000))
    if workspace.x_data_provider == "fixture":
        return {
            "monthly_unit_budget": budget,
            "units_month": round(monthly_units, 2),
            "reconciled_balance_units": reconciled_balance,
            "estimated_radar_daily_units": 0.0,
            "estimated_discovery_daily_units": 0.0,
            "projected_monthly_units": round(monthly_units, 1),
            "projected_monthly_pct": 0.0,
            "discovery_throttle_mode": "normal",
        }

    active_accounts = int(
        (
            await session.execute(
                select(func.count(TrackedAccount.id)).where(
                    TrackedAccount.workspace_id == workspace.id,
                    TrackedAccount.is_tracked.is_(True),
                    TrackedAccount.is_muted.is_(False),
                )
            )
        ).scalar_one()
        or 0
    )
    active_posts = int(
        (
            await session.execute(
                select(func.count(Post.id)).where(
                    Post.workspace_id == workspace.id,
                    Post.matured_48h.is_(False),
                )
            )
        ).scalar_one()
        or 0
    )

    poll_mins = max(2, int(workspace.radar_poll_minutes or 10))
    polls_per_day = 1440.0 / float(poll_mins)
    x_list_cnt = len(workspace.x_list_ids_json or [])
    if workspace.use_x_lists and x_list_cnt > 0:
        radar_queries = float(x_list_cnt)
    else:
        radar_queries = (
            float(math.ceil(active_accounts / 22.0)) if active_accounts > 0 else 0.0
        )
    snap_queries = float(math.ceil(active_posts / 100.0)) if active_posts > 0 else 0.0
    est_radar_daily = polls_per_day * (radar_queries + snap_queries)
    est_discovery_daily = (
        min(float(active_accounts) * 1.5, 240.0) if active_accounts > 0 else 0.0
    )

    projected_unthrottled = monthly_units + remaining_days * (
        est_radar_daily + est_discovery_daily
    )
    rem_budget = max(0.0, float(budget) - monthly_units)
    eff_remaining = (
        min(rem_budget, reconciled_balance)
        if reconciled_balance is not None
        else rem_budget
    )

    if (
        budget <= 0
        or monthly_units >= 0.95 * budget
        or projected_unthrottled >= 0.95 * budget
        or (reconciled_balance is not None and reconciled_balance <= 0.05 * budget)
        or (est_radar_daily > 0.0 and remaining_days * est_radar_daily >= eff_remaining)
    ):
        mode = "paused"
        projected_units = monthly_units + remaining_days * est_radar_daily
    elif (
        monthly_units >= 0.80 * budget
        or projected_unthrottled >= 0.80 * budget
        or (reconciled_balance is not None and reconciled_balance <= 0.20 * budget)
    ):
        mode = "throttled"
        projected_units = monthly_units + remaining_days * (
            est_radar_daily + 0.25 * est_discovery_daily
        )
    else:
        mode = "normal"
        projected_units = projected_unthrottled

    return {
        "monthly_unit_budget": budget,
        "units_month": round(monthly_units, 2),
        "reconciled_balance_units": reconciled_balance,
        "estimated_radar_daily_units": round(est_radar_daily, 1),
        "estimated_discovery_daily_units": round(est_discovery_daily, 1),
        "projected_monthly_units": round(projected_units, 1),
        "projected_monthly_pct": round(
            (projected_units / float(budget) * 100.0) if budget > 0 else 0.0, 2
        ),
        "discovery_throttle_mode": mode,
    }


async def build_spend_summary(
    session: AsyncSession,
    workspace: Workspace,
) -> dict[str, Any]:
    daily_usd, monthly_usd, daily_units, monthly_units = await get_workspace_spend_totals(
        session, workspace.id
    )
    unit_proj = await estimate_workspace_monthly_units(session, workspace)

    recent_stmt = (
        select(ApiCreditLedger)
        .where(
            (ApiCreditLedger.workspace_id == workspace.id)
            | (ApiCreditLedger.workspace_id.is_(None))
        )
        .order_by(ApiCreditLedger.id.desc())
        .limit(25)
    )
    recent_rows = list((await session.execute(recent_stmt)).scalars().all())

    reconciled_balance: float | None = unit_proj["reconciled_balance_units"]

    env_status = Settings.provider_env_status()
    daily_cap = max(float(workspace.daily_spend_cap_usd), 0.0)
    monthly_cap = max(float(workspace.monthly_spend_cap_usd), 0.0)

    return {
        "workspace_id": workspace.id,
        "x_data_provider": workspace.x_data_provider,
        "llm_provider": workspace.llm_provider,
        "daily_spent_usd": round(daily_usd, 6),
        "daily_spend_cap_usd": daily_cap,
        "daily_pct": round((daily_usd / daily_cap * 100.0) if daily_cap > 0 else 0.0, 2),
        "monthly_spent_usd": round(monthly_usd, 6),
        "monthly_spend_cap_usd": monthly_cap,
        "monthly_pct": round((monthly_usd / monthly_cap * 100.0) if monthly_cap > 0 else 0.0, 2),
        "units_today": round(daily_units, 2),
        "units_month": round(monthly_units, 2),
        "monthly_unit_budget": unit_proj["monthly_unit_budget"],
        "projected_monthly_units": unit_proj["projected_monthly_units"],
        "projected_monthly_pct": unit_proj["projected_monthly_pct"],
        "discovery_throttle_mode": unit_proj["discovery_throttle_mode"],
        "reconciled_balance_units": reconciled_balance,
        "x_provider_configured": env_status.get(workspace.x_data_provider, False),
        "llm_provider_configured": env_status.get(workspace.llm_provider, False),
        "provider_credentials_status": env_status,
        "recent_ledger": [
            {
                "id": r.id,
                "workspace_id": r.workspace_id,
                "provider": r.provider,
                "endpoint": r.endpoint,
                "units_consumed": r.units_consumed,
                "estimated_usd": r.estimated_usd,
                "reconciled_balance_units": r.reconciled_balance_units,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent_rows
        ],
    }
