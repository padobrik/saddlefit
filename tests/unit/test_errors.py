import pytest

from saddlefit.cli import ExitCode
from saddlefit.config import ConfigError
from saddlefit.errors import EnvironmentFailure, IntegrityViolation, SaddlefitError, UserError
from saddlefit.harness.loop_config import LoopConfigError
from saddlefit.sandbox.container import SandboxError
from saddlefit.tasks.splits import SplitError


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


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (ConfigError, ExitCode.USER_ERROR),
        (LoopConfigError, ExitCode.USER_ERROR),
        (SplitError, ExitCode.USER_ERROR),
        (SandboxError, ExitCode.ENVIRONMENT_ERROR),
    ],
)
def test_domain_errors_reach_the_cli_with_their_exit_code(
    cls: type[SaddlefitError], code: int
) -> None:
    assert issubclass(cls, SaddlefitError)
    assert cls("x").exit_code == code
