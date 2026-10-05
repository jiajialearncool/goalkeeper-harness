import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from goalkeeper.auth import AuthBroker, AuthError, ISSUER, SCOPES, SERVICE
from goalkeeper.models import ModelRequest, RunConfig
from goalkeeper.provider import ProviderError, ResponsesProvider


async def test_http_error_with_null_code_preserves_diagnostic_without_type_error():
    class Auth:
        async def access_token(self):
            return "synthetic"

    provider = ResponsesProvider(
        Auth(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    400, json={"error": {"code": None, "message": "invalid input history"}}
                )
            )
        ),
    )
    events = []
    with pytest.raises(ProviderError, match="invalid input history") as caught:
        async for event in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig())):
            events.append(event)
    assert caught.value.code == "http_400" and events[0].data["http_status"] == 400
    await provider.close()


class Vault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, key, value):
        self.values[(service, key)] = value

    def get_password(self, service, key):
        return self.values.get((service, key))

    def delete_password(self, service, key):
        self.values.pop((service, key), None)


@pytest.fixture
def oauth(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public["kid"] = "test"
    calls = []
    claims = {
        "iss": ISSUER,
        "sub": "test-sub",
        "aud": "oaiapp_test",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "nonce": "will-set",
    }

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [public]})
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "rotated-refresh",
                "id_token": token,
                "expires_in": 3600,
                "scope": SCOPES,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = AuthBroker(tmp_path, http=http, vault=Vault())
    url = broker.begin("http://127.0.0.1:8765/auth/callback", "browser")
    query = parse_qs(urlsplit(url).query)
    claims["nonce"] = broker.pending[query["state"][0]]["nonce"]
    return broker, query, claims, calls


async def test_own_oauth_pkce_and_no_codex_credentials(oauth):
    broker, query, _, calls = oauth
    assert query["client_id"] == ["dynamic_agent_client"] and query["code_challenge_method"] == ["S256"]
    assert query["scope"][0] == SCOPES
    await broker.callback(
        {"state": query["state"][0], "code": "synthetic-code", "client_id": "oaiapp_test"}, "browser"
    )
    assert broker.status()["connected"] and await broker.access_token() == "synthetic-access"
    assert "synthetic-access" not in broker.metadata_path.read_text()
    assert len(calls) == 2
    with pytest.raises(AuthError):
        await broker.callback({"state": query["state"][0], "code": "replay"}, "browser")


@pytest.mark.parametrize("failure", ["state", "browser", "nonce", "aud", "issuer", "expired"])
async def test_oauth_rejects_forged_or_expired_callback(oauth, failure):
    broker, query, claims, _ = oauth
    params = {"state": query["state"][0], "code": "synthetic", "client_id": "oaiapp_test"}
    browser = "browser"
    if failure == "state":
        params["state"] = "forged"
    if failure == "browser":
        browser = "wrong-browser"
    if failure == "nonce":
        claims["nonce"] = "wrong"
    if failure == "aud":
        claims["aud"] = "someone-else"
    if failure == "issuer":
        claims["iss"] = "https://wrong.invalid"
    if failure == "expired":
        claims["exp"] = int(time.time()) - 60
    with pytest.raises((AuthError, jwt.PyJWTError)):
        await broker.callback(params, browser)
    assert not broker.status()["connected"] and not broker.vault.values


async def test_refresh_rotation_keeps_identity_and_scope(oauth):
    broker, query, _, calls = oauth
    await broker.callback(
        {"state": query["state"][0], "code": "synthetic", "client_id": "oaiapp_test"}, "browser"
    )
    meta = broker.metadata()
    raw = json.loads(broker.vault.get_password(SERVICE, meta["account_key"]))
    raw["expires_at"] = 0
    raw["refresh_token"] = "old-refresh"
    broker.vault.set_password(SERVICE, meta["account_key"], json.dumps(raw))
    assert await broker.access_token() == "synthetic-access"
    new = json.loads(broker.vault.get_password(SERVICE, meta["account_key"]))
    assert new["refresh_token"] == "rotated-refresh"
    assert "refresh_token=old-refresh" in calls[-2].content.decode()


async def test_failed_code_exchange_retains_registration_without_signed_in_identity(oauth):
    broker, query, _, _ = oauth
    await broker.http.aclose()
    broker.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": "invalid_grant", "code": "must-not-be-logged"})
        )
    )
    with pytest.raises(AuthError, match="invalid_grant"):
        await broker.callback(
            {"state": query["state"][0], "code": "synthetic-code", "client_id": "oaiapp_test"}, "browser"
        )
    assert not broker.status()["connected"] and not broker.vault.values
    assert "must-not-be-logged" not in json.dumps(broker.status())
    retry = parse_qs(urlsplit(broker.begin("http://127.0.0.1:8765/auth/callback", "browser")).query)
    assert retry["client_id"] == ["oaiapp_test"] and "agent_name_hint" not in retry
    assert retry["code_challenge"] != query["code_challenge"]
    assert broker.status()["diagnostic"] is None
    await broker.close()


async def test_region_restriction_preserves_safe_error_diagnostic(oauth):
    broker, query, _, _ = oauth
    await broker.http.aclose()
    broker.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                403,
                json={
                    "error": {"code": "unsupported_country_region_territory", "message": "blocked"},
                    "access_token": "must-not-be-logged",
                },
                headers={"x-request-id": "request-diagnostic"},
            )
        )
    )
    with pytest.raises(AuthError, match="国家或地区"):
        await broker.callback(
            {"state": query["state"][0], "code": "synthetic-code", "client_id": "oaiapp_test"}, "browser"
        )
    assert broker.status()["diagnostic"] == {
        "stage": "code_exchange",
        "http_status": 403,
        "error_code": "unsupported_country_region_territory",
        "body_shape": "json_object",
        "request_id": "request-diagnostic",
    }
    assert "must-not-be-logged" not in json.dumps(broker.status())
    assert not broker.status()["connected"]
    await broker.close()


async def test_auth_and_model_clients_use_existing_system_proxy(monkeypatch, tmp_path):
    import httpx._utils

    seen = []

    def configured_proxies():
        seen.append(True)
        return {"https": "http://127.0.0.1:17897"}

    monkeypatch.setattr(httpx._utils, "getproxies", configured_proxies)
    broker = AuthBroker(tmp_path, vault=Vault())
    provider = ResponsesProvider(broker)
    assert len(seen) == 2
    assert not broker.status()["connected"]
    await provider.close()
    await broker.close()


class FakeAuth:
    async def access_token(self):
        return "synthetic-only"


async def test_stateless_stream_payload_and_usage_fields():
    body = {
        "type": "response.completed",
        "response": {"output": [], "usage": {"input_tokens": 2, "output_tokens": 3}},
    }

    def handler(request):
        data = json.loads(request.content)
        assert data["store"] is False and data["stream"] is True
        assert data["model"] == "gpt-6.1-sol"
        assert data["reasoning"] == {"effort": "xhigh"}
        assert "max_output_tokens" not in data and "previous_response_id" not in data
        return httpx.Response(
            200, text="data: " + json.dumps(body) + "\n\n", headers={"Content-Type": "text/event-stream"}
        )

    provider = ResponsesProvider(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    events = [
        e async for e in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig()))
    ]
    assert events[-1].type == "response.completed"
    await provider.close()


async def test_incomplete_stream_cannot_authorize_tool_execution():
    response = httpx.Response(
        200, text='data: {"type":"response.output_item.done","item":{"type":"function_call"}}\n\n'
    )
    provider = ResponsesProvider(
        FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
    )
    with pytest.raises(ProviderError) as result:
        _ = [
            e async for e in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig()))
        ]
    assert result.value.resource
    await provider.close()


async def test_legacy_model_cannot_reach_subscription_transport():
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("A historical model must not reach the subscription endpoint")

    provider = ResponsesProvider(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="旧模型记录只供查看"):
        _ = [
            event
            async for event in provider.stream(
                ModelRequest(instructions="historical", input=[], config=RunConfig(model="gpt-6-astra"))
            )
        ]
    assert not calls
    await provider.close()


@pytest.mark.parametrize("terminal_separator", ["\n\n", ""])
async def test_completed_stream_restores_items_when_terminal_output_is_empty(terminal_separator):
    call = {
        "type": "function_call",
        "id": "fc-test",
        "call_id": "call-test",
        "name": "write_file",
        "arguments": '{"path":"note.txt","content":"OK"}',
    }
    reasoning = {
        "type": "reasoning",
        "id": "rs-test",
        "summary": [],
        "encrypted_content": "synthetic-encrypted",
    }
    messages = [
        {"type": "response.output_item.done", "output_index": 1, "item": call},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-test",
                "output": [],
                "status": "completed",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        },
    ]
    stream = "\n\n".join("data: " + json.dumps(item) for item in messages) + terminal_separator
    provider = ResponsesProvider(
        FakeAuth(),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=stream))),
    )
    events = [
        event
        async for event in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig()))
    ]
    assert events[-1].data["response"]["output"] == [reasoning, call]
    assert events[-1].data["raw_terminal_response"]["output"] == []
    assert events[-1].data["response"]["usage"] == {"input_tokens": 2, "output_tokens": 3}
    await provider.close()


async def test_finished_tool_item_without_completed_response_is_not_executable():
    item = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {"type": "function_call", "call_id": "call-test", "name": "finish", "arguments": "{}"},
    }
    provider = ResponsesProvider(
        FakeAuth(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="data: " + json.dumps(item) + "\n\n")
            )
        ),
    )
    received = []
    with pytest.raises(ProviderError, match="response.completed"):
        async for event in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig())):
            received.append(event.type)
    assert received == ["response.output_item.done"]
    await provider.close()


async def test_quota_failure_is_resource_interruption():
    response = httpx.Response(429, json={"error": {"code": "usage_limit_reached", "message": "limit"}})
    provider = ResponsesProvider(
        FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
    )
    with pytest.raises(ProviderError) as result:
        _ = [
            e async for e in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig()))
        ]
    assert result.value.resource
    await provider.close()


@pytest.mark.parametrize(
    "code,error_type,resource",
    [
        ("server_is_overloaded", "service_unavailable_error", True),
        ("internal_error", "service_unavailable_error", True),
        ("invalid_request", "invalid_request_error", False),
    ],
)
async def test_stream_failure_classification_never_replays_partial_tools(code, error_type, resource):
    events = [
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": "partial-call",
                "name": "write_file",
                "arguments": '{"path":"note.txt","content":"never-execute"}',
            },
        },
        {"type": "error", "error": {"code": code, "type": error_type, "message": "synthetic"}},
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events))

    provider = ResponsesProvider(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    received = []
    with pytest.raises(ProviderError) as caught:
        async for event in provider.stream(ModelRequest(instructions="test", input=[], config=RunConfig())):
            received.append(event.type)
    assert caught.value.resource is resource and caught.value.code == code
    assert len(calls) == 1 and "response.completed" not in received
    await provider.close()
