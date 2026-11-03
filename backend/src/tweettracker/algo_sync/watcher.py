"""Automated Upstream xai-org/x-algorithm Sync Watcher, Rust param!() Parser & GitHub Issue Creator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import subprocess
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import (
    FusionCalibrationParams,
    PipelineParams,
    SurrogateCalibrationParams,
    UPSTREAM_COMMIT_SHA,
    ValueModelWeights,
)
from tweettracker.crypto import resolve_secret_with_source
from tweettracker.models import (
    AlertDelivery,
    AlgoParamVersion,
    UpstreamAlgoEvent,
    Workspace,
)
from tweettracker.providers.llm import get_llm_provider
from tweettracker.radar.sse import radar_sse_hub

UPSTREAM_XALGO_REPO = "xai-org/x-algorithm"
DEFAULT_TARGET_GITHUB_REPO = "gentoftech/tweettracker"
GITHUB_API_BASE = "https://api.github.com"
ALGO_SYNC_LABEL = "algo-sync"

GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")

WATCHED_RANKING_PATHS: tuple[str, ...] = (
    "home-mixer/params/param.rs",
    "home-mixer/params/",
    "vm-ranker/src/params.rs",
    "vm-ranker/params.rs",
    "xai-value-model/",
    "home-mixer/scorers/",
    "home-mixer/filters/",
    "vm-ranker/src/scoring/",
    "vm-ranker/scoring/",
    "src/scala/com/twitter/interaction_graph/scio/agg_all/user_cred_v2/",
    "user-cred-v2/",
    "phoenix/",
    "thunder/",
    "simclusters/",
    "docs/",
)

IGNORED_PATH_PREFIXES: tuple[str, ...] = (
    "ads/",
    ".github/",
    "ci/",
    "infra/",
    "scripts/",
    "bazel/",
)

IGNORED_FILENAMES: frozenset[str] = frozenset(
    {
        "readme.md",
        "license",
        "license.md",
        "contributing.md",
        "cargo.lock",
        ".gitignore",
        "rustfmt.toml",
    }
)

# Maps upstream Rust param!() identifiers & constants -> (target_group, python_field_name)
RUST_IDENT_TO_FIELD: dict[str, tuple[str, str]] = {
    # ValueModelWeights (weights.py)
    "FavoriteWeight": ("weights", "favorite"),
    "ReplyWeight": ("weights", "reply"),
    "RetweetWeight": ("weights", "retweet"),
    "PhotoExpandWeight": ("weights", "photo_expand"),
    "VideoOpenWeight": ("weights", "video_open"),
    "ClickWeight": ("weights", "click"),
    "OpenLinkWeight": ("weights", "open_link"),
    "ProfileClickWeight": ("weights", "profile_click"),
    "VqvWeight": ("weights", "vqv"),
    "VideoQualityViewWeight": ("weights", "vqv"),
    "ShareWeight": ("weights", "share"),
    "ShareViaDmWeight": ("weights", "share_via_dm"),
    "ShareViaCopyLinkWeight": ("weights", "share_via_copy_link"),
    "DwellWeight": ("weights", "dwell"),
    "QuoteWeight": ("weights", "quote"),
    "QuotedClickWeight": ("weights", "quoted_click"),
    "QuotedVqvWeight": ("weights", "quoted_vqv"),
    "FollowAuthorWeight": ("weights", "follow_author"),
    "PostUnexploredWeight": ("weights", "post_unexplored"),
    "PostUnexploredIncludeOutOfNetwork": ("weights", "post_unexplored_include_out_of_network"),
    "PostVideoWeight": ("weights", "post_video"),
    "NotInterestedWeight": ("weights", "not_interested"),
    "BlockAuthorWeight": ("weights", "block_author"),
    "BlockWeight": ("weights", "block_author"),
    "MuteAuthorWeight": ("weights", "mute_author"),
    "MuteWeight": ("weights", "mute_author"),
    "ReportWeight": ("weights", "report"),
    "NotDwelledWeight": ("weights", "not_dwelled"),
    "NotDwelled": ("weights", "not_dwelled"),
    "ContDwellTimeWeight": ("weights", "cont_dwell_time"),
    "ContinuousDwellTimeWeight": ("weights", "cont_dwell_time"),
    "ContClickDwellTimeWeight": ("weights", "cont_click_dwell_time"),
    "VideoContinuationWeight": ("weights", "video_continuation"),
    "UserVideoContinuationWeight": ("weights", "user_video_continuation"),
    "ProfileVisitSecsWeight": ("weights", "profile_visit_secs"),
    "MinVideoDurationMs": ("weights", "min_video_duration_ms"),
    "EnableQuotedVqvDurationCheck": ("weights", "enable_quoted_vqv_duration_check"),
    "BidirectionalFollowReplyWeightBoost": ("weights", "bidirectional_follow_reply_weight_boost"),
    "BidirectionalFollowDwellWeightBoost": ("weights", "bidirectional_follow_dwell_weight_boost"),
    "EnableAuthorDiversity": ("weights", "enable_author_diversity"),
    "AuthorDiversityDecayFactor": ("weights", "author_diversity_decay"),
    "AuthorDiversityDecay": ("weights", "author_diversity_decay"),
    "AuthorDiversityFloor": ("weights", "author_diversity_floor"),
    "EnableOonRescoreForInNetworkRepliesRetweets": (
        "weights",
        "oon_rescore_in_network_replies_retweets",
    ),
    "OonRescoreInNetworkRepliesRetweets": (
        "weights",
        "oon_rescore_in_network_replies_retweets",
    ),
    "MultiplierPreOffset": ("weights", "multiplier_pre_offset"),
    # PipelineParams (weights.py)
    "OonWeightFactor": ("pipeline", "oon_weight_factor"),
    "TopicOonWeightFactor": ("pipeline", "topic_oon_weight_factor"),
    "NewUserOonWeightFactor": ("pipeline", "new_user_oon_weight_factor"),
    "DppEnabled": ("pipeline", "dpp_enabled"),
    "TopicDppEnabled": ("pipeline", "topic_dpp_enabled"),
    "DppTheta": ("pipeline", "dpp_theta"),
    "DPP_THETA": ("pipeline", "dpp_theta"),
    "DEFAULT_DPP_THETA": ("pipeline", "dpp_theta"),
    "DppMaxSelectedRank": ("pipeline", "dpp_max_selected_rank"),
    "SimclustersMaxCandidateAgeHours": ("pipeline", "simclusters_max_candidate_age_hours"),
    "EnablePhoenixOonReplies": ("pipeline", "enable_phoenix_oon_replies"),
    "PhoenixOonRepliesRootOnly": ("pipeline", "phoenix_oon_replies_root_only"),
    "ColdStartFollowerCap": ("pipeline", "cold_start_follower_cap"),
    "COLD_START_FOLLOWER_CAP": ("pipeline", "cold_start_follower_cap"),
    "ColdStartMaxPostAgeSecs": ("pipeline", "cold_start_max_post_age_secs"),
    "ColdStartImpressionThreshold": ("pipeline", "cold_start_impression_threshold"),
    "ColdStartSlotMin": ("pipeline", "cold_start_slot_min"),
    "ColdStartSlotMax": ("pipeline", "cold_start_slot_max"),
    "LowImpressionsMaxPositionRatio": ("pipeline", "low_impressions_max_position_ratio"),
    "EnableViewerColdStart": ("pipeline", "enable_viewer_cold_start"),
    "EnableColdStartThompsonSampling": ("pipeline", "enable_cold_start_thompson_sampling"),
    "ColdStartBetaAlpha0": ("pipeline", "cold_start_beta_alpha0"),
    "ColdStartBetaBeta0": ("pipeline", "cold_start_beta_beta0"),
    "ColdStartTsTopK": ("pipeline", "cold_start_ts_top_k"),
    "ColdStartImpressionScale": ("pipeline", "cold_start_impression_scale"),
    # Constants (scoring.rs / UserCredV2.scala)
    "SCORE_INTERCEPT": ("user_cred_v2", "score_intercept"),
    "ScoreIntercept": ("user_cred_v2", "score_intercept"),
    "SCORE_SLOPE": ("user_cred_v2", "score_slope"),
    "ScoreSlope": ("user_cred_v2", "score_slope"),
    "NEGATIVE_SCORES_OFFSET": ("scoring", "negative_scores_offset"),
}

KNOWN_STRUCT_FIELD_MAP: dict[str, tuple[str, str]] = {
    **{k: ("weights", k) for k in ValueModelWeights.__dataclass_fields__},
    **{k: ("pipeline", k) for k in PipelineParams.__dataclass_fields__},
}

_PARAM_MACRO_RE = re.compile(
    r"\b(?:define_)?param!\s*\(\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*,\s*"
    r"[A-Za-z0-9_:<>]+\s*,\s*"
    r'(?:"[^"]*"\s*,\s*)?'
    r"(-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?|true|false)"
    r"\s*,?\s*\)",
    re.MULTILINE,
)

_CONST_ASSIGN_RE = re.compile(
    r"\b(?:pub\s+)?(?:const|static|val)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?::\s*[A-Za-z0-9_]+)?\s*=\s*"
    r"(-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?|true|false)\b"
)

_STRUCT_FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9_])([a-z][a-z0-9_]*)\s*:\s*"
    r"(-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?|true|false)\s*(?:,|\}|$)",
    re.MULTILINE,
)

_SYMBOL_RE = re.compile(
    r"\b(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|def|object|class)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

SYNTHETIC_DRIFT_BASE_SHA = "9106e9f"
SYNTHETIC_DRIFT_HEAD_SHA = "94a21c8e51b04d39f8120c77a6e5b1904f22d810"
SYNTHETIC_DRIFT_FILES: list[dict[str, Any]] = [
    {
        "filename": "vm-ranker/src/params.rs",
        "status": "modified",
        "additions": 3,
        "deletions": 3,
        "patch": (
            "@@ -18,9 +18,9 @@\n"
            '-param!(FavoriteWeight, f64, "favorite_weight", 0.5);\n'
            '+param!(FavoriteWeight, f64, "favorite_weight", 0.65);\n'
            ' param!(ReplyWeight, f64, "reply_weight", 5.0);\n'
            '-param!(BidirectionalFollowReplyWeightBoost, f64, "bidirectional_follow_reply_weight_boost", 15.0);\n'
            '+param!(BidirectionalFollowReplyWeightBoost, f64, "bidirectional_follow_reply_weight_boost", 18.0);\n'
            '-param!(OonWeightFactor, f64, "oon_weight_factor", 0.75);\n'
            '+param!(OonWeightFactor, f64, "oon_weight_factor", 0.72);\n'
        ),
    },
    {
        "filename": "vm-ranker/src/scoring/dpp_model.rs",
        "status": "modified",
        "additions": 4,
        "deletions": 2,
        "patch": (
            "@@ -42,7 +42,9 @@ pub fn greedy_dpp_select(candidates: &[DppItem]) -> Vec<f64> {\n"
            '-param!(DppTheta, f64, "dpp_theta", 0.65);\n'
            '+param!(DppTheta, f64, "dpp_theta", 0.70);\n'
            " // Tighten quality-vs-diversity trade-off for top-150 reranking window\n"
            "+    let quality_exponent = theta / (1.0 - theta);\n"
        ),
    },
]


@dataclass(slots=True)
class SyncCheckOutcome:
    event: UpstreamAlgoEvent
    staged_version: AlgoParamVersion | None
    deduplicated: bool = False


def validate_git_ref(ref: str, *, field_name: str = "target_ref") -> str:
    """Validates a Git SHA or branch/tag ref before interpolating into GitHub API paths."""
    cleaned = str(ref or "").strip()
    if (
        not cleaned
        or not GIT_REF_RE.fullmatch(cleaned)
        or ".." in cleaned
        or "//" in cleaned
        or cleaned.startswith("/")
        or cleaned.endswith("/")
        or cleaned.startswith("-")
    ):
        raise ValueError(
            f"Invalid {field_name}: must match ^[A-Za-z0-9._/-]{{1,120}}$ without '..' or path traversal."
        )
    return cleaned


def is_watched_ranking_path(path: str) -> bool:
    """Filters ranking-critical Rust/Scala/docs paths from ads/, root README, and CI churn."""
    normalized = path.strip().replace("\\", "/").lstrip("/")
    if not normalized:
        return False

    lower_path = normalized.lower()
    basename = lower_path.rsplit("/", 1)[-1]
    in_docs_tree = lower_path.startswith("docs/")

    if lower_path in IGNORED_FILENAMES:
        return False
    if basename in IGNORED_FILENAMES and not in_docs_tree:
        return False
    if lower_path.endswith(".md") and not in_docs_tree:
        return False

    for ignored_prefix in IGNORED_PATH_PREFIXES:
        if lower_path.startswith(ignored_prefix):
            return False

    for watched in WATCHED_RANKING_PATHS:
        w_lower = watched.lower()
        if w_lower.endswith("/"):
            if lower_path.startswith(w_lower):
                return True
        elif lower_path == w_lower:
            return True

    return False


def _parse_scalar_literal(raw: str) -> bool | int | float:
    cleaned = raw.strip().replace("_", "")
    if cleaned == "true":
        return True
    if cleaned == "false":
        return False
    if "." in cleaned or "e" in cleaned.lower():
        return float(cleaned)
    return int(cleaned)


def _compute_pct_change(
    old_val: bool | int | float,
    new_val: bool | int | float,
) -> float | None:
    if isinstance(old_val, bool) or isinstance(new_val, bool):
        return None
    old_f = float(old_val)
    new_f = float(new_val)
    if abs(old_f) < 1e-12:
        return None
    return round(((new_f - old_f) / abs(old_f)) * 100.0, 2)


def _split_patch_hunks(patch: str) -> list[tuple[str, str, list[str]]]:
    """Splits a unified diff patch into (before_text, after_text, hunk_headers)."""
    if not patch.strip():
        return []

    hunks: list[tuple[list[str], list[str], list[str]]] = []
    cur_before: list[str] = []
    cur_after: list[str] = []
    cur_headers: list[str] = []

    for raw_line in patch.splitlines():
        if raw_line.startswith("--- ") or raw_line.startswith("+++ "):
            continue
        if raw_line.startswith("@@"):
            if cur_before or cur_after:
                hunks.append((cur_before, cur_after, cur_headers))
                cur_before = []
                cur_after = []
                cur_headers = []
            parts = raw_line.split("@@", 2)
            if len(parts) == 3 and parts[2].strip():
                cur_headers.append(parts[2].strip())
            continue
        if raw_line.startswith("-"):
            cur_before.append(raw_line[1:])
        elif raw_line.startswith("+"):
            cur_after.append(raw_line[1:])
        elif raw_line.startswith("\\"):
            continue
        else:
            ctx_line = raw_line[1:] if raw_line.startswith(" ") else raw_line
            cur_before.append(ctx_line)
            cur_after.append(ctx_line)

    if cur_before or cur_after:
        hunks.append((cur_before, cur_after, cur_headers))

    return [("\n".join(b), "\n".join(a), hdrs) for b, a, hdrs in hunks]


def _extract_assignments(text: str) -> dict[str, tuple[bool | int | float, str, str]]:
    """Extracts {ident: (parsed_value, target_group, mapped_field)} from Rust/Scala text."""
    found: dict[str, tuple[bool | int | float, str, str]] = {}

    for match in _PARAM_MACRO_RE.finditer(text):
        ident, raw_val = match.group(1), match.group(2)
        target, mapped = RUST_IDENT_TO_FIELD.get(ident, ("unmapped", ident))
        found[ident] = (_parse_scalar_literal(raw_val), target, mapped)

    for match in _CONST_ASSIGN_RE.finditer(text):
        ident, raw_val = match.group(1), match.group(2)
        if ident in RUST_IDENT_TO_FIELD:
            target, mapped = RUST_IDENT_TO_FIELD[ident]
            found[ident] = (_parse_scalar_literal(raw_val), target, mapped)

    for match in _STRUCT_FIELD_RE.finditer(text):
        field_name, raw_val = match.group(1), match.group(2)
        if field_name in KNOWN_STRUCT_FIELD_MAP and field_name not in found:
            target, mapped = KNOWN_STRUCT_FIELD_MAP[field_name]
            found[field_name] = (_parse_scalar_literal(raw_val), target, mapped)

    return found


def parse_patch_param_diffs(file_path: str, patch: str) -> list[dict[str, Any]]:
    """Deterministically extracts changed param!(...), struct weights, and constants from a file patch."""
    norm_file = file_path.strip().replace("\\", "/").lstrip("/")
    diffs: list[dict[str, Any]] = []
    seen_idents: set[str] = set()

    for before_text, after_text, _ in _split_patch_hunks(patch):
        before_map = _extract_assignments(before_text)
        after_map = _extract_assignments(after_text)

        for ident, (new_val, target, mapped_field) in after_map.items():
            if ident not in before_map or ident in seen_idents:
                continue
            old_val, _, _ = before_map[ident]
            if old_val == new_val and type(old_val) is type(new_val):
                continue
            seen_idents.add(ident)
            diffs.append(
                {
                    "rust_ident": ident,
                    "mapped_field": mapped_field,
                    "target": target,
                    "file": norm_file,
                    "old_value": old_val,
                    "new_value": new_val,
                    "pct_change": _compute_pct_change(old_val, new_val),
                }
            )

    return diffs


def parse_compare_files(
    files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Splits GitHub compare files into (watched_files, ignored_files, param_diffs)."""
    watched_files: list[dict[str, Any]] = []
    ignored_files: list[dict[str, Any]] = []
    param_diffs: list[dict[str, Any]] = []

    for f in files:
        filename = str(f.get("filename") or "").strip()
        if not filename:
            continue
        if is_watched_ranking_path(filename):
            watched_files.append(f)
            patch = str(f.get("patch") or "")
            if patch:
                param_diffs.extend(parse_patch_param_diffs(filename, patch))
        else:
            ignored_files.append(f)

    return watched_files, ignored_files, param_diffs


def map_affected_local_files(watched_filenames: list[str]) -> list[str]:
    """Maps changed upstream x-algorithm files to local TweetTracker tracer modules."""
    affected: list[str] = []
    for path in watched_filenames:
        lp = path.lower()
        if "params" in lp or "param.rs" in lp or "weights.rs" in lp:
            affected.append("backend/src/tweettracker/algo/weights.py")
        if "scoring.rs" in lp or "inputs.rs" in lp or "phoenix_scores.rs" in lp or "value_model" in lp:
            affected.append("backend/src/tweettracker/algo/scoring.py")
        if "dpp" in lp:
            affected.append("backend/src/tweettracker/algo/dpp.py")
        if "filter" in lp or "cold_start" in lp:
            affected.append("backend/src/tweettracker/algo/filters.py")
        if "user_cred" in lp or "user-cred" in lp:
            affected.append("backend/src/tweettracker/algo/user_cred.py")
    # Preserve order while deduplicating
    return list(dict.fromkeys(affected))


def _has_structural_logic_changes(watched_files: list[dict[str, Any]]) -> bool:
    for f in watched_files:
        fname = str(f.get("filename") or "").lower()
        if not (fname.endswith("param.rs") or fname.endswith("params.rs")):
            return True
    return False


def build_deterministic_diff_summary(
    watched_files: list[dict[str, Any]],
    param_diffs: list[dict[str, Any]],
) -> str:
    """Produces a crisp deterministic impact summary when no LLM key is configured."""
    file_summaries: list[str] = []
    touched_symbols: list[str] = []

    for f in watched_files:
        fname = str(f.get("filename") or "")
        adds = int(f.get("additions") or 0)
        dels = int(f.get("deletions") or 0)
        file_summaries.append(f"{fname} (+{adds}/-{dels})")
        patch = str(f.get("patch") or "")
        for _, _, headers in _split_patch_hunks(patch):
            for hdr in headers:
                m = _SYMBOL_RE.search(hdr)
                if m:
                    touched_symbols.append(m.group(1))
        for m in _SYMBOL_RE.finditer(patch):
            touched_symbols.append(m.group(1))

    unique_symbols = list(dict.fromkeys(touched_symbols))[:8]
    parts: list[str] = [
        f"Detected {len(watched_files)} ranking file change(s): {', '.join(file_summaries)}."
    ]
    if param_diffs:
        formatted_params = []
        for d in param_diffs[:8]:
            pct = f" ({d['pct_change']:+.1f}%)" if d.get("pct_change") is not None else ""
            formatted_params.append(
                f"{d['rust_ident']} ({d['mapped_field']}): {d['old_value']} -> {d['new_value']}{pct}"
            )
        parts.append("Parsed parameter shifts: " + "; ".join(formatted_params) + ".")
    if unique_symbols:
        parts.append(f"Touched symbols: {', '.join(unique_symbols)}.")

    affected = map_affected_local_files([str(f.get("filename") or "") for f in watched_files])
    if affected:
        parts.append(f"Local tracer modules to review: {', '.join(affected)}.")

    return " ".join(parts)


async def summarize_structural_diff(
    watched_files: list[dict[str, Any]],
    param_diffs: list[dict[str, Any]],
    *,
    llm_provider_name: str = "google_gemini",
    session: AsyncSession | None = None,
    workspace: Workspace | None = None,
) -> str:
    """Summarizes structural Rust/Scala diffs via LLM router, falling back deterministically."""
    deterministic = build_deterministic_diff_summary(watched_files, param_diffs)
    if not _has_structural_logic_changes(watched_files):
        return deterministic

    # Try requested provider, or fallback to whichever Build 1 LLM key is present in env
    candidates_order = [llm_provider_name.strip().lower()]
    for fallback_p in ("google_gemini", "xai_grok"):
        if fallback_p not in candidates_order:
            candidates_order.append(fallback_p)

    patch_excerpt = "\n\n".join(
        f"### {f.get('filename')}\n{str(f.get('patch') or '')[:1800]}"
        for f in watched_files[:5]
    )
    prompt = (
        "Summarize the ranking impact of this upstream xai-org/x-algorithm diff for "
        "TweetTracker's Python value-model/vm-ranker tracer in 2-4 concise sentences.\n\n"
        f"Deterministic parse: {deterministic}\n\n"
        f"Patches:\n{patch_excerpt}"
    )

    for prov_name in candidates_order:
        try:
            provider = get_llm_provider(prov_name)
            resp = await provider.complete(
                prompt,
                system_prompt="You are a concise Rust/Scala ranking systems engineer.",
                temperature=0.1,
                max_tokens=320,
                session=session,
                workspace=workspace,
            )
            if resp.text:
                return resp.text
        except Exception:
            continue

    return deterministic


def resolve_github_token(*, allow_gh_cli_fallback: bool = False) -> tuple[str, str]:
    """Resolves GitHub token from in-app secrets or GITHUB_TOKEN env (with optional non-test gh CLI fallback)."""
    tok, src = resolve_secret_with_source("GITHUB_TOKEN")
    if tok:
        return tok, ("app:GITHUB_TOKEN" if src == "app" else "env:GITHUB_TOKEN")

    # NEVER execute `gh` CLI subprocess inside pytest!
    if allow_gh_cli_fallback and "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            proc = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip(), "cli:gh_auth_token"
        except Exception:
            pass

    return "", "none"


async def resolve_github_token_async(*, allow_gh_cli_fallback: bool = False) -> tuple[str, str]:
    """Non-blocking async wrapper for resolve_github_token."""
    if not allow_gh_cli_fallback or "PYTEST_CURRENT_TEST" in os.environ:
        return resolve_github_token(allow_gh_cli_fallback=False)
    tok, src = resolve_secret_with_source("GITHUB_TOKEN")
    if tok:
        return tok, ("app:GITHUB_TOKEN" if src == "app" else "env:GITHUB_TOKEN")
    return await asyncio.to_thread(resolve_github_token, allow_gh_cli_fallback=True)


def resolve_target_github_repo() -> str:
    return (
        os.environ.get("TWEETTRACKER_GITHUB_REPO", DEFAULT_TARGET_GITHUB_REPO).strip()
        or DEFAULT_TARGET_GITHUB_REPO
    )


def format_github_issue_markdown(
    *,
    base_sha: str,
    head_sha: str,
    watched_files: list[dict[str, Any]],
    param_diffs: list[dict[str, Any]],
    structural_summary: str,
    staged_version_id: int | None,
) -> tuple[str, str]:
    """Builds the structured title and Markdown body for gentoftech/tweettracker issues."""
    short_base = base_sha[:7]
    short_head = head_sha[:7]
    title = f"[algo-sync] Upstream xai-org/x-algorithm drift: {short_base} -> {short_head}"
    compare_url = (
        f"https://github.com/{UPSTREAM_XALGO_REPO}/compare/{base_sha}...{head_sha}"
    )

    lines: list[str] = [
        "## Automated Upstream `xai-org/x-algorithm` Drift Report",
        "",
        f"- **Base Synced SHA:** `{short_base}` (`{base_sha}`)",
        f"- **Upstream Head SHA:** `{short_head}` (`{head_sha}`)",
        f"- **Compare Diff:** {compare_url}",
    ]
    if staged_version_id is not None:
        lines.append(
            f"- **Staged Backtester Version:** `AlgoParamVersion #{staged_version_id}` "
            "(`source='upstream_algo_sync'`, ready for 1-click replay/activation)"
        )

    lines.extend(["", "### Changed Watched Ranking Files"])
    for f in watched_files:
        lines.append(
            f"- `{f.get('filename')}` (`+{int(f.get('additions') or 0)}` / `-{int(f.get('deletions') or 0)}`)"
        )

    if param_diffs:
        lines.extend(
            [
                "",
                "### Parsed `param!(...)` & Weight Shifts",
                "",
                "| Upstream Identifier | Mapped Field | File | Before | After | Δ% |",
                "| :--- | :--- | :--- | :---: | :---: | :---: |",
            ]
        )
        for d in param_diffs:
            pct_str = f"{d['pct_change']:+.2f}%" if d.get("pct_change") is not None else "—"
            lines.append(
                f"| `{d['rust_ident']}` | `{d['mapped_field']}` | `{d['file']}` "
                f"| `{d['old_value']}` | `{d['new_value']}` | {pct_str} |"
            )

    lines.extend(
        [
            "",
            "### Structural Impact Summary",
            "",
            structural_summary,
        ]
    )

    affected = map_affected_local_files([str(f.get("filename") or "") for f in watched_files])
    if affected:
        lines.extend(["", "### Local TweetTracker Tracer Checklist"])
        for local_path in affected:
            lines.append(f"- [ ] Verify parity in `{local_path}`")

    return title, "\n".join(lines)


async def ensure_deduplicated_github_issue(
    *,
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    watched_files: list[dict[str, Any]],
    param_diffs: list[dict[str, Any]],
    structural_summary: str,
    staged_version_id: int | None,
) -> tuple[int | None, str | None, bool]:
    """Ensures `algo-sync` label exists and opens a deduplicated GitHub issue if none exists for head_sha."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    short_head = head_sha[:7]

    # 1. Query open/recent algo-sync issues to prevent duplicates for head_sha
    issues_url = f"{GITHUB_API_BASE}/repos/{repo}/issues"
    existing_resp = await client.get(
        issues_url,
        params={"state": "all", "labels": ALGO_SYNC_LABEL, "per_page": 50},
        headers=headers,
    )
    if existing_resp.status_code == 200:
        for item in existing_resp.json() or []:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            issue_title = str(item.get("title") or "")
            issue_body = str(item.get("body") or "")
            if short_head in issue_title or head_sha in issue_body:
                return (
                    int(item["number"]) if item.get("number") is not None else None,
                    str(item.get("html_url") or "") or None,
                    True,
                )

    # 2. Ensure `algo-sync` label exists (create if 404)
    label_url = f"{GITHUB_API_BASE}/repos/{repo}/labels/{ALGO_SYNC_LABEL}"
    lbl_resp = await client.get(label_url, headers=headers)
    if lbl_resp.status_code == 404:
        await client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/labels",
            headers=headers,
            json={
                "name": ALGO_SYNC_LABEL,
                "color": "0ea5e9",
                "description": "Automated upstream xai-org/x-algorithm drift alert",
            },
        )

    # 3. Create new issue
    title, body = format_github_issue_markdown(
        base_sha=base_sha,
        head_sha=head_sha,
        watched_files=watched_files,
        param_diffs=param_diffs,
        structural_summary=structural_summary,
        staged_version_id=staged_version_id,
    )
    create_resp = await client.post(
        issues_url,
        headers=headers,
        json={
            "title": title,
            "body": body,
            "labels": [ALGO_SYNC_LABEL],
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    return (
        int(created["number"]) if created.get("number") is not None else None,
        str(created.get("html_url") or "") or None,
        False,
    )


async def _stage_candidate_param_version(
    session: AsyncSession,
    *,
    head_sha: str,
    param_diffs: list[dict[str, Any]],
) -> AlgoParamVersion | None:
    """Stages an inactive AlgoParamVersion when mapped ValueModelWeights or PipelineParams change."""
    actionable = [d for d in param_diffs if d.get("target") in ("weights", "pipeline")]
    if not actionable:
        return None

    existing_staged = (
        await session.execute(
            select(AlgoParamVersion)
            .where(
                AlgoParamVersion.source == "upstream_algo_sync",
                AlgoParamVersion.upstream_commit_sha == head_sha[:40],
            )
            .order_by(AlgoParamVersion.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_staged is not None:
        return existing_staged

    active_base = (
        await session.execute(
            select(AlgoParamVersion)
            .where(
                AlgoParamVersion.workspace_id.is_(None),
                AlgoParamVersion.is_active.is_(True),
            )
            .order_by(AlgoParamVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    max_ver = int(
        (await session.execute(select(func.max(AlgoParamVersion.version_number)))).scalar_one()
        or 1
    )

    weights_dict = (
        dict(active_base.weights_json)
        if active_base and active_base.weights_json
        else ValueModelWeights.vm_ranker_defaults().to_dict()
    )
    surrogate_dict = (
        dict(active_base.surrogate_params_json)
        if active_base and active_base.surrogate_params_json
        else SurrogateCalibrationParams().to_dict()
    )
    thresholds_dict = (
        dict(active_base.thresholds_json)
        if active_base and active_base.thresholds_json
        else {"watch_threshold": 65.0, "surge_threshold": 82.0}
    )
    pipeline_dict = dict(thresholds_dict.get("pipeline") or PipelineParams().to_dict())

    # Apply home-mixer first, then vm-ranker so final reranker weights take precedence
    ordered_diffs = sorted(
        actionable,
        key=lambda d: (1 if "vm-ranker" in str(d.get("file", "")).lower() else 0),
    )
    note_items: list[str] = []
    for d in ordered_diffs:
        field = str(d["mapped_field"])
        if d["target"] == "weights" and field in weights_dict:
            weights_dict[field] = d["new_value"]
        elif d["target"] == "pipeline" and field in pipeline_dict:
            pipeline_dict[field] = d["new_value"]
        note_items.append(f"{d['rust_ident']}: {d['old_value']} -> {d['new_value']}")

    # Validate staged parameter dicts before persisting
    weights_dict = ValueModelWeights.from_dict(weights_dict).to_dict()
    pipeline_dict = PipelineParams.from_dict(pipeline_dict).to_dict()
    surrogate_dict = SurrogateCalibrationParams.from_dict(surrogate_dict).to_dict()
    thresholds_dict["pipeline"] = pipeline_dict

    staged = AlgoParamVersion(
        workspace_id=None,
        version_number=max_ver + 1,
        label=f"Staged Upstream Sync @ {head_sha[:7]}",
        source="upstream_algo_sync",
        upstream_commit_sha=head_sha[:40],
        weights_json=weights_dict,
        surrogate_params_json=surrogate_dict,
        thresholds_json=thresholds_dict,
        holdout_metrics_json={},
        changelog_note=(
            f"Auto-staged from xai-org/x-algorithm commit {head_sha[:7]}: "
            + "; ".join(note_items[:8])
        ),
        is_active=False,
    )
    session.add(staged)
    await session.flush()
    return staged


async def get_last_synced_sha(session: AsyncSession) -> str:
    """Returns active global AlgoParamVersion.upstream_commit_sha (fallback 9106e9f)."""
    active = (
        await session.execute(
            select(AlgoParamVersion)
            .where(
                AlgoParamVersion.workspace_id.is_(None),
                AlgoParamVersion.is_active.is_(True),
            )
            .order_by(AlgoParamVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active and active.upstream_commit_sha:
        return active.upstream_commit_sha
    return UPSTREAM_COMMIT_SHA


async def check_upstream_algo_sync(
    session: AsyncSession,
    *,
    base_sha: str | None = None,
    target_ref: str = "main",
    simulate_drift: bool = False,
    auto_create_issue: bool = True,
    create_issue_on_simulate: bool = False,
    allow_gh_cli_fallback: bool = False,
    llm_provider_name: str = "google_gemini",
    workspace: Workspace | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> SyncCheckOutcome:
    """Compares xai-org/x-algorithm against last_synced_sha, stages weights, and opens deduplicated GitHub issue."""
    clean_target_ref = validate_git_ref(target_ref, field_name="target_ref")
    raw_base = base_sha or await get_last_synced_sha(session)
    resolved_base = validate_git_ref(raw_base, field_name="base_sha")
    token, _token_source = await resolve_github_token_async(
        allow_gh_cli_fallback=allow_gh_cli_fallback
    )
    target_repo = resolve_target_github_repo()

    async def _run_with_client(client: httpx.AsyncClient) -> SyncCheckOutcome:
        if simulate_drift:
            head_sha = SYNTHETIC_DRIFT_HEAD_SHA
            raw_files = SYNTHETIC_DRIFT_FILES
            eff_base = SYNTHETIC_DRIFT_BASE_SHA
        else:
            eff_base = resolved_base
            gh_headers: dict[str, str] = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if token:
                gh_headers["Authorization"] = f"Bearer {token}"
            compare_url = (
                f"{GITHUB_API_BASE}/repos/{UPSTREAM_XALGO_REPO}/compare/{eff_base}...{clean_target_ref}"
            )
            resp = await client.get(compare_url, headers=gh_headers)
            resp.raise_for_status()
            payload = resp.json()

            commits = payload.get("commits") or []
            head_sha = str(
                payload.get("head_sha")
                or (commits[-1].get("sha") if commits else "")
                or payload.get("merge_base_commit", {}).get("sha")
                or eff_base
            ).strip()
            raw_files = list(payload.get("files") or [])

        # Fast-path DB deduplication if this exact head_sha was already processed
        existing_event = (
            await session.execute(
                select(UpstreamAlgoEvent)
                .where(UpstreamAlgoEvent.head_sha == head_sha[:40])
                .order_by(UpstreamAlgoEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        should_try_issue = bool(
            token
            and auto_create_issue
            and ((not simulate_drift) or create_issue_on_simulate)
        )

        if existing_event is not None:
            existing_staged = (
                await session.get(AlgoParamVersion, existing_event.staged_param_version_id)
                if existing_event.staged_param_version_id
                else None
            )
            # If already in_sync, ignored_non_ranking, or already linked to a GitHub issue (or no issue needed), return deduplicated!
            if (
                existing_event.status in ("in_sync", "ignored_non_ranking")
                or existing_event.github_issue_url is not None
                or not should_try_issue
            ):
                return SyncCheckOutcome(
                    event=existing_event,
                    staged_version=existing_staged,
                    deduplicated=True,
                )

        # Identical SHA or zero changed files -> in_sync
        if head_sha[:7] == eff_base[:7] or not raw_files:
            event = UpstreamAlgoEvent(
                base_sha=eff_base[:40],
                head_sha=head_sha[:40],
                detected_at=datetime.now(timezone.utc),
                watched_files_changed_json=[],
                param_diffs_json=[],
                structural_summary=(
                    f"Upstream {UPSTREAM_XALGO_REPO} ({clean_target_ref}) is in sync with {eff_base[:7]}."
                ),
                status="in_sync",
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return SyncCheckOutcome(event=event, staged_version=None, deduplicated=False)

        watched_files, ignored_files, param_diffs = parse_compare_files(raw_files)

        # Non-ranking churn only (e.g. ads/, README, CI) -> ignored_non_ranking, zero GitHub issues
        if not watched_files:
            ignored_names = [str(f.get("filename") or "") for f in ignored_files[:6]]
            event = UpstreamAlgoEvent(
                base_sha=eff_base[:40],
                head_sha=head_sha[:40],
                detected_at=datetime.now(timezone.utc),
                watched_files_changed_json=[],
                param_diffs_json=[],
                structural_summary=(
                    f"Ignored {len(ignored_files)} non-ranking file(s) ({', '.join(ignored_names)}); "
                    "zero ranking/scorer/filter paths touched."
                ),
                status="ignored_non_ranking",
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return SyncCheckOutcome(event=event, staged_version=None, deduplicated=False)

        watched_filenames = [str(f.get("filename") or "") for f in watched_files]
        summary = await summarize_structural_diff(
            watched_files,
            param_diffs,
            llm_provider_name=llm_provider_name,
            session=session,
            workspace=workspace,
        )

        staged_ver = await _stage_candidate_param_version(
            session,
            head_sha=head_sha,
            param_diffs=param_diffs,
        )

        issue_num: int | None = None
        issue_url: str | None = None
        dedup_issue = False

        if should_try_issue:
            issue_num, issue_url, dedup_issue = await ensure_deduplicated_github_issue(
                client=client,
                token=token,
                repo=target_repo,
                base_sha=eff_base,
                head_sha=head_sha,
                watched_files=watched_files,
                param_diffs=param_diffs,
                structural_summary=summary,
                staged_version_id=(staged_ver.id if staged_ver else None),
            )

        if existing_event is not None:
            existing_event.github_issue_number = issue_num
            existing_event.github_issue_url = issue_url
            existing_event.staged_param_version_id = (
                staged_ver.id if staged_ver else existing_event.staged_param_version_id
            )
            await session.commit()
            await session.refresh(existing_event)
            return SyncCheckOutcome(
                event=existing_event,
                staged_version=staged_ver,
                deduplicated=True,
            )

        event = UpstreamAlgoEvent(
            base_sha=eff_base[:40],
            head_sha=head_sha[:40],
            detected_at=datetime.now(timezone.utc),
            watched_files_changed_json=watched_filenames,
            param_diffs_json=param_diffs,
            structural_summary=summary,
            github_issue_number=issue_num,
            github_issue_url=issue_url,
            staged_param_version_id=(staged_ver.id if staged_ver else None),
            status="drift_detected",
        )
        session.add(event)

        sse_payload = {
            "base_sha": eff_base[:7],
            "head_sha": head_sha[:7],
            "watched_files_count": len(watched_filenames),
            "param_diffs_count": len(param_diffs),
            "github_issue_url": issue_url,
            "staged_param_version_id": staged_ver.id if staged_ver else None,
        }

        # Emit SSE audit alert row and live SSE push across workspaces
        all_ws_ids = list(
            (await session.execute(select(Workspace.id).order_by(Workspace.id.asc())))
            .scalars()
            .all()
        )
        target_ws_id = workspace.id if workspace is not None else (all_ws_ids[0] if all_ws_ids else None)
        if target_ws_id is not None:
            session.add(
                AlertDelivery(
                    workspace_id=target_ws_id,
                    post_id=None,
                    event_type="algo.upstream_drift_detected",
                    channel="sse",
                    alert_tier="watch",
                    payload_json=sse_payload,
                    status="delivered",
                    attempts=1,
                )
            )

        await session.commit()
        await session.refresh(event)

        notify_ws_ids = [workspace.id] if workspace is not None else all_ws_ids
        for ws_id in notify_ws_ids:
            radar_sse_hub.publish(ws_id, "algo.upstream_drift_detected", sse_payload)

        return SyncCheckOutcome(
            event=event,
            staged_version=staged_ver,
            deduplicated=dedup_issue,
        )

    if http_client is not None:
        return await _run_with_client(http_client)
    async with httpx.AsyncClient(timeout=25.0) as client:
        return await _run_with_client(client)


async def activate_staged_param_version(
    session: AsyncSession,
    event_id: int,
    *,
    workspace_id: int | None = None,
) -> tuple[UpstreamAlgoEvent, AlgoParamVersion]:
    """1-click activates the staged AlgoParamVersion from an UpstreamAlgoEvent."""
    event = await session.get(UpstreamAlgoEvent, event_id)
    if event is None:
        raise LookupError(f"UpstreamAlgoEvent {event_id} not found.")
    if event.staged_param_version_id is None:
        raise ValueError(f"UpstreamAlgoEvent {event_id} has no staged AlgoParamVersion.")

    staged = await session.get(AlgoParamVersion, event.staged_param_version_id)
    if staged is None:
        raise LookupError(f"Staged AlgoParamVersion {event.staged_param_version_id} not found.")

    # Validate staged config integrity before activation
    weights_clean = ValueModelWeights.from_dict(staged.weights_json).to_dict()
    surrogate_clean = SurrogateCalibrationParams.from_dict(staged.surrogate_params_json).to_dict()
    thresholds_clean = dict(staged.thresholds_json or {})
    if "pipeline" in thresholds_clean:
        thresholds_clean["pipeline"] = PipelineParams.from_dict(
            thresholds_clean.get("pipeline")
        ).to_dict()
    if "fusion" in thresholds_clean:
        thresholds_clean["fusion"] = FusionCalibrationParams.from_dict(
            thresholds_clean.get("fusion")
        ).to_dict()

    if workspace_id is not None:
        ws = await session.get(Workspace, workspace_id)
        if ws is None:
            raise LookupError(f"Workspace {workspace_id} not found.")
        prev_ws_versions = list(
            (
                await session.execute(
                    select(AlgoParamVersion).where(
                        AlgoParamVersion.workspace_id == workspace_id,
                        AlgoParamVersion.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for pv in prev_ws_versions:
            pv.is_active = False

        max_ver = int(
            (await session.execute(select(func.max(AlgoParamVersion.version_number)))).scalar_one()
            or 1
        )
        activated = AlgoParamVersion(
            workspace_id=workspace_id,
            version_number=max_ver + 1,
            label=f"{staged.label} (Workspace #{workspace_id} Live)",
            source="upstream_algo_sync",
            upstream_commit_sha=staged.upstream_commit_sha,
            weights_json=weights_clean,
            surrogate_params_json=surrogate_clean,
            thresholds_json=thresholds_clean,
            holdout_metrics_json=dict(staged.holdout_metrics_json or {}),
            changelog_note=staged.changelog_note,
            is_active=True,
        )
        session.add(activated)

        next_watch = float(thresholds_clean.get("watch_threshold", ws.watch_threshold))
        next_surge = float(thresholds_clean.get("surge_threshold", ws.surge_threshold))
        if next_watch < next_surge:
            ws.watch_threshold = next_watch
            ws.surge_threshold = next_surge

        await session.flush()
    else:
        prev_globals = list(
            (
                await session.execute(
                    select(AlgoParamVersion).where(
                        AlgoParamVersion.workspace_id.is_(None),
                        AlgoParamVersion.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for pv in prev_globals:
            pv.is_active = False
        staged.weights_json = weights_clean
        staged.surrogate_params_json = surrogate_clean
        staged.thresholds_json = thresholds_clean
        staged.is_active = True
        activated = staged

    event.status = "activated"
    await session.commit()
    await session.refresh(event)
    await session.refresh(activated)
    return event, activated
