"""Structure of the publication workflow (.github/workflows/refresh.yml): ordering, outputs, gating, concurrency and no schedule.

These read the workflow file, they do not run it. They pin the properties the publication safeguards depend on, so a later edit
that moves a check after the upload, drops a job output or adds a schedule fails here first.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

from pipeline import live_check
from pipeline.test_live_check import Site, dash_first_report, now_iso, report

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WF = yaml.safe_load((WORKFLOWS / "refresh.yml").read_text(encoding="utf-8"))
BUILD, DEPLOY = WF["jobs"]["build"], WF["jobs"]["deploy"]


def names(job):
    return [s.get("name") or s.get("uses") or s.get("id") for s in job["steps"]]


def index(job, text):
    """Position of the first step whose name, `uses` or `run` contains `text`."""
    for i, s in enumerate(job["steps"]):
        if text in (s.get("name") or "") or text in (s.get("uses") or "") or text in (s.get("run") or ""):
            return i
    raise AssertionError(f"no step mentioning {text!r} in {names(job)}")


def step(job, text):
    return job["steps"][index(job, text)]


def triggers(wf):
    return wf.get("on", wf.get(True))        # PyYAML reads the bare key `on` as the boolean True


# ---------- no schedule, manual only, defaults ----------

def test_only_a_manual_dispatch_starts_the_publication_workflow():
    t = triggers(WF)
    assert set(t) == {"workflow_dispatch"}


def test_no_workflow_has_a_schedule_or_cron():
    for f in WORKFLOWS.glob("*.yml"):
        wf = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert "schedule" not in (triggers(wf) or {}), f.name
        assert "cron" not in yaml.safe_dump(wf), f.name


def test_the_inputs_and_their_defaults_are_unchanged():
    inputs = triggers(WF)["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "preview" and inputs["mode"]["options"] == ["preview", "publish"]
    assert inputs["first_release"]["default"] is False
    assert inputs["ringcx_source"]["default"] == "config"
    assert set(inputs) == {"mode", "first_release", "ringcx_source"}, "no rollback mode or other new input in this phase"


def test_runs_are_serialized_and_never_cancelled_midway():
    assert WF["concurrency"] == {"group": "pages-production", "cancel-in-progress": False}
    assert WF["permissions"] == {"contents": "read"}


# ---------- build job: order, verification, outputs ----------

def test_tests_and_config_check_come_before_any_fetch_and_the_build_before_verification():
    assert index(BUILD, "pytest") < index(BUILD, "--check-config") < index(BUILD, "Fetch Google Sheets") < index(BUILD, "pipeline.verify_site")


def test_verification_runs_after_the_build_and_before_anything_is_uploaded():
    v = index(BUILD, "pipeline.verify_site")
    assert v > index(BUILD, "Fetch Google Sheets")
    assert v < index(BUILD, "Site preview")
    assert v < index(BUILD, "upload-pages-artifact")
    uploads = [i for i, s in enumerate(BUILD["steps"]) if "upload-pages-artifact" in (s.get("uses") or "") or s.get("with", {}).get("name") == "site-preview"]
    assert uploads and all(i > v for i in uploads)


def test_verification_cannot_be_skipped_or_made_optional():
    s = step(BUILD, "pipeline.verify_site")
    assert "if" not in s, "default success(): it runs only if the build step succeeded"
    assert "continue-on-error" not in s and s["id"] == "candidate"
    assert "out/site" in s["run"] and "--github-output" in s["run"]
    for text in ("Fetch Google Sheets", "pytest", "--check-config", "upload-pages-artifact"):
        assert "continue-on-error" not in step(BUILD, text), text


def test_the_build_job_exposes_the_candidate_report_as_outputs():
    assert BUILD["outputs"] == {k: "${{ steps.candidate.outputs." + k + " }}" for k in ("refreshed_at", "version", "data_bytes", "rows")}


def test_the_pages_artifact_is_packaged_only_after_success_and_only_when_publishing():
    s = step(BUILD, "upload-pages-artifact")
    assert s["if"] == "success() && inputs.mode == 'publish'"
    assert s["with"]["path"] == "out/site"
    assert index(BUILD, "upload-pages-artifact") > index(BUILD, "pipeline.verify_site")


def test_the_recovery_copy_is_kept_90_days_for_publish_runs_and_14_for_previews():
    s = step(BUILD, "Site preview")
    assert s["if"] == "success()" and s["with"]["name"] == "site-preview" and s["with"]["include-hidden-files"] is True
    assert s["with"]["retention-days"] == "${{ inputs.mode == 'publish' && 90 || 14 }}"


def test_the_validation_report_is_kept_even_when_a_step_fails():
    assert step(BUILD, "Validation report")["if"] == "always()"


# ---------- deploy job: gating, guard, live check ----------

def test_the_deploy_job_runs_only_for_publish_and_only_after_a_successful_build():
    assert DEPLOY["needs"] == "build" and DEPLOY["if"] == "inputs.mode == 'publish'"


def test_the_stale_guard_runs_before_the_deployment_and_uses_the_candidate_timestamp():
    g, d = index(DEPLOY, "pipeline.live_check guard"), index(DEPLOY, "deploy-pages")
    assert index(DEPLOY, "actions/checkout") < g < d
    s = DEPLOY["steps"][g]
    assert "continue-on-error" not in s and "if" not in s
    assert s["env"]["CANDIDATE_REFRESHED_AT"] == "${{ needs.build.outputs.refreshed_at }}"
    assert s["env"]["FIRST_RELEASE"] == "${{ inputs.first_release }}"
    assert '--first-release' in s["run"] and '"$FIRST_RELEASE" = "true"' in s["run"]
    assert "--candidate-refreshed-at" in s["run"]


def test_the_live_check_runs_after_the_deployment_with_the_candidate_values_and_the_12_minute_window():
    v, d = index(DEPLOY, "pipeline.live_check verify"), index(DEPLOY, "deploy-pages")
    assert v > d
    s = DEPLOY["steps"][v]
    assert "continue-on-error" not in s and "if" not in s
    for key, out in (("CANDIDATE_REFRESHED_AT", "refreshed_at"), ("CANDIDATE_VERSION", "version"), ("CANDIDATE_DATA_BYTES", "data_bytes"), ("CANDIDATE_ROWS", "rows")):
        assert s["env"][key] == "${{ needs.build.outputs." + out + " }}"
    assert s["env"]["LIVE_URL"] == "${{ steps.deployment.outputs.page_url }}"
    assert "--deadline=720" in s["run"]
    assert DEPLOY["timeout-minutes"] >= 20, "room for the guard, the deployment and 12 minutes of waiting"


# ---------- how the workflow hands values to the checks (--name=value, never --name value) ----------
# A report version is base64url, so it starts with "-" about one time in 64, and argparse reads "--version -Abc..." as a missing value.
# These tests take the real command lines out of the workflow, fill in the variables the way the shell would, and run the real
# command-line parsers with a dash-first version, so a regression to the space-separated form fails here and not after a deployment.

def commands(step, module):
    """The `python3 -m pipeline.<module>` lines of a step's script, with line continuations joined and comments dropped."""
    lines = [l.strip() for l in step["run"].replace("\\\n", " ").splitlines() if l.strip() and not l.strip().startswith("#")]
    found = [l for l in lines if l.startswith(f"python3 -m pipeline.{module} ")]
    assert len(found) == 1, (module, lines)
    return found[0]


def argv_of(command, env):
    """What the program receives: $NAME replaced by its value, then split with the shell's quoting rules (quotes removed)."""
    expanded = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: env[m.group(1)], command)
    words = shlex.split(expanded)
    assert words[:2] == ["python3", "-m"], words
    return words[3:]


def live_check_step(subcommand):
    return step(DEPLOY, f"pipeline.live_check {subcommand}")


def test_every_value_passed_to_the_checks_is_attached_to_its_option_with_an_equals_sign():
    for s, module in ((step(DEPLOY, "pipeline.run_guard"), "run_guard"), (live_check_step("guard"), "live_check"), (live_check_step("verify"), "live_check")):
        words = shlex.split(commands(s, module))[3:]
        if module == "live_check":
            assert words.pop(0) in ("guard", "verify")
        for w in words:
            assert w == "$FLAG" or w == "--first-release" or re.fullmatch(r"--[a-z][a-z-]*=.+", w), f"{module}: {w!r} is not --name=value"
        assert not any(w.startswith("-") and "=" not in w and w != "--first-release" for w in words)


def test_the_verify_command_of_the_workflow_verifies_a_dash_first_version(monkeypatch, capsys):
    summary, raw, e = dash_first_report(now_iso(1))
    s = live_check_step("verify")
    env = {"LIVE_URL": "https://example.invalid/site/", "CANDIDATE_REFRESHED_AT": e["refreshed_at"], "CANDIDATE_VERSION": e["version"],
           "CANDIDATE_DATA_BYTES": str(e["data_bytes"]), "CANDIDATE_ROWS": str(e["rows"])}
    assert set(env) == set(s["env"]), "the test supplies exactly the variables the step defines"
    argv = argv_of(commands(s, "live_check"), env)
    assert e["version"].startswith("-") and f"--version={e['version']}" in argv
    live = Site()
    live.put(summary=summary, data=raw)
    monkeypatch.setattr(live_check, "http_fetch", live)
    argv = [w for w in argv if not w.startswith(("--deadline=", "--interval="))] + ["--deadline=1", "--interval=0.2"]      # only the waiting is shortened
    assert live_check.main(argv) == 0
    assert "LIVE CHECK PASSED" in capsys.readouterr().out and live.requests
    other = Site()                                      # the dash-first version is the value that is compared
    other.put(*report(e["refreshed_at"], n=4)[:2])
    monkeypatch.setattr(live_check, "http_fetch", other)
    assert live_check.main(argv) == 1


def test_the_old_space_separated_verify_command_fails_to_parse_with_a_dash_first_version(monkeypatch, capsys):
    """The regression this section guards against: the same command with `--version "$CANDIDATE_VERSION"` exits with a usage error."""
    _, _, e = dash_first_report(now_iso(1))
    s = live_check_step("verify")
    env = {"LIVE_URL": "https://example.invalid/site/", "CANDIDATE_REFRESHED_AT": e["refreshed_at"], "CANDIDATE_VERSION": e["version"],
           "CANDIDATE_DATA_BYTES": str(e["data_bytes"]), "CANDIDATE_ROWS": str(e["rows"])}
    old = commands(s, "live_check").replace('--version="$CANDIDATE_VERSION"', '--version "$CANDIDATE_VERSION"')
    assert old != commands(s, "live_check"), "the workflow passes the version as --version=..."
    with pytest.raises(SystemExit) as exc:
        live_check.main(argv_of(old, env))
    assert exc.value.code == 2 and "--version" in capsys.readouterr().err


@pytest.mark.parametrize("first_release", [False, True])
def test_the_cdn_guard_command_of_the_workflow_parses_and_decides(monkeypatch, capsys, first_release):
    s = live_check_step("guard")
    at = now_iso(1)
    env = {"SITE_URL": "https://example.invalid/site/", "CANDIDATE_REFRESHED_AT": at, "FLAG": "--first-release" if first_release else ""}
    assert set(s["env"]) <= set(env) | {"FIRST_RELEASE"}
    monkeypatch.setattr(live_check, "http_fetch", Site())                    # an unreadable live site: refused unless this is the first release
    assert live_check.main(argv_of(commands(s, "live_check"), env)) == (0 if first_release else 1)


def test_the_run_guard_command_of_the_workflow_parses(monkeypatch, capsys):
    from pipeline import run_guard
    s = step(DEPLOY, "pipeline.run_guard")
    env = {"REPO": "owner/repo", "RUN_ID": "123", "GH_TOKEN": "x"}
    assert set(s["env"]) == set(env)
    argv = argv_of(commands(s, "run_guard"), env)
    assert argv == ["--repo=owner/repo", "--run-id=123"]
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert run_guard.main(argv) == 1                         # parsed; stopped at the missing token before any network use
    assert "GH_TOKEN is not set" in capsys.readouterr().out


def test_the_old_smoke_test_that_could_not_tell_stale_from_new_is_gone():
    assert "curl" not in yaml.safe_dump(DEPLOY)
    assert "Check the live page" not in names(DEPLOY)


def test_the_deploy_job_has_only_the_permissions_it_needs():
    """actions: read is the one permission the run-history guard adds (it reads this workflow's runs and jobs); nothing writes."""
    assert DEPLOY["permissions"] == {"actions": "read", "contents": "read", "pages": "write", "id-token": "write"}
    assert DEPLOY["environment"]["name"] == "github-pages"
    assert "actions" not in BUILD["permissions"] and WF["permissions"] == {"contents": "read"}, "no other job or the workflow gains it"


def test_the_run_history_guard_runs_first_with_minimal_wiring_and_no_first_release_exemption():
    rg, cdn, d = index(DEPLOY, "pipeline.run_guard"), index(DEPLOY, "pipeline.live_check guard"), index(DEPLOY, "deploy-pages")
    assert index(DEPLOY, "actions/checkout") < rg < cdn < d, "checkout, then the API guard, then the CDN guard, then the deployment"
    s = DEPLOY["steps"][rg]
    assert "if" not in s and "continue-on-error" not in s
    assert s["env"] == {"GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}", "RUN_ID": "${{ github.run_id }}"}
    assert s["run"].strip() == 'python3 -m pipeline.run_guard --repo="$REPO" --run-id="$RUN_ID"'
    assert "first_release" not in str(s).lower() and "first-release" not in str(s).lower() and "FIRST_RELEASE" not in str(s)
    assert "secrets." not in yaml.safe_dump(DEPLOY), "the job's own token is the only credential the deploy job uses"
    assert index(DEPLOY, "pipeline.live_check guard") > rg, "the CDN guard is still there, after it"


def test_the_names_the_run_history_guard_reads_are_pinned_to_the_workflow():
    from pipeline import run_guard
    assert DEPLOY["name"] == run_guard.DEPLOY_JOB_NAME
    deploy_steps = [s for s in DEPLOY["steps"] if (s.get("uses") or "").startswith("actions/deploy-pages@")]
    assert len(deploy_steps) == 1
    assert deploy_steps[0]["name"] == run_guard.DEPLOY_STEP_NAME and deploy_steps[0]["id"] == "deployment"
    assert [s.get("name") for s in DEPLOY["steps"]].count(run_guard.DEPLOY_STEP_NAME) == 1
    assert run_guard.WORKFLOW_PATH == ".github/workflows/refresh.yml" and (ROOT / run_guard.WORKFLOW_PATH).is_file()
    assert run_guard.WORKFLOW_FILE == Path(run_guard.WORKFLOW_PATH).name
    assert "deploy" in WF["jobs"] and list(WF["jobs"]) == ["build", "deploy"]


def test_nothing_in_the_workflow_deploys_except_the_one_deploy_step():
    text = (WORKFLOWS / "refresh.yml").read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))          # comments may say what a step is not
    assert code.count("actions/deploy-pages") == 1
    assert "rollback" not in code.lower() and "git push" not in code and "gh api" not in code
    assert not any("rollback" in str(n).lower() for j in WF["jobs"].values() for n in names(j))


def test_the_checks_the_workflow_runs_exist_and_use_only_the_standard_library_where_nothing_is_installed():
    allowed = {"argparse", "base64", "datetime", "hashlib", "http", "json", "os", "re", "sys", "time", "urllib", "dataclasses", "typing"}
    for module in ("live_check.py", "run_guard.py"):
        src = (ROOT / "pipeline" / module).read_text(encoding="utf-8")
        imported = {l.split()[1].split(".")[0] for l in src.splitlines() if l.startswith(("import ", "from ")) and not l.startswith("from __future__")}
        assert imported <= allowed, (module, imported)


def test_the_build_timeout_covers_the_google_budget_the_full_suite_and_a_margin():
    """The pipeline may spend its whole Google read budget before it fails or succeeds; the job must not be cut off first.
    Allowance: that budget + 15 min for the full test suite (about 9 min measured on a laptop) + 10 min for install, verification and uploads."""
    from pipeline.sheets import GoogleSheetsSource
    needed_minutes = GoogleSheetsSource.JOB_BUDGET / 60 + 15 + 10
    assert BUILD["timeout-minutes"] >= needed_minutes, f"{BUILD['timeout-minutes']} min is less than the {needed_minutes:.0f} min the steps can use"
    assert BUILD["timeout-minutes"] <= 120, "a hung job must still be cut off in reasonable time"


def test_no_gate_can_be_bypassed_by_a_failure_policy_and_the_deploy_job_needs_a_successful_build():
    for job in (BUILD, DEPLOY):
        assert "continue-on-error" not in job and job.get("strategy", {}).get("fail-fast") is None
    cond = DEPLOY["if"]
    assert DEPLOY["needs"] == "build" and not any(f in cond for f in ("always()", "failure()", "cancelled()")), cond
    gate_steps = ("pytest", "--check-config", "Fetch Google Sheets", "pipeline.verify_site", "upload-pages-artifact")
    for text in gate_steps:
        s = step(BUILD, text)
        assert not any(f in str(s.get("if", "")) for f in ("always()", "failure()", "cancelled()")), text
    # the only build steps allowed to run after a failure are the two report steps that publish nothing
    after_failure = [s.get("name") for s in BUILD["steps"] if any(f in str(s.get("if", "")) for f in ("always()", "failure()", "cancelled()"))]
    assert after_failure == ["Validation report", "Write report to the run summary"]


def test_the_test_workflow_still_runs_the_full_suite():
    wf = yaml.safe_load((WORKFLOWS / "test.yml").read_text(encoding="utf-8"))
    runs = [s.get("run", "") for j in wf["jobs"].values() for s in j["steps"]]
    assert any("pytest" in r and r.rstrip().endswith("pipeline") for r in runs)
