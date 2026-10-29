"""publishing/localdb.py refuses any publishing database that is not on this machine."""
import pytest

from publishing import localdb


@pytest.mark.parametrize("url", [
    "postgresql://postgres:@127.0.0.1:58999/postgres",
    "postgresql://postgres:@localhost:5432/postgres",
    "postgresql://postgres:@/postgres?host=/tmp/tmpabc123",          # the embedded server on Linux (books runner)
])
def test_local_urls_are_accepted(monkeypatch, url):
    monkeypatch.setenv("PUBDB_URL", url)
    assert localdb.db_url() == url


@pytest.mark.parametrize("url", [
    "postgresql://postgres:x@db.pblaxsqhjwlqbrwbraxy.supabase.co:5432/postgres",   # production: never
    "postgresql://postgres:@/postgres",                                            # no host and no socket
    "postgresql://postgres:@/postgres?host=db.example.com",                         # a network host smuggled in
])
def test_anything_else_is_refused(monkeypatch, url):
    monkeypatch.setenv("PUBDB_URL", url)
    with pytest.raises(localdb.NotLocal):
        localdb.db_url()
