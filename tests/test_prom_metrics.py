"""F26 NICo /metrics — Prometheus text exposition passes the promtool-style lint."""
import os
import shutil
import subprocess

from app import prom


def test_metrics_text_format_and_lint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"] == prom.CONTENT_TYPE_TEXT
    assert prom.lint(r.text) == []
    s = prom.parse(r.text)
    assert s["nico_build_info"][0][1] == 1
    statuses = {lab["status"] for lab, _ in s["nico_machines"]}
    assert statuses <= {"Initializing", "Ready", "InUse", "Reset", "Error", "Decommissioned"}
    assert sum(v for _, v in s["nico_machines"]) > 0
    assert sum(v for _, v in s["nico_racks"]) > 0


def test_metrics_openmetrics(client):
    r = client.get("/metrics", headers={"accept": "application/openmetrics-text; version=1.0.0"})
    assert r.headers["content-type"] == prom.CONTENT_TYPE_OM
    assert r.text.endswith("# EOF\n")
    assert prom.lint(r.text, openmetrics=True) == []


def test_lint_catches_bad_exposition():
    bad = ("# TYPE x_count counter\nx_count 1\n# HELP y z\n# TYPE y gauge\ny{a=\"1\"} 1\n"
           "y{a=\"1\"} 2\nz{modelName=\"r\"} nope\n")
    p = " | ".join(prom.lint(bad))
    for frag in ("_total", "duplicate series", "no help text", "camelCase", "invalid value"):
        assert frag in p, frag


def test_promtool_when_available(client, tmp_path):
    tool = os.environ.get("PROMTOOL") or shutil.which("promtool")
    if not tool:
        import pytest
        pytest.skip("promtool not installed (set PROMTOOL=/path/to/promtool)")
    out = subprocess.run([tool, "check", "metrics"], input=client.get("/metrics").text,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
