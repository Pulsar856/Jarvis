"""Expected audio failures, distinct from generic application errors."""


class AudioError(RuntimeError):
    """Base class for an unsuccessful explicit audio operation."""


class BackendUnavailable(AudioError):
    """A required audio utility is not installed or executable."""


class DeviceNotFound(AudioError):
    """The requested input or output is missing or ambiguous."""


class AudioValidationError(AudioError):
    """Arguments, paths, or WAV contents are unsuitable."""


class AudioOperationError(AudioError):
    """An audio utility could not complete an operation."""


class AudioTimeout(AudioError):
    """An operation exceeded its bounded deadline."""


class AudioCancelled(AudioError):
    """An operation was cancelled by a process signal."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__("Audio operation cancelled; resources released.")


class StreamCancelled(AudioError):
    """An owner requested programmatic cancellation of a PCM session."""
