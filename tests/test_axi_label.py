"""``cwcli axi benches`` / ``axi label`` / ``axi self-update`` - TOON, exit codes, no prompts.

Thin serializers over the SAME core functions the human CLI calls, so these pin
the frontend contract only: one TOON document on stdout, the status/error ->
exit-code mapping, the bench-discovery answer `axi benches` exists to give, and
the deliberate exit-0-on-outdated divergence of `axi self-update --check`.
"""

from __future__ import annotations

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.label import BenchInfo, BenchList, LabelOutcome
from caffeinated_whale_cli.core.version import VersionInfo

runner = CliRunner()

BENCH_A = "/workspace/frappe-bench"
BENCH_B = "/workspace/frappe-bench-2"


def _bench_list():
    return BenchList(
        project="proj",
        benches=[
            BenchInfo(index=0, path=BENCH_A, label=None),
            BenchInfo(index=1, path=BENCH_B, label="staging"),
        ],
    )


def _label_outcome(*, cleared=False, label="staging"):
    return LabelOutcome(
        project="proj",
        bench_path=BENCH_B,
        label=None if cleared else label,
        previous_label="old" if cleared else None,
        marker_path=f"{BENCH_B}/.cwcli/.bench-label",
        cleared=cleared,
    )


def _version_info(**kw):
    base = dict(
        current="1.2.0",
        latest="1.3.0",
        method="uv",
        upgrade_command=["uv", "tool", "upgrade", "caffeinated-whale-cli"],
        is_outdated=True,
        is_dev=False,
        dev_path=None,
    )
    base.update(kw)
    return VersionInfo(**base)


class TestAxiBenches:
    def test_emits_indices_and_labels_as_toon(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_label,
            "list_benches",
            lambda p: Result(status=Status.OK, data=_bench_list()),
        )
        result = runner.invoke(axi_mod.app, ["benches", "proj"])

        assert result.exit_code == 0
        # The whole point of the verb: the index an agent must pass to --bench,
        # and the label it can pass instead, both readable from the document.
        assert "benches[2]" in result.stdout
        assert "staging" in result.stdout
        assert BENCH_A in result.stdout

    def test_uninspected_project_is_a_structured_error_naming_inspect(self, monkeypatch):
        def _raise(p):
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "benches.none_cached",
                "No cached benches for project 'proj'.",
                hint="Run 'cwcli inspect proj' first.",
            )

        monkeypatch.setattr(axi_mod.core_label, "list_benches", _raise)
        result = runner.invoke(axi_mod.app, ["benches", "proj"])

        assert result.exit_code == 1
        assert "error:" in result.stdout
        assert "help:" in result.stdout
        assert "inspect" in result.stdout


class TestAxiLabel:
    def test_set_emits_outcome(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_label,
            "set_label",
            lambda p, *, bench, label: Result(status=Status.OK, data=_label_outcome()),
        )
        result = runner.invoke(axi_mod.app, ["label", "proj", "--bench", "1", "--set", "staging"])

        assert result.exit_code == 0
        assert "label: staging" in result.stdout
        assert "cleared: false" in result.stdout

    def test_clear_emits_outcome(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_label,
            "clear_label",
            lambda p, *, bench: Result(status=Status.OK, data=_label_outcome(cleared=True)),
        )
        result = runner.invoke(axi_mod.app, ["label", "proj", "--bench", "1", "--clear"])

        assert result.exit_code == 0
        assert "cleared: true" in result.stdout
        assert "previous_label: old" in result.stdout

    def test_neither_set_nor_clear_is_a_usage_error_not_a_listing(self, monkeypatch):
        # Listing is `axi benches`, deliberately NOT a mode of this verb.
        result = runner.invoke(axi_mod.app, ["label", "proj", "--bench", "0"])

        assert result.exit_code == 2
        assert "error:" in result.stdout
        assert "benches" in result.stdout  # points at the discovery verb

    def test_both_set_and_clear_is_a_usage_error(self):
        result = runner.invoke(
            axi_mod.app, ["label", "proj", "--bench", "0", "--set", "x", "--clear"]
        )

        assert result.exit_code == 2
        assert "error:" in result.stdout

    def test_multi_bench_without_selector_is_a_usage_error_listing_benches(self, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'proj' has multiple benches; select one.",
            options=[
                {"value": "0", "label": BENCH_A},
                {"value": "1", "label": f"'staging' {BENCH_B}"},
            ],
        )
        monkeypatch.setattr(
            axi_mod.core_label,
            "set_label",
            lambda p, *, bench, label: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["label", "proj", "--set", "staging"])

        assert result.exit_code == 2
        assert "pass --bench" in result.stdout
        assert "options[2]" in result.stdout

    def test_stopped_container_error_does_not_name_a_flag_label_lacks(self, monkeypatch):
        def _raise(p, *, bench, label):
            raise CwcliError(
                ErrorKind.NOT_RUNNING,
                "container.not_running",
                "Frappe container for project 'proj' is not running.",
                hint="Start the project first - the label marker is stored inside the bench.",
            )

        monkeypatch.setattr(axi_mod.core_label, "set_label", _raise)
        result = runner.invoke(axi_mod.app, ["label", "proj", "--bench", "1", "--set", "x"])

        assert result.exit_code == 1
        assert "--yes" not in result.stdout  # the widened hint, verified on the agent surface
        assert "Start the project first" in result.stdout


class TestAxiSelfUpdateCheck:
    def test_outdated_is_a_successful_read_exit_0(self, monkeypatch):
        # The deliberate divergence from `cwcli self-update --check`'s exit 1: on the
        # agent surface a non-zero exit means an error, and a read verb that
        # successfully answers "you are outdated" has not failed.
        monkeypatch.setattr(
            axi_mod.core_version,
            "check",
            lambda **kw: Result(status=Status.OK, data=_version_info()),
        )
        result = runner.invoke(axi_mod.app, ["self-update", "--check"])

        assert result.exit_code == 0
        assert "is_outdated: true" in result.stdout
        assert "latest: 1.3.0" in result.stdout

    def test_up_to_date_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_version,
            "check",
            lambda **kw: Result(
                status=Status.OK, data=_version_info(latest="1.2.0", is_outdated=False)
            ),
        )
        result = runner.invoke(axi_mod.app, ["self-update", "--check"])

        assert result.exit_code == 0
        assert "is_outdated: false" in result.stdout

    def test_unreachable_pypi_fails_open_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_version,
            "check",
            lambda **kw: Result(
                status=Status.WARNING,
                data=_version_info(latest=None, is_outdated=False),
                warnings=[
                    Message("pypi.unreachable", "Could not reach PyPI to check for updates.")
                ],
            ),
        )
        result = runner.invoke(axi_mod.app, ["self-update", "--check"])

        assert result.exit_code == 0  # a read-only check must not punish a flaky network
        assert "latest: null" in result.stdout
        assert "Could not reach PyPI" in result.stdout

    def test_dev_install_is_reported_not_special_cased(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_version,
            "check",
            lambda **kw: Result(
                status=Status.OK,
                data=_version_info(
                    method="dev", upgrade_command=None, is_dev=True, dev_path="/src/cwcli"
                ),
            ),
        )
        result = runner.invoke(axi_mod.app, ["self-update", "--check"])

        assert result.exit_code == 0
        assert "method: dev" in result.stdout

    def test_check_is_required_so_the_verb_can_never_mutate(self):
        # The mutating form is deliberately deferred; --check is required rather
        # than defaulted so `axi self-update` cannot upgrade by omission.
        result = runner.invoke(axi_mod.app, ["self-update"])
        assert result.exit_code == 2

    def test_no_cache_is_forwarded(self, monkeypatch):
        seen = {}

        def _check(**kw):
            seen.update(kw)
            return Result(status=Status.OK, data=_version_info())

        monkeypatch.setattr(axi_mod.core_version, "check", _check)
        runner.invoke(axi_mod.app, ["self-update", "--check", "--no-cache"])

        assert seen == {"use_cache": False}
