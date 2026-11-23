"""Custom-resource handlers behind cross-stack references.

``writer`` backs ``Custom::CrossRegionExportWriter`` (one per producer stack) and ``reader`` backs
``Custom::CrossRegionExportReader`` (one per consumer stack).  Both are run by the stack engine as
``python3 -m crossref.writer`` / ``python3 -m crossref.reader``; see docs/cross-stack-references.md.
"""

SSM_EXPORT_PATH_PREFIX = "cdk/exports/"
