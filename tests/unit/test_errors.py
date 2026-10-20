import pytest

from saddlefit.cli import ExitCode
from saddlefit.errors import EnvironmentFailure, IntegrityViolation, SaddlefitError, UserError


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (UserError, ExitCode.USER_ERROR),
        (IntegrityViolation, ExitCode.INTEGRITY_VIOLATION),
        (EnvironmentFailure, ExitCode.ENVIRONMENT_ERROR),
        (SaddlefitError, ExitCode.USER_ERROR),
    ],
)
def test_error_classes_map_to_exit_codes(cls: type[SaddlefitError], code: int) -> None:
    assert cls("x").exit_code == code
