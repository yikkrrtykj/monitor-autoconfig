"""T-05 当前 AP 身份、独立窗口与共享 FDB 回归。"""
import json
import time
from unittest.mock import patch

import pytest
import ap_topology as ap
from .test_topology_server_librenms import gte, device, ServerClient


def identity(n=1, **labels):
    return {"metric": {"type": "uap", "ip": f"10.1.0.{n}",
                       "mac": f"02:00:00:00:00:{n:02x}", "name": f"AP-{n}", **labels}}


def candidate(mac, ip="10.0.0.11", index=1, age=6000, **fields):
    return {"mac": mac, "switch_ip": ip, "ifindex": index,
            "port_name": "Gi1/0/1", "vlan": 42,
            "evidence_age_seconds": age, **fields}


def test_current_identity_rejects_duplicate_missing_and_non_uap():
    rows = [identity(), identity(2), identity(2, mac="02:00:00:00:00:03"),
            identity(4, mac=""), identity(5, type="usw"), identity(6, ip="bad"),
            identity(7, mac="02:00:00:00:00:01")]
    assert ap.current_ap_identities(rows) == []
    assert ap.current_ap_identities([identity()])[0]["mac"] == "02:00:00:00:00:01"
    with pytest.raises(ValueError):
        ap.current_ap_identities([identity()] * 513)


def test_single_bounded_prometheus_request(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, bound):
            assert bound == ap.MAX_RESPONSE_BYTES + 1
            return json.dumps({"status": "success", "data": {"resultType": "vector", "result": [identity()]}}).encode()
    calls = []
    def request(url, timeout):
        calls.append((url, timeout)); return Response()
    monkeypatch.setattr(ap, "urlopen", request)
    assert len(ap.fetch_current_aps()) == 1
    assert len(calls) == 1 and calls[0][1] == 5
    assert "/api/v1/query?" in calls[0][0]


def test_no_uap_inert():
    assert ap.build_ap_artifact([], {})["attachments"] == []
