"""Exercise the installed TypeSafe SDK with an in-memory HTTP transport."""
import json

import pytest

import jev


def test_typesafe_wire_contract(monkeypatch):
    sdk = pytest.importorskip("typesafe_sdk")
    http = pytest.importorskip("httpx2")
    client_type = sdk.TypeSafeClient
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return http.Response(200, json={
            "model": jev.MODEL,
            "usage": {"input_tokens": 123, "output_tokens": 0},
            "answers": {
                "safe": {"type": "noul", "noul": 0.95},
                "pick": {"type": "choice", "choice": "yes", "confidence": 0.9,
                         "probabilities": {"yes": 0.95, "none": 0.05}},
                "fit": {"type": "score", "score": 0.75, "confidence": 0.8,
                        "probabilities": {"0": 0.25, "1": 0.75},
                        "legend": {"0": "absent", "1": "present"}},
            },
        })

    clients = []

    def isolated_client(**kwargs):
        client = client_type(**kwargs, transport=http.MockTransport(respond),
                             base_url="https://typesafe.example.test/v1")
        clients.append(client)
        return client

    monkeypatch.setattr(sdk, "TypeSafeClient", isolated_client)
    questions = {
        "safe": {"type": "noul", "instructions": {"question": "Is it safe?"}},
        "pick": {"type": "choice", "instructions": "Pick an option",
                 "criteria": {"yes": {"meaning": "supported"}, "none": None}},
        "fit": {"type": "score", "instructions": "Rate fit",
                "criteria": ["absent", "present"]},
    }
    jev.reset_usage()
    try:
        judge = jev.TypeSafeJev(api_key="synthetic-test-key")
        answers = judge.judge({"text": "synthetic application"}, questions)
        assert requests == [{"state": {"text": "synthetic application"},
                             "questions": questions, "model": jev.MODEL}]
        assert answers["safe"].noul == 0.95
        assert answers["pick"].choice == "yes"
        assert answers["pick"].confidence == 0.9
        assert answers["fit"].score == 0.75
        assert answers["fit"].probabilities == {"0": 0.25, "1": 0.75}
        assert judge.last_model == jev.MODEL
        assert jev.usage()["input_tokens"] == 123
    finally:
        for client in clients:
            client.close()
        jev.reset_usage()
