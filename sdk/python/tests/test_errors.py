from __future__ import annotations

import pytest
from conftest import lines
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError
from qupai_lab.errors import from_response


def test_instrucore_error_shape() -> None:
    body = (b'{"error": {"code": "BENCH_HELD", "message": "\xe5\xae\x9e\xe9\xaa\x8c\xe5\x8f\xb0 is held", '
            b'"detail": {"holder": {"other_org": true}}}, "req_id": "abc"}')
    e = from_response(409, body, "application/json")
    assert (e.code, e.message, e.status) == ("BENCH_HELD", "实验台 is held", 409)
    assert e.detail == {"holder": {"other_org": True}, "req_id": "abc"}


def test_gateway_problem_json() -> None:
    body = (b'{"type": "urn:qupai:problem:invalid-key", "title": "Invalid key", "status": 401, '
            b'"detail": "The key is not valid"}')
    e = from_response(401, body, "application/problem+json")
    assert (e.code, e.message, e.status) == ("INVALID_KEY", "The key is not valid", 401)
    assert e.detail == {"type": "urn:qupai:problem:invalid-key", "title": "Invalid key", "source": "gateway"}
    e = from_response(503, b'{"type": "urn:qupai:problem:unavailable", "title": "Unavailable"}', "")
    assert (e.code, e.message) == ("UNAVAILABLE", "Unavailable")


def test_other_answers() -> None:
    assert from_response(502, b"<html>Bad gateway</html>").code == "HTTP_502"
    assert from_response(502, b"<html>Bad gateway</html>").message == "<html>Bad gateway</html>"
    assert from_response(500, b"").message == "HTTP 500"
    assert from_response(404, b'{"error": "as the upstream said"}').message == "as the upstream said"


def test_problem_json_raised_from_a_call(lab: Lab, fake: FakeInstruCore) -> None:
    fake.respond("GET", "/v1/machines", 403, {"type": "urn:qupai:problem:forbidden", "title": "Not allowed",
                                              "status": 403, "detail": "you hold no role on this lab"},
                 content_type="application/problem+json")
    with pytest.raises(LabError) as e:
        lab.machines()
    assert (e.value.code, e.value.message, e.value.status) == ("FORBIDDEN", "you hold no role on this lab", 403)
    assert lines(lab.recorder.path)[-1]["code"] == "FORBIDDEN"


def test_429_waits_for_retry_after_and_retries(lab: Lab, fake: FakeInstruCore) -> None:
    waits: list[float] = []
    lab._sleep = waits.append
    rate = {"type": "urn:qupai:problem:rate", "title": "Too many requests", "status": 429, "detail": "slow down"}
    fake.respond("GET", "/v1/machines", 429, rate, headers={"Retry-After": "3"}, content_type="application/problem+json")
    fake.respond("GET", "/v1/machines", 429, rate, content_type="application/problem+json")
    assert len(lab.machines()) == 3
    assert waits == [3.0, 2.0]  # the header's, then the default
    assert len(fake.calls("GET", "/v1/machines")) == 3
    lab.close()  # a dict result waits for the next line, in case the script saves it
    line = lines(lab.recorder.path)[-1]
    assert (line["ok"], line["status"], line["retries"]) == (True, 200, 2)


def test_429_gives_up_after_five_retries(lab: Lab, fake: FakeInstruCore) -> None:
    waits: list[float] = []
    lab._sleep = waits.append
    fake.respond("GET", "/v1/machines", 429, {"type": "urn:qupai:problem:rate", "title": "Too many requests",
                                              "detail": "slow down"}, headers={"Retry-After": "1"}, times=10)
    with pytest.raises(LabError) as e:
        lab.machines()
    assert (e.value.code, e.value.status, e.value.message) == ("RATE", 429, "slow down")
    assert waits == [1.0] * 5 and len(fake.calls("GET", "/v1/machines")) == 6
