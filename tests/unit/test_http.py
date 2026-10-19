import httpx
import pytest
import respx

from argus.collectors.http import AuthError, Fetcher, FetchError, make_client


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def fetcher(sleeps: Sleeps, **kw: object) -> Fetcher:
    return Fetcher(make_client(), sleep=sleeps, rand=lambda: 0.5, **kw)  # type: ignore[arg-type]


URL = "https://example.test/x"


@respx.mock
async def test_success_returns_body_and_sends_user_agent() -> None:
    route = respx.get(URL).respond(200, text="hello")
    r = await fetcher(Sleeps()).get(URL)
    assert r.text == "hello"
    assert route.calls[0].request.headers["User-Agent"].startswith("Argus/")


@respx.mock
async def test_retries_5xx_with_full_jitter_backoff() -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200, text="ok")]
    )
    sleeps = Sleeps()
    r = await fetcher(sleeps, base_delay=2.0).get(URL)
    assert r.text == "ok"
    assert sleeps.calls == [1.0, 2.0]  # 2*2^0*0.5, 2*2^1*0.5


@respx.mock
async def test_backoff_is_capped() -> None:
    f = fetcher(Sleeps(), base_delay=10.0, max_delay=15.0)
    assert f.backoff(5, None) == 7.5


@respx.mock
async def test_retry_after_is_honoured_and_capped() -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    sleeps = Sleeps()
    await fetcher(sleeps).get(URL)
    assert sleeps.calls == [7.0]
    assert fetcher(Sleeps(), max_delay=5.0).backoff(0, "900") == 5.0
    assert fetcher(Sleeps()).backoff(0, "Wed, 21 Oct 2026 07:28:00 GMT") == 1.0


@respx.mock
async def test_transport_errors_retry_then_give_up() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    sleeps = Sleeps()
    with pytest.raises(FetchError, match="after 3 attempts: ConnectError"):
        await fetcher(sleeps, max_attempts=3).get(URL)
    assert len(sleeps.calls) == 2


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_not_retried(status: int) -> None:
    route = respx.get(URL).respond(status)
    with pytest.raises(AuthError):
        await fetcher(Sleeps()).get(URL)
    assert route.call_count == 1


@respx.mock
async def test_other_4xx_is_permanent_and_query_is_not_logged() -> None:
    respx.get(URL).respond(404)
    with pytest.raises(FetchError) as e:
        await fetcher(Sleeps()).get(URL, params={"token": "s3cret"})
    assert "s3cret" not in str(e.value)


@respx.mock
async def test_conditional_get_headers_and_304() -> None:
    route = respx.get(URL).respond(304)
    r = await fetcher(Sleeps()).get(
        URL, etag='"abc"', last_modified="Mon, 19 Oct 2026 13:00:00 GMT"
    )
    assert r.not_modified
    req = route.calls[0].request
    assert req.headers["If-None-Match"] == '"abc"'
    assert req.headers["If-Modified-Since"] == "Mon, 19 Oct 2026 13:00:00 GMT"


@respx.mock
async def test_link_pagination_follows_next() -> None:
    respx.get(URL, params={"page": "1"}).respond(
        200, json=[1], headers={"Link": f'<{URL}?page=2>; rel="next", <{URL}?page=1>; rel="first"'}
    )
    respx.get(URL, params={"page": "2"}).respond(200, json=[2])
    pages = await fetcher(Sleeps()).get_all_pages(URL, params={"page": "1"})
    assert [p.json() for p in pages] == [[1], [2]]


@respx.mock
async def test_link_pagination_refuses_other_host() -> None:
    respx.get(URL).respond(
        200, json=[], headers={"Link": '<https://evil.test/x?page=2>; rel="next"'}
    )
    with pytest.raises(FetchError, match="another host"):
        await fetcher(Sleeps()).get_all_pages(URL)


@respx.mock
async def test_link_pagination_page_cap() -> None:
    respx.get(URL).respond(200, json=[], headers={"Link": f'<{URL}>; rel="next"'})
    with pytest.raises(FetchError, match="more than 3 pages"):
        await fetcher(Sleeps()).get_all_pages(URL, max_pages=3)


@respx.mock
async def test_json_helper() -> None:
    respx.post(URL).respond(200, json={"a": 1})
    r = await fetcher(Sleeps()).request("POST", URL, data={"k": "v"})
    assert r.json() == {"a": 1}
