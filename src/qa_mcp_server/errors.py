"""Exception hierarchy.

The audit log and the MCP layer classify failures by type:

* :class:`PolicyViolation` -> outcome ``rejected`` (bad input, path outside the
  sandbox, command not on the allowlist). Nothing was executed.
* every other :class:`QAMCPError` -> outcome ``error`` (the command ran, or tried
  to, and failed).
"""

from __future__ import annotations


class QAMCPError(Exception):
    """Base class for all errors raised by qa-mcp-server."""


class PolicyViolation(QAMCPError):
    """Input or command rejected by a security policy before anything ran."""


class DeviceError(QAMCPError):
    """The target device is missing, offline or ambiguous."""


class CommandFailed(QAMCPError):
    """A command ran but returned a failure (non-zero exit, adb error text)."""


class CommandTimeout(QAMCPError):
    """A command exceeded its timeout and was killed."""


class OutputTooLarge(QAMCPError):
    """A command produced more output than its cap allows and the result is unusable."""
