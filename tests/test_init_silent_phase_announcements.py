"""Pin init's announce-before-run progress contract.

The authoritative rationale lives in
``.claude/skills/cwcli-lifecycle/references/init.md``. These tests cover event
ordering for ``align_uid`` and ``python_install``, spinner continuity at the
stage boundary, the separate Python install spinner, and verbose ordering.
"""

from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.utils import config_utils, db_utils

from .test_core_init import FakeContainer, bench_kwargs, use_container

PROJECT = "proj"


# ------------------------------------------------------------------ core: announce-before-run


class TestPhaseAnnouncedBeforeItRuns:
    def _patched(self, monkeypatch):
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(config_utils, "add_custom_path", lambda path: True)
        monkeypatch.setattr(core_init.time, "sleep", lambda _s: None)

    def test_align_uid_is_announced_before_the_blocking_call_returns(self, monkeypatch):
        """A fake ``align_container_user_to_host`` that appends to ``order`` from
        INSIDE the call proves the InitStepStart lands before that call even
        starts running - not merely before some later step. The real alignment
        call sits between "announced" and "align_finished"; this fake collapses
        that time but preserves the ordering that matters."""
        self._patched(monkeypatch)
        container = FakeContainer()
        use_container(monkeypatch, container)

        order: list[str] = []

        def fake_align(container, *, chown_home=False):
            order.append("align_running")
            return (True, None)

        monkeypatch.setattr(core_init.core_docker, "align_container_user_to_host", fake_align)

        def on_event(event):
            if isinstance(event, core_init.InitStepStart) and event.phase == "align_uid":
                order.append("announced")
            elif isinstance(event, core_init.InitStepEnd) and event.phase == "align_uid":
                order.append("step_end")

        result = core_init.init_bench(PROJECT, **bench_kwargs(), on_event=on_event)

        assert result.data is not None
        assert order == ["announced", "align_running", "step_end"]

    def test_python_install_is_announced_before_the_blocking_call_returns(self, monkeypatch):
        self._patched(monkeypatch)
        container = FakeContainer(
            exec_run_responses={
                "ls ~/.pyenv/versions": (0, b""),  # nothing installed yet
                "pyenv install --list": (0, b"  3.10.14\n"),
                "ls ~/.nvm/versions/node/": (0, b"v16.20.2"),
            }
        )
        use_container(monkeypatch, container)

        order: list[str] = []
        real_exec_run = container.exec_run

        def recording_exec_run(cmd, **kwargs):
            script = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else ""
            if script == "pyenv install 3.10.14":
                order.append("install_running")
            return real_exec_run(cmd, **kwargs)

        monkeypatch.setattr(container, "exec_run", recording_exec_run)

        def on_event(event):
            if isinstance(event, core_init.InitStepStart) and event.phase == "python_install":
                order.append("announced")
            elif isinstance(event, core_init.InitStepEnd) and event.phase == "python_install":
                order.append("step_end")

        core_init.init_bench(PROJECT, **bench_kwargs(frappe_ref="version-14"), on_event=on_event)

        assert order == ["announced", "install_running", "step_end"]


# ------------------------------------------------------------------ renderer: no dead spinner


class _RecordingSpinner:
    """Mirrors test_init_reuse_bench.py's fake: records live/dead + labels."""

    instances: list["_RecordingSpinner"] = []

    def __init__(self, label, *args, **kwargs):
        self.label = label
        self.active = False
        self.updates: list[str] = []
        type(self).instances.append(self)

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.active = False
        return False

    def update(self, message):
        self.updates.append(message)


class TestNonVerboseSpinnerSurvivesTheStageBoundary:
    def setup_method(self):
        _RecordingSpinner.instances = []

    def test_align_uid_opens_a_spinner_with_no_gap_after_stage_one_closes(self, monkeypatch):
        monkeypatch.setattr(init_mod, "TipSpinner", _RecordingSpinner)
        renderer = init_mod._InitRenderer(verbose=False, show_tips=False, project=PROJECT)
        renderer.bench_name = "frappe-bench"
        renderer.site_name = "development.localhost"

        # Stage 1's last event, then the frontend's close() between the two
        # core calls - reproducing exactly what commands/init.py does today.
        renderer(core_init.InitStepStart(phase="wait_ready", message="Waiting for containers"))
        renderer.close()
        assert _RecordingSpinner.instances[-1].active is False

        # Stage 2's FIRST event must re-open a spinner immediately - there is
        # no event in between during which the terminal would show nothing.
        renderer(
            core_init.InitStepStart(
                phase="align_uid",
                message="Aligning container user to host uid/gid",
            )
        )
        assert _RecordingSpinner.instances[-1].active is True
        assert "Aligning container user" in _RecordingSpinner.instances[-1].updates[-1]

    def test_python_install_keeps_the_spinner_alive_as_its_own_group(self, monkeypatch):
        monkeypatch.setattr(init_mod, "TipSpinner", _RecordingSpinner)
        renderer = init_mod._InitRenderer(verbose=False, show_tips=False, project=PROJECT)
        renderer.bench_name = "frappe-bench"
        renderer.site_name = "development.localhost"

        renderer(core_init.InitStepStart(phase="align_uid", message="Aligning..."))
        renderer(core_init.InitStepEnd(phase="align_uid"))
        renderer(
            core_init.InitStepStart(
                phase="python_install",
                message="Installing Python 3.10 via pyenv (can take several minutes)",
            )
        )

        # A new group opened (not the stale align_uid spinner still showing
        # its old label) and it is alive.
        spinner = _RecordingSpinner.instances[-1]
        assert spinner.active is True
        assert "Installing Python" in spinner.updates[-1]


class TestVerboseAnnouncesBeforeCompletion:
    def test_align_uid_prints_its_start_line_before_the_completion_trace(self, monkeypatch, capsys):
        renderer = init_mod._InitRenderer(verbose=True, show_tips=False, project=PROJECT)
        renderer.bench_name = "frappe-bench"

        renderer(
            core_init.InitStepStart(
                phase="align_uid",
                message="Aligning container user to host uid/gid",
            )
        )
        out_before = capsys.readouterr().err
        assert "Aligning container user to host uid/gid" in out_before

        renderer(core_init.InitStepEnd(phase="align_uid"))
        renderer(
            core_init.InitTrace(text="Aligned the container 'frappe' user to the host uid/gid.")
        )
        out_after = capsys.readouterr().err
        assert "Aligned the container 'frappe' user" in out_after
