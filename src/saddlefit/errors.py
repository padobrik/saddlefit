"""Error classes that map onto the CLI's exit codes (B14)."""

from __future__ import annotations


class SaddlefitError(Exception):
    """Base class for errors the CLI reports without a traceback."""

    exit_code = 1


class UserError(SaddlefitError):
    """Bad input, configuration or usage: exit code 1."""

    exit_code = 1


class IntegrityViolation(SaddlefitError):
    """A leak, tamper or sealed-store access: exit code 2."""

    exit_code = 2


class EnvironmentFailure(SaddlefitError):
    """Docker, the model runner or the filesystem failed: exit code 3."""

    exit_code = 3
