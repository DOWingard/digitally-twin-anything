"""The document a twin is installed or revised from: its manifest and its ontology text.

The toolkit writes a bundle and the runtime reads it, so its format is defined
once, here, for both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml

from .errors import DtaError, ManifestTooNew
from .log import Log
from .manifest import Manifest, parse_manifest
from .manifest.io import VERSION_FIELD, to_plain

log = Log("core.bundle")


class BundleError(DtaError):
    """The text is not a bundle."""

    code = "twin.bundle_invalid"


@dataclass(frozen=True)
class Bundle:
    """Everything needed to install or revise a twin, as one document."""

    manifest: Manifest
    ontology_text: str

    def to_json(self) -> str:
        plain = to_plain(self.manifest)
        if plain.get(VERSION_FIELD) == 1:
            # A runtime from before manifests named their version reads version 1 and knows
            # no such field; a newer one reads the manifest the same without it.
            del plain[VERSION_FIELD]
        text = yaml.safe_dump(plain, sort_keys=False, allow_unicode=True)
        return json.dumps({"manifest": text, "ontology": self.ontology_text})

    @classmethod
    def from_json(cls, text: str) -> Bundle:
        """The bundle a text holds. A manifest newer than this software reads is refused
        as such (`manifest.too_new`), not as a text that is no bundle."""
        try:
            document = json.loads(text)
            manifest_text, ontology_text = document["manifest"], document["ontology"]
            if not isinstance(manifest_text, str) or not isinstance(ontology_text, str):
                raise TypeError("manifest and ontology must both be text")
            manifest = parse_manifest(yaml.safe_load(manifest_text), origin="bundle")
        except ManifestTooNew:
            raise
        except (ValueError, KeyError, TypeError, yaml.YAMLError, DtaError) as exc:
            log.error("bundle cannot be read", exc=exc)
            raise BundleError(
                f"the bundle is not a JSON document holding a manifest and an ontology: {exc}"
            ) from exc
        return cls(manifest=manifest, ontology_text=ontology_text)
