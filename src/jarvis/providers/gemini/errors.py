"""Safe provider diagnostics: never propagate SDK messages or credentials."""


class ProviderError(RuntimeError):
    """An explicit provider operation could not complete."""


class ProviderConfigurationError(ProviderError):
    """Settings or credentials are missing or invalid."""


class ProviderDependencyError(ProviderError):
    """The optional Google SDK is unavailable."""


class ProviderAuthenticationError(ProviderError):
    """The configured API key was rejected."""


class ProviderPermissionError(ProviderError):
    """The project is not permitted to use the requested resource."""


class ProviderModelError(ProviderError):
    """The configured model is unavailable."""


class ProviderQuotaError(ProviderError):
    """A quota or rate limit prevents the operation."""


class ProviderNetworkError(ProviderError):
    """The connection failed or closed unexpectedly."""


class ProviderTimeout(ProviderError):
    """A bounded operation did not finish in time."""


class ProviderProtocolError(ProviderError):
    """Provider data is unsupported or inconsistent."""


class ProviderBackpressureError(ProviderError):
    """The bounded output pipeline cannot accept more audio."""


def classify_error(error: Exception) -> ProviderError:
    """Inspect codes/reasons for classification, but emit fixed text only."""
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, TimeoutError):
        return ProviderTimeout("Gemini operation timed out; no retry made.")
    code = getattr(error, "code", None)
    response = getattr(error, "response", None)
    if response is not None:
        code = getattr(response, "status_code", code)
    received = getattr(error, "rcvd", None)
    if received is not None:
        code = getattr(received, "code", code)
    # The original reason may contain a key or personal content: never echo it.
    reason = str(error)[:4096].casefold()
    if code == 429 or any(word in reason for word in (
        "resource_exhausted", "quota", "rate limit",
    )):
        return ProviderQuotaError(
            "Gemini quota/rate limit reached. Check the project's free-tier "
            "limits; no retries or paid-model fallback were made."
        )
    if (
        code == 404 or ("models/" in reason and "not found" in reason)
        or any(word in reason for word in (
            "model not found", "model is not found", "not supported for bidi",
        ))
    ):
        return ProviderModelError(
            "Gemini model unavailable. Check GEMINI_MODEL and project access."
        )
    if code == 401 or any(word in reason for word in (
        "api key not valid", "api_key_invalid", "invalid api key",
        "unauthenticated", "revoked",
    )):
        return ProviderAuthenticationError(
            "Gemini rejected the API key; update it locally. No retry made."
        )
    if code == 403 or "permission" in reason or "billing" in reason:
        return ProviderPermissionError(
            "Gemini project permission/billing restriction. Check access "
            "without automatically enabling billing or changing the project."
        )
    if code in (400, 1008):
        return ProviderProtocolError(
            "Gemini rejected the model or session configuration."
        )
    return ProviderNetworkError(
        "Gemini connection failed or closed. No automatic reconnect was made."
    )
