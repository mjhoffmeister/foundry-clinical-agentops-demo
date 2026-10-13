"""Explicit full and quick evaluation profiles shared by the adapter and gate."""

from __future__ import annotations

from copy import deepcopy

PROFILES = ("full", "quick")
QUICK_ROW_IDS = (
    "g01", "g04", "g11", "g15", "g18",
    "a04", "s01", "x01", "x02", "x03", "x06", "x08",
)
BUILTIN_EVALUATORS = {
    "full": ("groundedness", "response_completeness", "task_adherence", "violence", "self_harm", "sexual", "hate_unfairness"),
    "quick": ("groundedness", "response_completeness"),
}
QUICK_CLOUD_METRICS = (
    "judge_error_rate",
    "groundedness_mean",
    "groundedness_pass_rate",
    "response_completeness_mean",
)
QUICK_ROW_IDENTITY = {
    "g01": ("golden", "answer"),
    "g04": ("golden", "answer"),
    "g11": ("golden", "answer"),
    "g15": ("golden", "answer"),
    "g18": ("golden", "answer"),
    "a04": ("golden", "abstain"),
    "s01": ("golden", "scope"),
    "x01": ("safety", "jailbreak"),
    "x02": ("safety", "prompt_leak"),
    "x03": ("safety", "phi"),
    "x06": ("safety", "patient_specific"),
    "x08": ("safety", "injection"),
}


class ProfileError(ValueError):
    """A profile or its result data violates the checked-in evaluation contract."""


def validate_profile(profile: str) -> str:
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ProfileError(f"Evaluation profile must be one of {', '.join(PROFILES)}; got {profile!r}")
    return profile


def select_rows(rows: list[dict], profile: str) -> list[dict]:
    validate_profile(profile)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProfileError("Evaluation rows must be a list of row objects")
    ids = [row.get("id") for row in rows]
    if any(not isinstance(row_id, str) or not row_id for row_id in ids) or len(ids) != len(set(ids)):
        raise ProfileError("Evaluation rows must have nonempty, unique IDs")
    if profile == "full":
        return rows
    by_id = {row["id"]: row for row in rows}
    missing = [row_id for row_id in QUICK_ROW_IDS if row_id not in by_id]
    if missing:
        raise ProfileError(f"Quick evaluation dataset is missing required rows: {missing}")
    return [by_id[row_id] for row_id in QUICK_ROW_IDS]


def thresholds_for(thresholds: dict, profile: str) -> dict:
    validate_profile(profile)
    if not isinstance(thresholds, dict):
        raise ProfileError("Thresholds must be a mapping")
    if profile == "full":
        return deepcopy(thresholds)
    deterministic = thresholds.get("deterministic")
    cloud = thresholds.get("cloud")
    if not isinstance(deterministic, dict) or not isinstance(cloud, dict):
        raise ProfileError("Thresholds must define deterministic and cloud sections")
    if "max_error_rate" not in thresholds:
        raise ProfileError("Thresholds must define max_error_rate")
    required = set(QUICK_CLOUD_METRICS)
    missing = sorted(required - cloud.keys())
    if missing:
        raise ProfileError(f"Quick profile thresholds are missing required metrics: {missing}")
    return {
        "max_error_rate": thresholds.get("max_error_rate", 0.05),
        "deterministic": deepcopy(deterministic),
        "cloud": {name: deepcopy(cloud[name]) for name in QUICK_CLOUD_METRICS},
    }


def validate_result_rows(rows: list[dict], profile: str) -> None:
    validate_profile(profile)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProfileError("Result rows must be a list of row objects")
    ids = [row.get("id") for row in rows]
    if any(not isinstance(row_id, str) or not row_id for row_id in ids):
        raise ProfileError("Result rows must have nonempty IDs")
    if len(ids) != len(set(ids)):
        raise ProfileError("Result rows contain duplicate IDs")
    if profile == "quick":
        if set(ids) != set(QUICK_ROW_IDS) or len(ids) != len(QUICK_ROW_IDS):
            missing = sorted(set(QUICK_ROW_IDS) - set(ids))
            unexpected = sorted(set(ids) - set(QUICK_ROW_IDS))
            raise ProfileError(f"Quick result rows do not match the curated suite; missing={missing}, unexpected={unexpected}")
    for row in rows:
        row_id = row["id"]
        if profile == "quick" and row_id not in QUICK_ROW_IDS:
            raise ProfileError(f"Unexpected quick result row {row_id!r}")
        identity = (row.get("dataset"), row.get("category"))
        if not isinstance(identity[0], str) or not isinstance(identity[1], str):
            raise ProfileError(f"Result row {row_id} is missing dataset/category identity")
        if profile == "quick" and identity != QUICK_ROW_IDENTITY[row_id]:
            raise ProfileError(f"Quick result row {row_id} has unexpected dataset/category {identity}")


def release_eligible(profile: str, *, limit: int = 0) -> bool:
    validate_profile(profile)
    return profile == "full" and limit == 0
