from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from .models import TERMINAL_WORK_STATUSES, WorkItem


def _effective_skills(
    skills: Iterable[str],
    default_skills: Iterable[str],
) -> list[str]:
    return sorted(
        {
            normalized
            for value in [*default_skills, *skills]
            if (normalized := str(value).strip())
        }
    )


def execution_scope_document(
    item: WorkItem,
    *,
    profile: str,
    skills: Iterable[str] = (),
    default_skills: Iterable[str] = (),
    goal_mode: bool = False,
) -> dict[str, Any]:
    """Return the exact, stable scope that an execution approval covers.

    Runtime lifecycle fields and priority scores are deliberately absent. They
    may change as the scheduler works without changing what the operator
    approved. The fields below can change the requested outcome, hierarchy,
    schedule, verifier, or execution environment and therefore invalidate an
    earlier approval.
    """

    verification_contract = item.metadata.get("verification_contract")
    scope_revision = item.authorization_scope_revision
    if (
        isinstance(scope_revision, bool)
        or not isinstance(scope_revision, int)
        or scope_revision < 1
    ):
        scope_revision = 1
    return {
        "schema": "hermes-operator.execution-scope.v1",
        "work_id": item.id,
        "scope_revision": scope_revision,
        "kind": item.kind.value,
        "title": item.title,
        "description": item.description,
        "parent_id": item.parent_id,
        "acceptance_criteria": list(item.acceptance_criteria),
        "due_at": item.due_at,
        "scheduled_at": item.scheduled_at,
        "recurrence_rule": item.recurrence_rule,
        "profile": str(profile).strip(),
        "effective_skills": _effective_skills(skills, default_skills),
        "goal_mode": bool(goal_mode),
        "verification_contract": verification_contract,
    }


def execution_scope_digest(
    item: WorkItem,
    *,
    profile: str,
    skills: Iterable[str] = (),
    default_skills: Iterable[str] = (),
    goal_mode: bool = False,
) -> str:
    document = execution_scope_document(
        item,
        profile=profile,
        skills=skills,
        default_skills=default_skills,
        goal_mode=goal_mode,
    )
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execution_scope_binding(
    item: WorkItem,
    *,
    profile: str,
    skills: Iterable[str] = (),
    default_skills: Iterable[str] = (),
    goal_mode: bool = False,
    execution_authorized: bool | None = None,
) -> dict[str, Any]:
    requested_skills = [
        normalized
        for value in skills
        if (normalized := str(value).strip())
    ]
    configured_skills = [
        normalized
        for value in default_skills
        if (normalized := str(value).strip())
    ]
    binding: dict[str, Any] = {
        "work_id": item.id,
        "work_version": item.version,
        "scope_revision": item.authorization_scope_revision,
        "scope_digest": execution_scope_digest(
            item,
            profile=profile,
            skills=requested_skills,
            default_skills=configured_skills,
            goal_mode=goal_mode,
        ),
        "profile": str(profile).strip(),
        "skills": requested_skills,
        "default_skills": configured_skills,
        "goal_mode": bool(goal_mode),
    }
    if execution_authorized is not None:
        binding["execution_authorized"] = bool(execution_authorized)
    return binding


def binding_execution_parameters(
    binding: Mapping[str, Any],
) -> tuple[str, list[str], list[str], bool] | None:
    """Validate and return the execution parameters stored in an authority binding."""

    profile = binding.get("profile")
    skills = binding.get("skills", [])
    default_skills = binding.get("default_skills", [])
    goal_mode = binding.get("goal_mode", False)
    if not isinstance(profile, str):
        return None
    if not isinstance(skills, list) or not all(
        isinstance(value, str) for value in skills
    ):
        return None
    if not isinstance(default_skills, list) or not all(
        isinstance(value, str) for value in default_skills
    ):
        return None
    if not isinstance(goal_mode, bool):
        return None
    return profile, list(skills), list(default_skills), goal_mode


def binding_matches_work(binding: Mapping[str, Any], item: WorkItem) -> bool:
    """Return whether a persisted authorization binding still matches work."""

    # Terminal work is never executable.  In addition to lifecycle mutations
    # advancing the authorization scope revision, keep this predicate
    # fail-closed for imported or legacy rows that may not have crossed the
    # current transition path.
    if item.status in TERMINAL_WORK_STATUSES:
        return False

    digest = binding.get("scope_digest")
    work_version = binding.get("work_version")
    scope_revision = binding.get("scope_revision")
    parameters = binding_execution_parameters(binding)
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or isinstance(work_version, bool)
        or not isinstance(work_version, int)
        or work_version < 1
        or work_version > item.version
        or isinstance(scope_revision, bool)
        or not isinstance(scope_revision, int)
        or scope_revision != item.authorization_scope_revision
        or parameters is None
    ):
        return False
    profile, skills, default_skills, goal_mode = parameters
    return digest == execution_scope_digest(
        item,
        profile=profile,
        skills=skills,
        default_skills=default_skills,
        goal_mode=goal_mode,
    )


def binding_matches_execution(
    binding: Mapping[str, Any],
    item: WorkItem,
    *,
    profile: str,
    skills: Iterable[str] = (),
    default_skills: Iterable[str] = (),
    goal_mode: bool = False,
) -> bool:
    """Require both a current work scope and the exact proposed executor scope."""

    if not binding_matches_work(binding, item):
        return False
    return binding.get("scope_digest") == execution_scope_digest(
        item,
        profile=profile,
        skills=skills,
        default_skills=default_skills,
        goal_mode=goal_mode,
    )
