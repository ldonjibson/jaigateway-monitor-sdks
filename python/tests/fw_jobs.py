"""Job functions importable by RQ workers in tests."""
import insight_sdk


def boom(secret_arg="s3cret-argument"):
    insight_sdk.set_user({"id": "job-user"})
    raise ValueError("job boom")


def fine():
    insight_sdk.set_tag("leak", "1")
    return 42


def flaky_always():
    raise RuntimeError("always fails")
