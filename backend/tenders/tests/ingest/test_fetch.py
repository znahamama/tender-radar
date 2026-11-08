import io
import urllib.error

import pytest

from tenders.ingest import fetch


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, length: str | None = None):
        super().__init__(body)
        self.headers = {"Content-Length": length} if length else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _fake_urlopen(responses, seen):
    def urlopen(req, timeout):
        seen.append((req.get_header("User-agent"), timeout))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return urlopen


def _http_error(code):
    return urllib.error.HTTPError("https://x/f.csv", code, "err", {}, None)


def test_download_retries_then_succeeds(tmp_path, monkeypatch):
    seen = []
    responses = [_http_error(503), urllib.error.URLError("dns"), FakeResp(b"a,b\n1,2\n")]
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _fake_urlopen(responses, seen))
    path = fetch.download("https://x/f.csv", tmp_path, backoff_s=0)
    assert path == tmp_path / "f.csv"
    assert path.read_bytes() == b"a,b\n1,2\n"
    assert seen == [(fetch.USER_AGENT, 60)] * 3
    assert list(tmp_path.iterdir()) == [path]  # no leftover temp file


def test_download_gives_up_after_max_retries(tmp_path, monkeypatch):
    seen = []
    responses = [_http_error(503)] * 4
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _fake_urlopen(responses, seen))
    with pytest.raises(urllib.error.HTTPError):
        fetch.download("https://x/f.csv", tmp_path, backoff_s=0)
    assert len(seen) == 4  # 1 try + 3 retries


def test_download_does_not_retry_404(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _fake_urlopen([_http_error(404)], seen))
    with pytest.raises(urllib.error.HTTPError):
        fetch.download("https://x/f.csv", tmp_path, backoff_s=0)
    assert len(seen) == 1


@pytest.mark.parametrize("length", [None, "100"])
def test_download_aborts_over_size_limit(tmp_path, monkeypatch, length):
    seen = []
    monkeypatch.setattr(
        fetch.urllib.request, "urlopen", _fake_urlopen([FakeResp(b"x" * 100, length)], seen)
    )
    with pytest.raises(fetch.DownloadTooLarge):
        fetch.download("https://x/f.csv", tmp_path, max_bytes=50, backoff_s=0)
    assert list(tmp_path.iterdir()) == []  # partial file cleaned up
    assert len(seen) == 1  # not retried
