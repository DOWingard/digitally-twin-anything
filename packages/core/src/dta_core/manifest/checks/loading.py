"""What the build's load would refuse in the sources' mappings and views, found earlier.

The load plans each run from the manifest and the ontology and refuses, with
`sync.mapping_invalid`, a manifest whose views or mappings it cannot carry out.
The problems it can see in the manifest alone are reported here, under the same
code and with the same words, so that `dta manifest validate` refuses them
before a build is spent on them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

from ...schema import FactKind
from .findings import Issue, check
from .sqlrefs import scan
from .view import ManifestView

MAPPING_INVALID = "sync.mapping_invalid"


def _view_issues(view: ManifestView) -> Iterator[Issue]:
    for sid, source in sorted(view.sources.items()):
        for name, text in sorted((source.views or {}).items()):
            path = f"sources.{sid}.views.{name}"
            found = scan(text or "")
            if found.statements > 1:
                yield Issue(path, f"holds {found.statements} statements; exactly one is allowed")
                continue
            reported: set[tuple[str, ...]] = set()
            for parts in found.tables:
                if parts in reported:
                    continue
                reported.add(parts)
                if len(parts) == 1:
                    if parts[0] in found.ctes:
                        continue
                    yield Issue(
                        path,
                        f'refers to {parts[0]} without its source; write "<source>"."{parts[0]}"',
                    )
                elif len(parts) == 2:
                    if parts[0] not in view.sources:
                        yield Issue(
                            path,
                            f"refers to source {parts[0]!r}, which the manifest does not have",
                        )
                else:
                    yield Issue(
                        path, f'names the catalog {parts[0]!r}; write "<source>"."<dataset>"'
                    )


def _mapping_issues(view: ManifestView) -> Iterator[Issue]:
    feeding: Counter[str] = Counter()
    for source in view.sources.values():
        for mapping in (source.mappings or {}).values():
            if mapping.object_type:
                feeding[mapping.object_type] += 1
    for sid, source in sorted(view.sources.items()):
        series: dict[str, set[str]] = {}
        for mid, mapping in sorted((source.mappings or {}).items()):
            base = f"sources.{sid}.mappings.{mid}"
            for prop, how in sorted((mapping.properties or {}).items()):
                if (how.column is None) == (how.expression is None):
                    yield Issue(
                        f"{base}.properties.{prop}", "give exactly one of column and expression"
                    )
            if mapping.kind == FactKind.simulated:
                yield Issue(f"{base}.kind", "simulated facts cannot be loaded from a source")
            if mapping.mode == "snapshot":
                if mapping.valid_time is not None:
                    yield Issue(
                        f"{base}.mode",
                        "snapshot cannot be used with valid_time: rows absent from a time "
                        "series are not deletions",
                    )
                type_name = mapping.object_type
                if type_name and feeding[type_name] != 1:
                    yield Issue(
                        f"{base}.mode",
                        f"snapshot needs to be the only mapping of {type_name}, which "
                        f"{feeding[type_name]} mappings feed: an object absent from one of them "
                        "may come from another",
                    )
            if mapping.valid_time is not None and mapping.dataset:
                series.setdefault(mapping.dataset, set()).add(mapping.valid_time)
        for relation, columns in sorted(series.items()):
            if len(columns) > 1:
                yield Issue(
                    f"sources.{sid}",
                    f"the time-series mappings of {relation} name different valid_time "
                    f"columns ({', '.join(sorted(columns))}); a dataset is read up to one time",
                )


@check(MAPPING_INVALID)
def mapping_invalid(view: ManifestView) -> Iterator[Issue]:
    yield from _view_issues(view)
    yield from _mapping_issues(view)
