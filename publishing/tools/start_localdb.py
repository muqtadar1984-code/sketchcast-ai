"""Start (or find) the local isolated publishing Postgres and print its URL.

Run with the separate local-DB venv, which holds the embedded server:
    .venv-localdb/Scripts/python.exe publishing/tools/start_localdb.py
then export the printed value as PUBDB_URL. The data directory sits outside
OneDrive on purpose: a synced folder corrupts a live Postgres cluster.
"""
import os

import pgserver

DATA_DIR = os.getenv("PUBDB_DATA", os.path.expanduser(r"~\sketchcast-pubdb"))

if __name__ == "__main__":
    srv = pgserver.get_server(DATA_DIR, cleanup_mode=None)
    print(srv.get_uri())
