"""Deterministic multi-workspace fixture seed bootstrapper for TweetTracker Build 1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any

import numpy as np
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tweettracker.algo import (
    ObservablePostMetrics,
    PipelineParams,
    QueryScoringContext,
    SurrogateCalibrationParams,
    UPSTREAM_COMMIT_SHA,
    UserCredV2,
    ValueModelWeights,
    build_candidate_scoring_inputs,
    compute_text_embedding,
    compute_value_scores,
    dpp_novelty_multiplier,
    recommend_engagement_action,
)
from tweettracker.crypto import refresh_runtime_secrets
from tweettracker.models import (
    AccountCommunityMembership,
    AlertDelivery,
    AlgoParamVersion,
    ApiCreditLedger,
    Base,
    BreakoutPrediction,
    Community,
    DiscoveredCandidate,
    PeerEndorsement,
    Post,
    PostInteraction,
    PostSnapshot,
    RelationshipEdge,
    TrackedAccount,
    UpstreamAlgoEvent,
    Workspace,
)

SNAPSHOT_AGES = (15, 30, 45, 60, 120, 360, 1440, 2880)


def _build_baseline_curves(median_48h_views: int) -> dict[str, dict[str, int]]:
    """Constructs realistic age-matched P25/P50/P75/P90 view curves across 15m..48h."""
    age_fracs = {
        "15": 0.09,
        "30": 0.17,
        "45": 0.25,
        "60": 0.33,
        "120": 0.50,
        "360": 0.72,
        "1440": 0.91,
        "2880": 1.00,
    }
    curves: dict[str, dict[str, int]] = {"p25": {}, "p50": {}, "p75": {}, "p90": {}}
    for age_str, frac in age_fracs.items():
        p50 = max(40, int(median_48h_views * frac))
        curves["p25"][age_str] = max(20, int(p50 * 0.58))
        curves["p50"][age_str] = p50
        curves["p75"][age_str] = int(p50 * 1.55)
        curves["p90"][age_str] = int(p50 * 2.45)
    return curves


WS1_ACCOUNTS: list[tuple[str, str, str, int, int, list[int], str]] = [
    # Applied AI (community index 0) + bridges
    ("tensor_architect", "Elena Vance", "Inference kernels, KV-cache quantization & production LLM serving.", 142_000, 0, [0], "mutual_active"),
    ("agentic_runtime", "Marcus Chen", "Building reliable multi-agent workflows and eval harnesses.", 88_500, 0, [0, 1], "mutual_active"),
    ("eval_bench", "Priya Nair", "Empirical benchmarks for reasoning models & retrieval pipelines.", 64_200, 0, [0], "outbound_warming"),
    ("cuda_craft", "Viktor Novak", "GPU systems engineering, Triton kernels, low-latency batching.", 119_000, 0, [0], "outbound_cold"),
    ("synth_data_lab", "Hannah Brooks", "Post-training data curation, DPO pipelines, reward modeling.", 47_800, 0, [0], "mutual_dormant"),
    ("rag_pragmatist", "Devon Patel", "Hybrid search, Postgres pgvector & real-world enterprise RAG.", 53_400, 0, [0, 2], "mutual_active"),
    ("voice_ai_ops", "Clara Morales", "Sub-300ms voice agents for clinic & field-service dispatch.", 39_100, 0, [0, 1, 2], "inbound_unreciprocated"),
    ("open_weights_hq", "Liam O'Connor", "Open-weight fine-tuning, LoRA routing, on-prem deployments.", 95_600, 0, [0], "outbound_warming"),
    ("prompt_compiler", "Sora Takahashi", "Structured outputs, constrained decoding & grammar engines.", 31_200, 0, [0], "mutual_active"),
    ("model_router", "Nadia Al-Mansoor", "Cost/latency routing across frontier & distilled models.", 44_900, 0, [0, 1], "outbound_warming"),
    ("edge_infer", "Owen Lindqvist", "On-device ML, WebGPU & quantized local assistants.", 28_700, 0, [0], "outbound_cold"),
    ("safety_redteam", "Tessa Sterling", "Adversarial testing, prompt-injection guardrails & tool sandboxes.", 36_400, 0, [0], "inbound_unreciprocated"),
    # Venture & Startups (community index 1) + bridges
    ("seed_stage_memo", "Jordan Mercer", "Pre-seed & seed operator-investor. B2B workflow & vertical AI.", 165_000, 1, [1], "mutual_active"),
    ("plg_mechanics", "Chloe Park", "Self-serve funnels, pricing experiments & NRR expansion loops.", 79_300, 1, [1], "mutual_dormant"),
    ("vertical_saas_vc", "rafael_costa_vc", "Backing vertical software replacing legacy spreadsheets.", 112_400, 1, [1, 2], "mutual_active"),
    ("founder_cadence", "Amara Okafor", "Weekly execution rhythms from $0 to $10M ARR.", 58_900, 1, [1], "outbound_warming"),
    ("gtm_engineer", "Tyler Vance", "Outbound automation, intent signals & lean GTM stacks.", 46_200, 1, [0, 1], "mutual_active"),
    ("cap_table_notes", "benjamin_hale", "Term sheets, secondaries, runway math & board prep.", 91_000, 1, [1], "outbound_cold"),
    ("design_partner_0", "Mei-Ling Zhou", "Landing your first 10 enterprise design partners without fluff.", 34_800, 1, [1], "inbound_unreciprocated"),
    ("bootstrapped_arr", "craig_henderson", "Profitable B2B software without institutional dilution.", 67_500, 1, [1, 2], "mutual_active"),
    ("ai_native_gtm", "Zoe Lindemann", "Usage-based outcome pricing for AI-native SaaS teams.", 51_700, 1, [0, 1, 2], "mutual_active"),
    ("churn_forensics", "Daniel Cho", "Cohort retention diagnostics and enterprise onboarding.", 29_400, 1, [1], "outbound_warming"),
    ("talent_density", "Rachel Goldstein", "Founding-engineer hiring & early org design.", 42_100, 1, [1], "mutual_dormant"),
    ("infra_angel", "Samir Kulkarni", "Angel investing in devtools, AI infra & workflow automation.", 83_600, 1, [0, 1], "outbound_cold"),
    # SMB Operations (community index 2) + bridges
    ("mainstreet_cfo", "brett_callahan", "Fractional CFO for $2M-$25M service & trade businesses.", 74_800, 2, [2], "mutual_active"),
    ("hvac_dispatch", "karla_mendez_ops", "Modernizing multi-truck home service scheduling & margins.", 41_300, 2, [2], "mutual_active"),
    ("smb_rollups", "grant_whitaker", "Acquiring and systematizing enduring B2B service companies.", 104_500, 2, [1, 2], "outbound_warming"),
    ("backoffice_auto", "nina_kowalski", "AP/AR automation, bookkeeping close & ERP migrations for SMBs.", 33_900, 2, [0, 2], "mutual_active"),
    ("agency_margins", "derek_solomon", "Utilization, retainer scoping & cash conversion for agencies.", 48_600, 2, [2], "mutual_dormant"),
    ("logistics_floor", "trevor_bates_3pl", "3PL warehouse slotting, freight audit & inventory turns.", 27_800, 2, [2], "outbound_cold"),
    ("dental_group_ops", "dr_alison_choi", "Scaling 14-location healthcare & dental practice operations.", 31_600, 2, [2], "inbound_unreciprocated"),
    ("payroll_compliance", "monica_reeves", "Multi-state payroll, workers comp & SOP playbooks.", 22_400, 2, [2], "outbound_warming"),
    ("field_sop_builder", "jason_mccoy_ops", "Turning tribal knowledge into repeatable SOPs with AI video.", 37_200, 2, [0, 2], "mutual_active"),
    ("local_lead_engine", "vanessa_cruz", "Local SEO, speed-to-lead SMS & review flywheels for SMBs.", 52_100, 2, [2], "outbound_warming"),
    ("procurement_desk", "arthur_pendleton", "Vendor negotiation and SaaS spend consolidation for 50-500 seat cos.", 26_500, 2, [1, 2], "outbound_cold"),
    ("franchise_playbook", "kelly_brennan", "Unit economics and territory density across 40+ franchise units.", 61_900, 2, [2], "mutual_dormant"),
]

WS1_LIVE_POST_TOPICS: list[tuple[int, int, str, float, bool, str | None]] = [
    (0, 22, "We cut KV-cache memory 58% in production without dropping accuracy on tool-calling tasks.\n\nThe trick wasn't 4-bit weights—it was asymmetric sink-token pinning + chunked prefill scheduling.\n\nFull latency percentiles across 1M requests:", 3.9, True, "photo"),
    (20, 18, "Outcome-based AI pricing looks great in a pitch deck until your enterprise buyer asks for a hard annual budget cap.\n\nWhat is actually closing in Q4: hybrid base platform fee + committed resolution tiers.", 4.3, False, None),
    (24, 31, "Reviewed P&Ls for 19 profitable $5M-$15M service businesses this month.\n\nEvery top-quartile operator tracks gross margin per labor hour weekly—not 20 days after month-end close.", 3.6, True, "photo"),
    (6, 27, "Replaced an SMB's voicemail tree with a 240ms voice dispatch agent across 14 HVAC trucks.\n\nBooked-job conversion after 6pm jumped from 19% to 64% in week one.", 4.6, True, "video"),
    (12, 39, "The best seed-stage moat right now isn't a wrapper around an API—it's owning the system-of-record ledger where exceptions get resolved.", 3.4, False, None),
    (1, 16, "Most multi-agent architectures fail because errors compound multiplicatively across hops.\n\n3 deterministic state-machine checkpoints beat 12 autonomous sub-agents every single time.", 3.8, False, None),
    (19, 44, "Crossed $4.2M bootstrapped ARR with 9 people.\n\nZero outbound SDRs. 80% of pipeline comes from free diagnostic calculators embedded directly in operator workflows.", 3.2, True, "photo"),
    (27, 25, "Automated 3-way invoice matching for a 60-person distributor using structured vision extraction + ERP webhooks.\n\nSaved 28 hours/week of manual keystrokes.", 3.5, False, None),
    # Near-duplicate semantic post to exercise DPP crowding discount:
    (9, 29, "We cut KV-cache memory 55% in production LLM serving using sink-token pinning and chunked prefill latency scheduling!", 2.4, False, None),
    (2, 48, "New reasoning eval drop: 40% of public benchmark gains vanish when you randomize variable names in multi-step SQL & Python tasks.", 2.6, True, "photo"),
    (15, 52, "Quick Friday note on board deck structure: put the 3 existential bottlenecks on slide 1 instead of burying them in appendix metrics.", 1.3, False, None),
    (25, 35, "Dispatch board utilization tip: hold 12% same-day buffer capacity for high-margin emergency calls rather than booking techs to 100% 3 days out.", 2.2, False, None),
    (16, 19, "Link to our updated Q4 GTM automation stack benchmark report", 0.9, False, None),
    (32, 41, "Recorded a 5-minute loom with the warehouse team and auto-generated bilingual step-by-step SOP checklists in 90 seconds.", 2.7, True, "video"),
]


WS2_COMMUNITIES = [
    ("Payments & Banking Infra", "payments-banking-infra", "#f59e0b", "Real-time rails, ledger cores, treasury & acquiring.", "195002001"),
    ("Cloud & DevTools", "cloud-devtools", "#06b6d4", "Compilers, CI/CD, observability & developer workflows.", "195002002"),
    ("Security & Identity", "security-identity", "#f43f5e", "Zero-trust auth, workload identity & cloud posture.", "195002003"),
    ("Data & AI Infra", "data-ai-infra", "#8b5cf6", "Lakehouses, streaming engines & feature stores.", "195002004"),
]


async def seed_initial_data(session: AsyncSession) -> bool:
    """Seeds global x-algorithm 9106e9f baseline + 2 complete multi-community workspaces if empty."""
    existing_ws_count = int(
        (await session.execute(select(func.count(Workspace.id)))).scalar_one() or 0
    )
    if existing_ws_count > 0:
        return False

    now = datetime.now(timezone.utc)
    rng = np.random.default_rng(20261101)

    vm_weights = ValueModelWeights.vm_ranker_defaults()
    surrogate_defaults = SurrogateCalibrationParams()
    pipeline_defaults = PipelineParams()

    param_v1 = AlgoParamVersion(
        workspace_id=None,
        version_number=1,
        label="xai-org/x-algorithm @ 9106e9f (vm-ranker baseline)",
        source="upstream_xalgo",
        upstream_commit_sha=UPSTREAM_COMMIT_SHA,
        weights_json=vm_weights.to_dict(),
        surrogate_params_json=surrogate_defaults.to_dict(),
        thresholds_json={
            "watch_threshold": 65.0,
            "surge_threshold": 82.0,
            "pipeline": pipeline_defaults.to_dict(),
        },
        holdout_metrics_json={
            "precision": 0.78,
            "recall": 0.71,
            "f05": 0.765,
            "brier": 0.142,
            "matured_posts": 50,
        },
        changelog_note=(
            "Initial baseline synced from xai-org/x-algorithm HEAD 9106e9f "
            "(29-head ValueModelWeights, +15.0 mutual reply boost, 0.75x OON/reply rescore, DppTheta=0.65)."
        ),
        is_active=True,
        created_at=now - timedelta(days=3),
    )
    session.add(param_v1)
    await session.flush()

    session.add(
        UpstreamAlgoEvent(
            base_sha="9106e9f",
            head_sha="9106e9f",
            detected_at=now - timedelta(hours=4),
            watched_files_changed_json=[],
            param_diffs_json=[],
            structural_summary="Verified in-sync with upstream xai-org/x-algorithm HEAD 9106e9f (Oct 31, 2026).",
            status="in_sync",
            staged_param_version_id=param_v1.id,
        )
    )

    # =========================================================================
    # WORKSPACE 1: K=3 Communities + my_x_handle="operator_hq"
    # =========================================================================
    ws1 = Workspace(
        name="AI, Startups & SMB Ops",
        slug="ai-startups-smb-ops",
        my_x_handle="operator_hq",
        x_data_provider="fixture",
        llm_provider="google_gemini",
        radar_poll_minutes=10,
        use_x_lists=False,
        daily_spend_cap_usd=2.0,
        monthly_spend_cap_usd=25.0,
        watch_threshold=65.0,
        surge_threshold=82.0,
        quiet_hours_start="23:00",
        quiet_hours_end="06:30",
        max_alerts_per_hour=10,
        webhook_urls_json=[],
    )
    session.add(ws1)
    await session.flush()

    ws1_comm_defs = [
        ("Applied AI", "applied-ai", "#38bdf8", "Production LLM engineering, agents, inference & evals.", "184001001"),
        ("Venture & Startups", "venture-startups", "#a78bfa", "Seed-to-Series B founders, GTM mechanics & vertical SaaS.", "184001002"),
        ("SMB Operations", "smb-operations", "#34d399", "Cashflow, field dispatch, back-office automation & rollups.", "184001003"),
    ]
    ws1_comms: list[Community] = []
    for idx, (c_name, c_slug, c_color, c_desc, c_list) in enumerate(ws1_comm_defs):
        comm = Community(
            workspace_id=ws1.id,
            name=c_name,
            slug=c_slug,
            color=c_color,
            description=c_desc,
            x_list_id=c_list,
            sort_order=idx,
        )
        session.add(comm)
        ws1_comms.append(comm)
    await session.flush()

    ws1_accounts: list[TrackedAccount] = []
    for idx, (handle, disp, bio, followers, primary_c, comm_idxs, rel_state) in enumerate(WS1_ACCOUNTS):
        follows_me = rel_state in ("mutual_active", "mutual_dormant", "inbound_unreciprocated")
        followed_by_me = rel_state in ("mutual_active", "mutual_dormant", "outbound_warming", "outbound_cold")
        n_circles = len(comm_idxs)
        if n_circles >= 3:
            b_role = "multi_circle_connector"
            part_coeff = round(0.84 + 0.08 * float(rng.random()), 3)
            brokerage = round(0.76 + 0.14 * float(rng.random()), 3)
        elif n_circles == 2:
            b_role = "two_way_bridge"
            part_coeff = round(0.62 + 0.14 * float(rng.random()), 3)
            brokerage = round(0.58 + 0.18 * float(rng.random()), 3)
        else:
            b_role = "provincial"
            part_coeff = round(0.08 + 0.18 * float(rng.random()), 3)
            brokerage = round(0.22 + 0.20 * float(rng.random()), 3)

        bridge_score = round(min(100.0, (0.55 * part_coeff + 0.45 * brokerage) * 100.0), 1)
        mass = max(1e-11, (followers / 2_500_000.0) * (1.5 if n_circles > 1 else 0.9))
        u_cred = round(UserCredV2.from_mass(10_000 + idx, mass).score, 1)
        cat_lift = round(1.15 + 0.85 * float(rng.random()) + (0.35 if n_circles > 1 else 0.0), 2)
        s_core = round(3.0 + 7.5 * float(rng.random()), 2)
        salsa_auth = round(0.25 + 0.70 * float(rng.random()), 3)
        salsa_scout = round(0.30 + 0.65 * float(rng.random()), 3)
        hh_score = round(
            min(99.0, 22.0 + 18.0 * cat_lift + 2.4 * s_core + 24.0 * salsa_auth + 0.12 * u_cred),
            1,
        )
        median_48h = max(1_800, int(followers * (0.14 + 0.12 * float(rng.random()))))

        acct = TrackedAccount(
            workspace_id=ws1.id,
            x_user_id=str(100_100_000 + idx),
            handle=handle,
            display_name=disp,
            bio=bio,
            followers_count=followers,
            following_count=int(320 + (idx * 37) % 1_100),
            followers_7d_ago=max(100, int(followers * (0.975 - 0.015 * float(rng.random())))),
            is_tracked=True,
            follows_me=follows_me,
            followed_by_me=followed_by_me,
            relationship_state=rel_state,
            last_interaction_at=(
                now - timedelta(days=int(rng.integers(1, 6)))
                if rel_state in ("mutual_active", "outbound_warming")
                else (now - timedelta(days=int(rng.integers(20, 45))) if rel_state == "mutual_dormant" else None)
            ),
            mutual_interaction_count_30d=(
                int(rng.integers(5, 18))
                if rel_state == "mutual_active"
                else (int(rng.integers(1, 4)) if rel_state == "outbound_warming" else 0)
            ),
            heavy_hitter_score=hh_score,
            catalysis_lift=cat_lift,
            s_core_depth=s_core,
            salsa_authority=salsa_auth,
            salsa_scout=salsa_scout,
            user_cred_v2=u_cred,
            bridge_score=bridge_score,
            participation_coeff=part_coeff,
            burt_brokerage=brokerage,
            bridge_role=b_role,
            sub_pocket_label=f"{ws1_comms[primary_c].name} Core #{(idx % 3) + 1}",
            baseline_curves_json=_build_baseline_curves(median_48h),
        )
        session.add(acct)
        ws1_accounts.append(acct)
    await session.flush()

    # Memberships across K=3 communities
    for idx, acct in enumerate(ws1_accounts):
        _, _, _, _, primary_c, comm_idxs, _ = WS1_ACCOUNTS[idx]
        for c_idx, comm_obj in enumerate(ws1_comms):
            if c_idx in comm_idxs:
                known_w = 0.85 if c_idx == primary_c else 0.52
                int_w = round(known_w * (0.85 + 0.15 * float(rng.random())), 3)
                session.add(
                    AccountCommunityMembership(
                        workspace_id=ws1.id,
                        account_id=acct.id,
                        community_id=comm_obj.id,
                        is_manual_tag=True,
                        nmf_known_for=round(known_w, 3),
                        nmf_interested_in=int_w,
                    )
                )

    # Behavioral edges (within-community + cross-community bridge edges; indices 3 & 17 are dormant high-follower ghosts)
    ghost_indices = {3, 17}
    edge_pairs_seen: set[tuple[int, int, str]] = set()
    for i, src in enumerate(ws1_accounts):
        if i in ghost_indices:
            continue
        src_c = WS1_ACCOUNTS[i][4]
        src_comms = WS1_ACCOUNTS[i][5]
        for j, tgt in enumerate(ws1_accounts):
            if i == j or j in ghost_indices:
                continue
            tgt_c = WS1_ACCOUNTS[j][4]
            tgt_comms = WS1_ACCOUNTS[j][5]
            same_c = src_c == tgt_c
            if same_c:
                prob = 0.42
            elif len(src_comms) > 1 and tgt_c in src_comms:
                prob = 0.36
            elif len(tgt_comms) > 1 and src_c in tgt_comms:
                prob = 0.16
            else:
                prob = 0.01
            if float(rng.random()) < prob:
                e_type = ["follow", "reply", "quote", "repost"][int(rng.integers(0, 4))]
                key = (src.id, tgt.id, e_type)
                if key not in edge_pairs_seen:
                    edge_pairs_seen.add(key)
                    cnt = int(rng.integers(1, 9))
                    session.add(
                        RelationshipEdge(
                            workspace_id=ws1.id,
                            source_account_id=src.id,
                            target_account_id=tgt.id,
                            edge_type=e_type,
                            interaction_count=cnt,
                            decayed_weight=round(cnt * (0.55 + 0.45 * float(rng.random())), 3),
                            last_event_at=now - timedelta(hours=int(rng.integers(2, 240))),
                        )
                    )

    # Seed 14 live 15-60m posts + 50 matured 48h historical posts (64 total in Workspace 1)
    active_embeddings: list[list[float]] = []
    ctx = QueryScoringContext(effective_oon_weight=0.75)

    for p_idx, (acct_idx, age_m, text, mult, has_media, m_type) in enumerate(WS1_LIVE_POST_TOPICS):
        author = ws1_accounts[acct_idx]
        is_mutual = author.follows_me and author.followed_by_me
        p50_at_age = int(
            author.baseline_curves_json["p50"].get(str(min(SNAPSHOT_AGES, key=lambda a: abs(a - age_m))), 1000)
        )
        views = max(180, int(p50_at_age * mult))
        has_url = "Link to" in text
        likes = int(views * (0.024 if mult > 3.0 else 0.011))
        replies = int(views * (0.0048 if mult > 3.0 else 0.0011))
        quotes = int(views * (0.0022 if mult > 3.0 else 0.0004))
        reposts = int(views * (0.0035 if mult > 3.0 else 0.0012))
        bookmarks = int(views * (0.0085 if mult > 3.0 else 0.0018))

        emb = compute_text_embedding(text)
        dpp_mult = round(dpp_novelty_multiplier(emb, active_embeddings, theta=0.65), 3)
        active_embeddings.append(emb)

        cand_in = build_candidate_scoring_inputs(
            ObservablePostMetrics(
                views=views,
                likes=likes,
                replies=replies,
                reposts=reposts,
                quotes=quotes,
                bookmarks=bookmarks,
                text=text,
                has_url=has_url,
                has_media=has_media,
                media_type=m_type,
                video_duration_ms=(22_000 if m_type == "video" else None),
            ),
            author_id=acct_idx + 1,
            in_network=author.followed_by_me,
            is_mutual_follow_author=is_mutual,
        )
        vm_res = compute_value_scores(vm_weights, ctx, [cand_in])
        vm_score = round(vm_res.scores[0], 4)

        vel_z = round((mult - 1.0) * 1.65, 2)
        seismic = round(min(1.0, 0.18 + 0.19 * mult), 3)
        cat_boost = round(0.18 * mult if mult > 3.0 else 0.02, 2)
        raw_b_score = (36.0 + 13.5 * vel_z + 18.0 * seismic + 8.0 * min(vm_score, 2.5) + 12.0 * cat_boost) * (
            0.55 + 0.45 * dpp_mult
        )
        b_score = round(min(98.5, max(12.0, raw_b_score)), 1)
        rec_action = recommend_engagement_action(
            is_mutual_follow_author=is_mutual,
            is_reply=False,
            is_retweet=False,
            breakout_score=b_score,
            watch_threshold=ws1.watch_threshold,
        )

        created_x = now - timedelta(minutes=age_m)
        post = Post(
            workspace_id=ws1.id,
            x_post_id=str(1895001000 + p_idx),
            author_account_id=author.id,
            text=text,
            created_at_x=created_x,
            is_reply=False,
            is_retweet=False,
            is_quote=(p_idx in (1, 5)),
            quoted_post_id=(str(1894000100 + p_idx) if p_idx in (1, 5) else None),
            has_url=has_url,
            has_media=has_media,
            media_type=m_type,
            video_duration_ms=(22_000 if m_type == "video" else None),
            embedding_json=emb,
            latest_views=views,
            latest_likes=likes,
            latest_replies=replies,
            latest_quotes=quotes,
            latest_reposts=reposts,
            latest_bookmarks=bookmarks,
            breakout_score=b_score,
            velocity_z_score=vel_z,
            seismic_infectiousness=seismic,
            value_model_score=vm_score,
            dpp_novelty_multiplier=dpp_mult,
            catalysis_boost=cat_boost,
            recommended_action=rec_action,
            matured_48h=False,
        )
        session.add(post)
        await session.flush()

        for snap_age in (15, 30, 45, 60):
            if snap_age <= age_m:
                ratio = snap_age / float(age_m)
                session.add(
                    PostSnapshot(
                        post_id=post.id,
                        age_minutes=snap_age,
                        captured_at=created_x + timedelta(minutes=snap_age),
                        views=max(50, int(views * ratio)),
                        likes=int(likes * ratio),
                        replies=int(replies * ratio),
                        quotes=int(quotes * ratio),
                        reposts=int(reposts * ratio),
                        bookmarks=int(bookmarks * ratio),
                        breakout_score_at_capture=round(b_score * (0.85 + 0.15 * ratio), 1),
                    )
                )

        tier = "surge" if b_score >= ws1.surge_threshold else ("watch" if b_score >= ws1.watch_threshold else "none")
        prob_p = round(1.0 / (1.0 + math.exp(-(b_score - 68.0) / 9.5)), 4)
        session.add(
            BreakoutPrediction(
                workspace_id=ws1.id,
                post_id=post.id,
                param_version_id=param_v1.id,
                predicted_at=now - timedelta(minutes=max(1, age_m - 15)),
                post_age_minutes=min(age_m, 30),
                breakout_probability=prob_p,
                breakout_score=b_score,
                alert_tier=tier,
            )
        )

        if tier in ("surge", "watch"):
            actor_idx = (acct_idx + 5) % len(ws1_accounts)
            if actor_idx in ghost_indices:
                actor_idx = (actor_idx + 1) % len(ws1_accounts)
            actor = ws1_accounts[actor_idx]
            session.add(
                PostInteraction(
                    workspace_id=ws1.id,
                    post_id=post.id,
                    actor_account_id=actor.id,
                    actor_x_user_id=actor.x_user_id,
                    actor_handle=actor.handle,
                    actor_followers=actor.followers_count,
                    interaction_type="quote" if p_idx % 2 == 0 else "reply",
                    occurred_at=created_x + timedelta(minutes=12),
                )
            )
            # Also add a Heavy Hitter / Bridge catalyst interaction
            cat_idx = [0, 1, 6, 12, 14, 20, 24][p_idx % 7]
            if cat_idx != acct_idx:
                cat_actor = ws1_accounts[cat_idx]
                session.add(
                    PostInteraction(
                        workspace_id=ws1.id,
                        post_id=post.id,
                        actor_account_id=cat_actor.id,
                        actor_x_user_id=cat_actor.x_user_id,
                        actor_handle=cat_actor.handle,
                        actor_followers=cat_actor.followers_count,
                        interaction_type="reply" if p_idx % 2 == 0 else "quote",
                        occurred_at=created_x + timedelta(minutes=8),
                    )
                )
            session.add(
                AlertDelivery(
                    workspace_id=ws1.id,
                    post_id=post.id,
                    event_type=f"breakout.{tier}",
                    channel="sse",
                    alert_tier=tier,
                    payload_json={
                        "x_post_id": post.x_post_id,
                        "handle": author.handle,
                        "breakout_score": b_score,
                        "recommended_action": rec_action,
                    },
                    status="delivered",
                    attempts=1,
                    created_at=now - timedelta(minutes=max(1, age_m - 15)),
                )
            )

    # 50 matured 48h historical trajectories for Epic 3 Scorecard & Backtesting Sandbox
    archetypes = (
        [("true_breakout", 4.4, 3.8)] * 14
        + [("fizzle_fp", 2.9, 1.7)] * 5
        + [("slow_burn_fn", 1.5, 3.4)] * 5
        + [("steady_tn", 1.1, 1.15)] * 26
    )
    for h_idx, (kind, early_mult, final_mult) in enumerate(archetypes):
        author = ws1_accounts[h_idx % len(ws1_accounts)]
        is_mutual = author.follows_me and author.followed_by_me
        created_x = now - timedelta(hours=52 + h_idx * 4)
        p50_48h = int(author.baseline_curves_json["p50"]["2880"])
        final_views = int(p50_48h * final_mult * (0.92 + 0.16 * float(rng.random())))
        actual_mult = round(final_views / max(p50_48h, 1), 2)
        is_true_bo = actual_mult >= 3.0 and final_views >= 5_000

        text = (
            f"Field notes #{h_idx + 1}: operational lessons on {author.sub_pocket_label} "
            f"latency, unit margins, and repeatable customer distribution."
        )
        emb = compute_text_embedding(text)
        early_b_score = round(
            min(96.0, max(18.0, 32.0 + 18.5 * early_mult + 4.0 * float(rng.normal(0, 1)))),
            1,
        )
        rec_action = recommend_engagement_action(
            is_mutual_follow_author=is_mutual,
            is_reply=False,
            is_retweet=False,
            breakout_score=early_b_score,
            watch_threshold=ws1.watch_threshold,
        )

        h_post = Post(
            workspace_id=ws1.id,
            x_post_id=str(1892000000 + h_idx),
            author_account_id=author.id,
            text=text,
            created_at_x=created_x,
            is_reply=False,
            is_retweet=False,
            is_quote=(h_idx % 7 == 0),
            has_url=(h_idx % 6 == 0),
            has_media=(h_idx % 3 == 0),
            media_type=("photo" if h_idx % 3 == 0 else None),
            embedding_json=emb,
            latest_views=final_views,
            latest_likes=int(final_views * 0.018),
            latest_replies=int(final_views * 0.0024),
            latest_quotes=int(final_views * 0.0011),
            latest_reposts=int(final_views * 0.0022),
            latest_bookmarks=int(final_views * 0.0045),
            breakout_score=early_b_score,
            velocity_z_score=round((early_mult - 1.0) * 1.5, 2),
            seismic_infectiousness=round(min(0.95, 0.15 + 0.18 * early_mult), 3),
            value_model_score=round(0.35 + 0.25 * early_mult, 4),
            dpp_novelty_multiplier=round(0.82 + 0.16 * float(rng.random()), 3),
            catalysis_boost=round(0.25 if kind == "true_breakout" else 0.04, 2),
            recommended_action=rec_action,
            matured_48h=True,
            actual_48h_views=final_views,
            actual_48h_multiple=actual_mult,
            is_true_breakout_48h=is_true_bo,
        )
        session.add(h_post)
        await session.flush()

        for s_age in SNAPSHOT_AGES:
            base_age_p50 = int(author.baseline_curves_json["p50"][str(s_age)])
            interp = (s_age / 2880.0) ** 0.45
            cur_mult = early_mult * (1.0 - interp) + actual_mult * interp
            s_views = max(40, int(base_age_p50 * cur_mult))
            session.add(
                PostSnapshot(
                    post_id=h_post.id,
                    age_minutes=s_age,
                    captured_at=created_x + timedelta(minutes=s_age),
                    views=s_views,
                    likes=int(s_views * 0.018),
                    replies=int(s_views * 0.0024),
                    quotes=int(s_views * 0.0011),
                    reposts=int(s_views * 0.0022),
                    bookmarks=int(s_views * 0.0045),
                    breakout_score_at_capture=early_b_score,
                )
            )

        pred_prob = round(1.0 / (1.0 + math.exp(-(early_b_score - 65.0) / 9.0)), 4)
        pred_positive = early_b_score >= ws1.watch_threshold
        y_true = 1.0 if is_true_bo else 0.0
        brier = round((pred_prob - y_true) ** 2, 5)
        if pred_positive and is_true_bo:
            out_cls = "true_positive"
        elif pred_positive and not is_true_bo:
            out_cls = "false_positive"
        elif (not pred_positive) and is_true_bo:
            out_cls = "false_negative"
        else:
            out_cls = "true_negative"

        session.add(
            BreakoutPrediction(
                workspace_id=ws1.id,
                post_id=h_post.id,
                param_version_id=param_v1.id,
                predicted_at=created_x + timedelta(minutes=30),
                post_age_minutes=30,
                breakout_probability=pred_prob,
                breakout_score=early_b_score,
                alert_tier=("surge" if early_b_score >= ws1.surge_threshold else ("watch" if pred_positive else "none")),
                evaluated_at_48h=created_x + timedelta(hours=48),
                brier_error=brier,
                outcome_class=out_cls,
            )
        )

    # 8 Rising Creator Candidates (<50k followers) for Epic 4
    rising_defs = [
        ("kernel_whisperer", "Aiden Sato", "Writing custom CUDA & Metal kernels from scratch.", 2_150, 0.19, 8, 6, 7.4, {"applied-ai": 0.86, "venture-startups": 0.14}),
        ("workflow_plumber", "Maya Lin", "Automating ugly back-office PDFs & EDI feeds for regional distributors.", 6_400, 0.14, 9, 6, 5.8, {"smb-operations": 0.68, "applied-ai": 0.32}),
        ("pricing_teardown", "Lucas Meyer", "Weekly teardowns of vertical AI & usage-based B2B pricing pages.", 14_200, 0.11, 7, 5, 4.9, {"venture-startups": 0.74, "applied-ai": 0.26}),
        ("shopfloor_metrics", "carmen_delgado", "OEE, preventive maintenance & shift handoff software for light mfg.", 4_800, 0.16, 6, 4, 5.2, {"smb-operations": 0.88, "venture-startups": 0.12}),
        ("eval_harness_dev", "Rohan Gupta", "Deterministic replay sandboxes for browser & computer-use agents.", 9_300, 0.15, 7, 5, 6.1, {"applied-ai": 0.81, "venture-startups": 0.19}),
        ("service_mna_notes", "Parker Ellis", "SBA 7(a) deal structures and 90-day post-close playbooks.", 21_500, 0.09, 8, 5, 4.2, {"smb-operations": 0.64, "venture-startups": 0.36}),
        ("sql_agent_bench", "Freya Lindholm", "Text-to-SQL semantic layers that survive dirty enterprise schemas.", 18_900, 0.12, 6, 4, 4.6, {"applied-ai": 0.72, "smb-operations": 0.28}),
        ("micro_vertical_hq", "Noah Sterling", "Bootstrapping niche compliance tools to $1M ARR.", 34_600, 0.08, 7, 4, 3.8, {"venture-startups": 0.58, "smb-operations": 0.42}),
    ]
    for r_idx, (r_handle, r_name, r_bio, r_foll, r_growth, s_cnt, above3, med_mult, proj) in enumerate(rising_defs):
        paw = round(min(98.0, 48.0 + 6.2 * med_mult + 40.0 * r_growth), 1)
        peer_resp = round(min(97.0, 52.0 + 5.5 * (8 - r_idx)), 1)
        comb = round(0.55 * paw + 0.45 * peer_resp, 1)
        cand = DiscoveredCandidate(
            workspace_id=ws1.id,
            x_user_id=str(700_200_000 + r_idx),
            handle=r_handle,
            display_name=r_name,
            bio=r_bio,
            followers_count=r_foll,
            followers_growth_rate=r_growth,
            sampled_posts_count=s_cnt,
            posts_above_3x_count=above3,
            median_reach_multiple=med_mult,
            punch_above_weight_score=paw,
            peer_respect_score=peer_resp,
            combined_rising_score=comb,
            community_projection_json=proj,
            top_posts_json=[
                {
                    "x_post_id": str(1896100000 + r_idx),
                    "text": f"How we benchmarked {r_handle}'s core workflow and hit {med_mult}x organic reach.",
                    "views": int(r_foll * med_mult),
                    "reach_multiple": med_mult,
                }
            ],
            status="active",
        )
        session.add(cand)
        await session.flush()

        for e_off, e_type, e_mult in ((0, "follow", 3.0), (6, "quote", 2.0), (13, "reply", 1.0)):
            endorser = ws1_accounts[(r_idx * 3 + e_off) % len(ws1_accounts)]
            session.add(
                PeerEndorsement(
                    workspace_id=ws1.id,
                    candidate_id=cand.id,
                    endorser_account_id=endorser.id,
                    endorsement_type=e_type,
                    source_x_post_id=(str(1896200000 + r_idx * 10 + e_off) if e_type != "follow" else None),
                    weight_contribution=round(e_mult * (endorser.heavy_hitter_score / 100.0), 3),
                    occurred_at=now - timedelta(hours=12 + r_idx * 5 + e_off),
                )
            )

    session.add(
        ApiCreditLedger(
            workspace_id=ws1.id,
            provider="fixture",
            endpoint="fixture://bootstrap_seed",
            units_consumed=0.0,
            estimated_usd=0.0,
            reconciled_balance_units=None,
            created_at=now - timedelta(minutes=5),
        )
    )

    # =========================================================================
    # WORKSPACE 2: K=4 Communities & my_x_handle=None (Pure Ecosystem Research)
    # =========================================================================
    ws2 = Workspace(
        name="FinTech & DevTools Research",
        slug="fintech-devtools-research",
        my_x_handle=None,
        x_data_provider="fixture",
        llm_provider="xai_grok",
        radar_poll_minutes=15,
        use_x_lists=True,
        daily_spend_cap_usd=3.0,
        monthly_spend_cap_usd=35.0,
        watch_threshold=64.0,
        surge_threshold=80.0,
        max_alerts_per_hour=12,
        webhook_urls_json=[],
    )
    session.add(ws2)
    await session.flush()

    ws2_comms: list[Community] = []
    for idx, (c_name, c_slug, c_color, c_desc, c_list) in enumerate(WS2_COMMUNITIES):
        comm = Community(
            workspace_id=ws2.id,
            name=c_name,
            slug=c_slug,
            color=c_color,
            description=c_desc,
            x_list_id=c_list,
            sort_order=idx,
        )
        session.add(comm)
        ws2_comms.append(comm)
    await session.flush()

    ws2_handles = [
        ("fednow_rails", "Claire Donovan", 0, [0]),
        ("ledger_invariant", "Mateo Silva", 0, [0, 3]),
        ("treasury_api", "Sonia Kapoor", 0, [0]),
        ("acquiring_geek", "Graham Bell", 0, [0, 2]),
        ("stablecoin_clearing", "Tariq Hassan", 0, [0]),
        ("underwriting_ml", "Iris Chen", 0, [0, 3]),
        ("rust_build_cache", "Lukas Weber", 1, [1]),
        ("ebpf_tracer", "Mina Park", 1, [1, 2]),
        ("ci_flake_hunter", "Evan Ross", 1, [1]),
        ("wasm_edge_rt", "Klara Lind", 1, [1, 3]),
        ("otel_collector", "Sanjay Verma", 1, [1]),
        ("repo_monolith", "Hannah Cole", 1, [1]),
        ("passkey_architect", "Devon Brooks", 2, [2]),
        ("workload_spiffe", "Nora Lindqvist", 2, [1, 2]),
        ("fraud_graph_sec", "julian_mercer", 2, [0, 2, 3]),
        ("cloud_iam_audit", "fatima_zahra", 2, [2]),
        ("supplychain_sbom", "caleb_wright", 2, [1, 2]),
        (" enclave_kms", "yuna_kim_sec", 2, [2]),
        ("iceberg_catalog", "pieter_vandenberg", 3, [3]),
        ("streaming_flink", "arjun_nair_data", 3, [1, 3]),
        ("duckdb_analytics", "emma_larsson", 3, [3]),
        ("feature_store_rt", "marcus_okafor", 3, [0, 1, 3]),
        ("columnar_storage", "wei_zhang_db", 3, [3]),
        ("query_optimizer", "olivia_grant", 3, [3]),
    ]

    ws2_accounts: list[TrackedAccount] = []
    for idx, (raw_h, disp, prim_c, c_list_idxs) in enumerate(ws2_handles):
        h = raw_h.strip()
        foll = 18_000 + (idx * 6_700) % 95_000
        n_c = len(c_list_idxs)
        b_role = "multi_circle_connector" if n_c >= 3 else ("two_way_bridge" if n_c == 2 else "provincial")
        b_score = 81.5 if n_c >= 3 else (66.0 if n_c == 2 else 19.5)
        u_cred = round(UserCredV2.from_mass(20_000 + idx, foll / 2_000_000.0).score, 1)
        acct = TrackedAccount(
            workspace_id=ws2.id,
            x_user_id=str(200_100_000 + idx),
            handle=h,
            display_name=disp,
            bio=f"Researching {ws2_comms[prim_c].name} architecture and production reliability.",
            followers_count=foll,
            following_count=410 + idx * 19,
            followers_7d_ago=int(foll * 0.98),
            is_tracked=True,
            follows_me=False,
            followed_by_me=False,
            relationship_state="none",
            heavy_hitter_score=round(58.0 + (idx * 7) % 36, 1),
            catalysis_lift=round(1.3 + 0.1 * (idx % 6), 2),
            s_core_depth=round(4.0 + (idx % 5), 2),
            salsa_authority=round(0.35 + 0.02 * idx, 3),
            salsa_scout=round(0.40 + 0.02 * ((idx + 3) % 15), 3),
            user_cred_v2=u_cred,
            bridge_score=b_score,
            participation_coeff=round(b_score / 100.0, 3),
            burt_brokerage=round(min(0.92, b_score / 105.0), 3),
            bridge_role=b_role,
            sub_pocket_label=f"{ws2_comms[prim_c].name} Cluster",
            baseline_curves_json=_build_baseline_curves(int(foll * 0.16)),
        )
        session.add(acct)
        ws2_accounts.append(acct)
    await session.flush()

    for idx, acct in enumerate(ws2_accounts):
        prim_c = ws2_handles[idx][2]
        for c_i in ws2_handles[idx][3]:
            session.add(
                AccountCommunityMembership(
                    workspace_id=ws2.id,
                    account_id=acct.id,
                    community_id=ws2_comms[c_i].id,
                    is_manual_tag=True,
                    nmf_known_for=(0.86 if c_i == prim_c else 0.54),
                    nmf_interested_in=(0.82 if c_i == prim_c else 0.58),
                )
            )

    ws2_edge_seen: set[tuple[int, int, str]] = set()
    for i, src in enumerate(ws2_accounts):
        prim_c = ws2_handles[i][2]
        tagged_cs = ws2_handles[i][3]
        # Intra-community behavioral connections
        comm_start = prim_c * 6
        for local_off in (1, 2, 3):
            tgt_idx = comm_start + ((i - comm_start + local_off) % 6)
            if tgt_idx != i:
                tgt = ws2_accounts[tgt_idx]
                etype = "follow" if local_off != 2 else "reply"
                key = (src.id, tgt.id, etype)
                if key not in ws2_edge_seen:
                    ws2_edge_seen.add(key)
                    session.add(
                        RelationshipEdge(
                            workspace_id=ws2.id,
                            source_account_id=src.id,
                            target_account_id=tgt.id,
                            edge_type=etype,
                            interaction_count=3,
                            decayed_weight=2.4,
                            last_event_at=now - timedelta(days=2),
                        )
                    )
        # Cross-community bridge edges for multi-tagged connectors
        if len(tagged_cs) > 1:
            for other_c in tagged_cs:
                if other_c == prim_c:
                    continue
                for b_off in (0, 2):
                    tgt_idx = other_c * 6 + ((i + b_off) % 6)
                    tgt = ws2_accounts[tgt_idx]
                    etype = "quote" if b_off == 0 else "follow"
                    key = (src.id, tgt.id, etype)
                    if key not in ws2_edge_seen:
                        ws2_edge_seen.add(key)
                        session.add(
                            RelationshipEdge(
                                workspace_id=ws2.id,
                                source_account_id=src.id,
                                target_account_id=tgt.id,
                                edge_type=etype,
                                interaction_count=3,
                                decayed_weight=2.8,
                                last_event_at=now - timedelta(days=1),
                            )
                        )

    for p_i in range(24):
        author = ws2_accounts[p_i % len(ws2_accounts)]
        is_live = p_i < 8
        age_mins = (20 + p_i * 5) if is_live else 2880
        created_x = now - timedelta(minutes=age_mins) if is_live else now - timedelta(hours=60 + p_i * 3)
        mult = 3.7 if p_i % 3 == 0 else 1.4
        views = int(author.followers_count * 0.15 * mult)
        b_score = 84.5 if p_i % 3 == 0 else 48.0
        txt = f"Deep-dive #{p_i + 1}: double-entry ledger throughput, zero-trust mTLS & Apache Iceberg compaction."
        p_obj = Post(
            workspace_id=ws2.id,
            x_post_id=str(1995002000 + p_i),
            author_account_id=author.id,
            text=txt,
            created_at_x=created_x,
            is_reply=False,
            is_retweet=False,
            is_quote=(p_i % 4 == 0),
            embedding_json=compute_text_embedding(txt),
            latest_views=views,
            latest_likes=int(views * 0.02),
            latest_replies=int(views * 0.003),
            latest_quotes=int(views * 0.0015),
            latest_reposts=int(views * 0.0025),
            latest_bookmarks=int(views * 0.006),
            breakout_score=b_score,
            velocity_z_score=round((mult - 1.0) * 1.6, 2),
            seismic_infectiousness=0.72 if b_score > 70 else 0.31,
            value_model_score=1.18 if b_score > 70 else 0.42,
            dpp_novelty_multiplier=0.94,
            catalysis_boost=0.32 if b_score > 70 else 0.05,
            recommended_action=("quote_oon_safe" if b_score >= ws2.watch_threshold else "watch"),
            matured_48h=(not is_live),
            actual_48h_views=(views if not is_live else None),
            actual_48h_multiple=(mult if not is_live else None),
            is_true_breakout_48h=((mult >= 3.0) if not is_live else None),
        )
        session.add(p_obj)
        await session.flush()
        session.add(
            PostSnapshot(
                post_id=p_obj.id,
                age_minutes=min(age_mins, 30),
                captured_at=created_x + timedelta(minutes=min(age_mins, 30)),
                views=max(100, int(views * 0.45)),
                likes=int(views * 0.009),
                replies=int(views * 0.0012),
                quotes=int(views * 0.0007),
                reposts=int(views * 0.001),
                bookmarks=int(views * 0.0028),
                breakout_score_at_capture=b_score,
            )
        )
        if p_i % 3 == 0:
            cat_actor2 = ws2_accounts[(p_i + 1) % len(ws2_accounts)]
            session.add(
                PostInteraction(
                    workspace_id=ws2.id,
                    post_id=p_obj.id,
                    actor_account_id=cat_actor2.id,
                    actor_x_user_id=cat_actor2.x_user_id,
                    actor_handle=cat_actor2.handle,
                    actor_followers=cat_actor2.followers_count,
                    interaction_type="quote",
                    occurred_at=created_x + timedelta(minutes=10),
                )
            )

    for c_i in range(6):
        cand2 = DiscoveredCandidate(
            workspace_id=ws2.id,
            x_user_id=str(800_300_000 + c_i),
            handle=f"infra_builder_{c_i + 1}",
            display_name=f"Infra Builder {c_i + 1}",
            bio="Low-latency distributed systems & payment state machines.",
            followers_count=3_200 + c_i * 4_100,
            followers_growth_rate=0.14,
            sampled_posts_count=7,
            posts_above_3x_count=5,
            median_reach_multiple=5.4,
            punch_above_weight_score=84.0 - c_i * 2.5,
            peer_respect_score=80.0 - c_i * 2.0,
            combined_rising_score=82.2 - c_i * 2.2,
            community_projection_json={
                "payments-banking-infra": 0.45,
                "cloud-devtools": 0.55,
            },
            top_posts_json=[],
            status="active",
        )
        session.add(cand2)

    await session.flush()

    # Run real Epic 1 Graph Analytics pipeline (Seeded NMF, SALSA, s-Core, UserCredV2, Bridges & CRM)
    from tweettracker.graph.service import recompute_workspace_graph

    await recompute_workspace_graph(session, ws1.id, commit=False, reference_time=now)
    await recompute_workspace_graph(session, ws2.id, commit=False, reference_time=now)

    await session.commit()
    return True


def _ensure_additive_columns(sync_conn: Connection) -> None:
    """Idempotently adds newly introduced columns to pre-existing SQLite/Postgres tables."""
    insp = inspect(sync_conn)
    existing_tables = set(insp.get_table_names())

    migrations: dict[str, list[tuple[str, str]]] = {
        "workspaces": [
            (
                "x_list_ids_json",
                "ALTER TABLE workspaces ADD COLUMN x_list_ids_json JSON",
            ),
            (
                "monthly_unit_budget",
                "ALTER TABLE workspaces ADD COLUMN monthly_unit_budget INTEGER NOT NULL DEFAULT 100000",
            ),
            (
                "hot_posts_min_projected_views",
                "ALTER TABLE workspaces ADD COLUMN hot_posts_min_projected_views INTEGER NOT NULL DEFAULT 5000",
            ),
            (
                "last_discovery_sync_at",
                "ALTER TABLE workspaces ADD COLUMN last_discovery_sync_at TIMESTAMP",
            ),
        ],
        "tracked_accounts": [
            (
                "is_auto_tracked",
                "ALTER TABLE tracked_accounts ADD COLUMN is_auto_tracked BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            (
                "is_muted",
                "ALTER TABLE tracked_accounts ADD COLUMN is_muted BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            (
                "auto_tracked_at",
                "ALTER TABLE tracked_accounts ADD COLUMN auto_tracked_at TIMESTAMP",
            ),
            (
                "last_follow_synced_at",
                "ALTER TABLE tracked_accounts ADD COLUMN last_follow_synced_at TIMESTAMP",
            ),
            (
                "core_affinity_score",
                "ALTER TABLE tracked_accounts ADD COLUMN core_affinity_score FLOAT NOT NULL DEFAULT 0.0",
            ),
            (
                "yield_score",
                "ALTER TABLE tracked_accounts ADD COLUMN yield_score FLOAT NOT NULL DEFAULT 0.0",
            ),
            (
                "reply_sample_count",
                "ALTER TABLE tracked_accounts ADD COLUMN reply_sample_count INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "avg_reply_views",
                "ALTER TABLE tracked_accounts ADD COLUMN avg_reply_views FLOAT NOT NULL DEFAULT 0.0",
            ),
            (
                "attributed_follows_30d",
                "ALTER TABLE tracked_accounts ADD COLUMN attributed_follows_30d FLOAT NOT NULL DEFAULT 0.0"
            ),
        ],
        "discovered_candidates": [
            (
                "avatar_url",
                "ALTER TABLE discovered_candidates ADD COLUMN avatar_url VARCHAR(512)",
            ),
            (
                "following_count",
                "ALTER TABLE discovered_candidates ADD COLUMN following_count INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "core_affinity_score",
                "ALTER TABLE discovered_candidates ADD COLUMN core_affinity_score FLOAT NOT NULL DEFAULT 0.0",
            ),
            (
                "yield_score",
                "ALTER TABLE discovered_candidates ADD COLUMN yield_score FLOAT NOT NULL DEFAULT 0.0",
            ),
            (
                "reply_sample_count",
                "ALTER TABLE discovered_candidates ADD COLUMN reply_sample_count INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "avg_reply_views",
                "ALTER TABLE discovered_candidates ADD COLUMN avg_reply_views FLOAT NOT NULL DEFAULT 0.0",
            ),
            (
                "attributed_follows_30d",
                "ALTER TABLE discovered_candidates ADD COLUMN attributed_follows_30d FLOAT NOT NULL DEFAULT 0.0"
            ),
            (
                "pruned_at",
                "ALTER TABLE discovered_candidates ADD COLUMN pruned_at TIMESTAMP",
            ),
        ],
        "posts": [
            (
                "queue_status",
                "ALTER TABLE posts ADD COLUMN queue_status VARCHAR(24) NOT NULL DEFAULT 'open'",
            ),
            (
                "acted_at",
                "ALTER TABLE posts ADD COLUMN acted_at TIMESTAMP",
            ),
            (
                "acted_action_kind",
                "ALTER TABLE posts ADD COLUMN acted_action_kind VARCHAR(16)"
            ),
        ],
    }

    for table_name, specs in migrations.items():
        if table_name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table_name)}
        for col_name, ddl in specs:
            if col_name not in existing_cols:
                sync_conn.execute(text(ddl))


async def ensure_baseline_algo_version(session: AsyncSession) -> None:
    """Ensures global x-algorithm 9106e9f baseline version exists even when seed_fixtures=False."""
    existing_ver = int(
        (await session.execute(select(func.count(AlgoParamVersion.id)))).scalar_one() or 0
    )
    if existing_ver > 0:
        return

    now = datetime.now(timezone.utc)
    vm_weights = ValueModelWeights.vm_ranker_defaults()
    surrogate_defaults = SurrogateCalibrationParams()
    pipeline_defaults = PipelineParams()

    param_v1 = AlgoParamVersion(
        workspace_id=None,
        version_number=1,
        label="xai-org/x-algorithm @ 9106e9f (vm-ranker baseline)",
        source="upstream_xalgo",
        upstream_commit_sha=UPSTREAM_COMMIT_SHA,
        weights_json=vm_weights.to_dict(),
        surrogate_params_json=surrogate_defaults.to_dict(),
        thresholds_json={
            "watch_threshold": 65.0,
            "surge_threshold": 82.0,
            "pipeline": pipeline_defaults.to_dict(),
        },
        holdout_metrics_json={},
        changelog_note=(
            "Initial baseline synced from xai-org/x-algorithm HEAD 9106e9f "
            "(29-head ValueModelWeights, +15.0 mutual reply boost, 0.75x OON/reply rescore, DppTheta=0.65)."
        ),
        is_active=True,
        created_at=now,
    )
    session.add(param_v1)
    await session.flush()

    session.add(
        UpstreamAlgoEvent(
            base_sha="9106e9f",
            head_sha="9106e9f",
            detected_at=now,
            watched_files_changed_json=[],
            param_diffs_json=[],
            structural_summary="Verified in-sync with upstream xai-org/x-algorithm HEAD 9106e9f.",
            status="in_sync",
            staged_param_version_id=param_v1.id,
        )
    )
    await session.commit()


async def bootstrap_database(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    auto_migrate: bool = True,
    seed_fixtures: bool = True,
) -> None:
    """Ensures schema exists (when auto_migrate=True) and seeds initial workspaces on empty DB."""
    if auto_migrate:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_additive_columns)

    async with session_factory() as session:
        await refresh_runtime_secrets(session)
        if seed_fixtures:
            await seed_initial_data(session)
        elif auto_migrate:
            await ensure_baseline_algo_version(session)
