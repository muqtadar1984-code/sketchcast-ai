"""Constrained SymPy math microservice.

Deployed as a SECOND Railway service from this repo (see mathsvc/README.md).
The Next.js AI-tutor orchestrator posts structured tool calls to /math; only
whitelisted operations on validated expressions ever run — no model-generated
code is executed here.
"""
