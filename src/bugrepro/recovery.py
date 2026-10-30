"""Fixed, content-free diagnostics for managed pre-dispatch recovery."""

FAILURE_MESSAGES = {
    "locator_missing": "No matching control became available; no input was issued.",
    "locator_ambiguous": "The locator matched multiple controls; no input was issued.",
    "control_disabled": "The control is disabled; no input was issued.",
    "control_hidden": "The control has no visible box; no input was issued.",
    "control_readonly": "The text control is read-only; no input was issued.",
    "fixed_outside_viewport": "The control is fixed outside the viewport; no click was issued.",
}
APPLICATION_ACTIONS = frozenset({"goto", "click", "fill", "reload"})


def validate_recovery_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2:
        raise ValueError("action_recovery_limit must be an integer from 0 to 2.")
    return value


class PreDispatchFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(FAILURE_MESSAGES[code])


def validate_export_mapping(data, recipe) -> None:
    """A recovery recipe is an ordered projection, never the full attempt trace."""
    from .recipe import recipe_fingerprint

    actions = data["actions"]
    indices = [a["index"] for a in actions]
    if indices != list(range(1, len(actions) + 1)):
        raise ValueError("Invalid attempt indices.")
    if any(a["status"] not in {"ok", "failed"} or a["type"] != a["input"]["type"] for a in actions):
        raise ValueError("Invalid attempt outcome or type.")
    successes = [a for a in actions if a["status"] == "ok"]
    omitted = [a["index"] for a in actions if a["status"] != "ok"]
    export = data["export"]
    recovery = data["recovery"]
    validate_recovery_limit(recovery["limit"])
    if (export.get("path_semantics") != "successful_actions"
            or export.get("source_attempt_indices") != [a["index"] for a in successes]
            or export.get("omitted_attempt_indices") != omitted
            or [a["input"] for a in successes] != [a.to_dict() for a in recipe.actions]
            or recipe_fingerprint(recipe) != data["recipe"]["sha256"]
            or recipe.expectation.fingerprint() != data["expectation"]["fingerprint"]
            or data["execution_errors"] or recovery["fatal"] or recovery["pending_attempt_indices"]
            or data["budget"]["exhausted"] or data["check"]["status"] not in {"passed", "failed"}):
        raise ValueError("Recovery export does not match the successful path.")
    failures = data["action_failures"]
    if ([f["index"] for f in failures] != omitted or len(failures) != recovery["used"]
            or recovery["recovered_count"] != len(failures)):
        raise ValueError("Failure history does not match the projection.")
    if not 0 <= recovery["used"] <= recovery["limit"]:
        raise ValueError("Invalid recovery count.")
    for failure in failures:
        resolved = failure.get("resolved_by")
        if (failure.get("recoverable") is not True or failure.get("phase") != "before_dispatch"
                or failure.get("code") not in FAILURE_MESSAGES
                or not isinstance(resolved, int) or resolved <= failure["index"]
                or not any(a["index"] == resolved and a["type"] in APPLICATION_ACTIONS for a in successes)):
            raise ValueError("An omitted failure was not recovered before export.")
