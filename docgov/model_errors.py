"""Stable public error codes; never expose arbitrary model/provider text."""


def model_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "missing dependency" in message or "botocore[crt]" in message:
        return "aws_login_dependency_missing"
    if "currently being verified" in message:
        return "aws_account_verification_pending"
    if any(value in message for value in ("unable to locate credentials", "no credentials", "expiredtoken", "token has expired")):
        return "aws_login_required"
    if "accessdenied" in message or "access denied" in message:
        return "model_access_denied"
    if "throttl" in message:
        return "model_throttled"
    if "timeout" in message or "timed out" in message:
        return "model_timeout"
    return "model_plan_failed"
