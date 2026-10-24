"""Study-guide publishing engine (Sprint 1: scope, audit, readiness).

This package sits DOWNSTREAM of the catalogue lifecycle
(docs: study-guide-publishing-architecture.md, revision 2). In Sprint 1 it
writes only to a local isolated Postgres. It reads production only through
GET requests in ``publishing.snapshot``.
"""
