"""Opening a connector from a source entry of the manifest."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..errors import ConfigError
from ..log import Log
from ..manifest.models import ConnectorKind, Source
from ..sources import Connector, SourceConfigError, placeholders, resolve_placeholders
from .base import Redactor

log = Log("connect")


def open_connector(source_id: str, source: Source, env: Mapping[str, str]) -> Connector:
    """Fill the location's `${NAME}` placeholders from `env` and open the source read-only.

    Raises `ConfigError` (`config.missing_env`) naming every variable that is
    missing, `SourceConfigError` for a kind that is not available or options
    that cannot be used, and the other `SourceError`s when the source cannot
    be reached. The filled-in location never appears in a log line or error.
    """
    kind = source.connector
    location = source.location
    context: dict[str, Any] = {
        "source": source_id,
        "kind": kind.value if kind is not None else None,
        "location": location,
    }
    if kind is None:
        raise _refuse("the source names no connector kind", context)
    if not location:
        raise _refuse("the source has no location", context)
    if kind is ConnectorKind.custom:
        raise _refuse(f"the {kind.value} connector kind is not available", context)
    # Only an HTTP source keeps secrets outside its address: the values of its headers.
    options: Mapping[str, Any] = source.options
    option_names = _placeholders_in(options) if kind is ConnectorKind.http else []
    names = list(dict.fromkeys([*placeholders(location), *option_names]))
    try:
        resolved = resolve_placeholders(location, env)
        if option_names:
            options = _filled(options, env)
    except ConfigError as exc:
        log.error(
            "the source's placeholders cannot be filled",
            code=exc.code,
            missing=exc.detail.get("missing"),
            **context,
        )
        raise
    secrets = [env[name] for name in names]
    if resolved != location:
        secrets.append(resolved)
    redactor = Redactor(secrets)
    log.debug("opening connector", placeholders=names, **context)
    if kind is ConnectorKind.http:
        try:
            from .http import open_http
            from .http import parse_options as parse_http_options
        except ImportError:
            raise _refuse("the http connector kind is not available", context) from None
        http_settings = _parse(parse_http_options, options, context)
        return open_http(source_id, location, resolved, http_settings, redactor)
    if kind is ConnectorKind.sql:
        from .sql import open_sql
        from .sql import parse_options as parse_sql_options

        settings = _parse(parse_sql_options, source.options, context)
        return open_sql(source_id, location, resolved, settings, redactor)
    from .files import open_files
    from .files import parse_options as parse_file_options

    datasets = _parse(parse_file_options, source.options, context)
    return open_files(source_id, location, resolved, datasets, redactor)


def _placeholders_in(value: Any) -> list[str]:
    """Names of the variables the texts inside a nested option value refer to."""
    if isinstance(value, str):
        return placeholders(value)
    if isinstance(value, Mapping):
        return [name for item in value.values() for name in _placeholders_in(item)]
    if isinstance(value, list | tuple):
        return [name for item in value for name in _placeholders_in(item)]
    return []


def _filled(value: Any, env: Mapping[str, str]) -> Any:
    """The option value with every `${NAME}` in its texts filled in."""
    if isinstance(value, str):
        return resolve_placeholders(value, env)
    if isinstance(value, Mapping):
        return {key: _filled(item, env) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_filled(item, env) for item in value]
    return value


def _parse[Parsed](
    parse: Callable[[Mapping[str, Any]], Parsed],
    raw: Mapping[str, Any],
    context: dict[str, Any],
) -> Parsed:
    try:
        return parse(raw)
    except SourceConfigError as exc:
        exc.detail.update(context)
        log.error(exc.message, code=exc.code, option=exc.detail.get("option"), **context)
        raise


def _refuse(message: str, context: dict[str, Any]) -> SourceConfigError:
    log.error(message, code=SourceConfigError.code, **context)
    return SourceConfigError(message, detail=dict(context))
