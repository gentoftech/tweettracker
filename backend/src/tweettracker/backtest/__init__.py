"""Epic 3 48h Outcome Grader, Regularized Auto-Tuner & Vectorized Backtester exports."""

from tweettracker.backtest.grader import (
    compute_workspace_scorecard,
    grade_workspace_outcomes,
)
from tweettracker.backtest.replay import (
    FusionCalibrationParams,
    ResolvedAlgoConfig,
    TrajectoryMatrix,
    TrajectoryMeta,
    VectorizedScoreOutput,
    apply_candidate_overrides,
    compute_calibration_metrics_vec,
    compute_param_diffs,
    config_from_version,
    get_active_algo_version,
    pack_workspace_trajectories,
    promote_candidate_params,
    run_backtest_replay,
    synthesize_benchmark_trajectories,
    vectorized_score_trajectories,
)
from tweettracker.backtest.tuner import (
    TUNABLE_SPECS,
    clamp_with_drift_cap,
    list_workspace_changelog,
    rollback_to_version,
    run_auto_calibration,
    serialize_param_version,
)

__all__ = [
    "FusionCalibrationParams",
    "ResolvedAlgoConfig",
    "TUNABLE_SPECS",
    "TrajectoryMatrix",
    "TrajectoryMeta",
    "VectorizedScoreOutput",
    "apply_candidate_overrides",
    "clamp_with_drift_cap",
    "compute_calibration_metrics_vec",
    "compute_param_diffs",
    "compute_workspace_scorecard",
    "config_from_version",
    "get_active_algo_version",
    "grade_workspace_outcomes",
    "list_workspace_changelog",
    "pack_workspace_trajectories",
    "promote_candidate_params",
    "rollback_to_version",
    "run_auto_calibration",
    "run_backtest_replay",
    "serialize_param_version",
    "synthesize_benchmark_trajectories",
    "vectorized_score_trajectories",
]
