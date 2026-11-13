"""Exceptions raised by the parse service and its text-extraction backends."""

from __future__ import annotations


class ParseError(Exception):
    """Base class for all parse-service failures."""


class UnsupportedFileTypeError(ParseError):
    """The requested filetype has no parser."""


class DocumentLoadError(ParseError):
    """A document could not be opened, rendered or converted."""


class InvalidBackendError(ParseError):
    """Unknown parser provider, or a provider that is missing required configuration."""


class BackendCallError(ParseError):
    """A transient backend failure (timeout, 5xx, rate limit, empty reply). Retryable."""


class BackendRequestError(ParseError):
    """A permanent backend failure (bad credentials, rejected request). Not retried."""
