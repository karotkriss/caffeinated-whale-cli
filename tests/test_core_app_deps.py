"""``core.app_deps``: an app's Frappe range and required apps, planned before it lands.

The planner is driven with injected readers (a remote ref's declarations, a remote's
branches, the catalog's sources), so every rule is exercised without a network or a
container: parsing, bench's range dialect, source and ref choice, ordering, cycles,
and how a plan becomes a refusal, a question or a go-ahead.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import app_deps
from caffeinated_whale_cli.core.app_deps import Declarations, Source
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

# ------------------------------------------------------------------------- parsing


class TestRequiredApps:
    def test_a_commented_line_is_not_a_declaration(self):
        assert app_deps.required_apps('# required_apps = ["toolkit"]\n') == []

    def test_a_multi_line_list_parses(self):
        text = 'app_name = "x"\nrequired_apps = [\n    "frappe/erpnext",\n    "hrms",\n]\n'
        assert app_deps.required_apps(text) == ["frappe/erpnext", "hrms"]

    def test_a_computed_value_is_could_not_read_never_empty(self):
        assert app_deps.required_apps("required_apps = get_required()\n") is None

    def test_the_last_top_level_assignment_wins_as_at_import(self):
        assert app_deps.required_apps('required_apps = ["a"]\nrequired_apps = ["b"]\n') == ["b"]

    def test_an_annotated_assignment_counts(self):
        assert app_deps.required_apps('required_apps: list = ["payments"]\n') == ["payments"]

    def test_an_absent_file_declares_nothing(self):
        assert app_deps.required_apps(None) == []

    def test_a_syntax_error_is_could_not_read(self):
        assert app_deps.required_apps("required_apps = [\n") is None

    def test_hooks_py_is_never_executed(self, tmp_path):
        marker = tmp_path / "ran"
        text = f"open({str(marker)!r}, 'w').write('x')\nrequired_apps = ['a']\n"
        assert app_deps.required_apps(text) == ["a"]
        assert not marker.exists()


class TestEntryForms:
    def test_a_bare_name(self):
        assert app_deps.entry_module("toolkit") == ("toolkit", None)

    def test_org_repo_pins_github(self):
        assert app_deps.entry_module("frappe/payments") == (
            "payments",
            "https://github.com/frappe/payments",
        )

    def test_an_at_branch_is_discarded_as_frappe_does(self):
        assert app_deps.entry_module("hrms@version-15") == ("hrms", None)

    def test_a_url_pins_itself(self):
        url = "https://gitlab.example.com/tools/shop-floor.git"
        assert app_deps.entry_module(url) == ("shop_floor", url)


class TestDeclarations:
    def test_the_range_table_and_the_static_version(self):
        pyproject = (
            '[project]\nname = "booking"\nversion = "1.2.0"\n'
            '[tool.bench.frappe-dependencies]\nfrappe = ">=15.0.0,<16.0.0"\n'
            'toolkit = ">=0.22.0"\n'
        )
        decl = app_deps.declarations_from("booking", pyproject=pyproject, hooks="", init="")
        assert decl.ranges == {"frappe": ">=15.0.0,<16.0.0", "toolkit": ">=0.22.0"}
        assert decl.version == "1.2.0" and decl.readable

    def test_a_dynamic_version_reads_dunder_version(self):
        pyproject = '[project]\nname = "frappe"\ndynamic = ["version"]\n'
        decl = app_deps.declarations_from(
            "frappe", pyproject=pyproject, hooks="", init='__version__ = "16.54.0"\n'
        )
        assert decl.version == "16.54.0"

    def test_an_unparseable_pyproject_is_an_error_not_an_empty_table(self):
        decl = app_deps.declarations_from("x", pyproject="[project\n", hooks="", init="")
        assert not decl.readable and "pyproject" in decl.error


class TestRangeDialect:
    @pytest.mark.parametrize(
        ("spec", "version", "ok"),
        [
            (">=15.0.0,<16.0.0", "15.125.0", True),
            (">=15.0.0,<16.0.0", "16.0.0", False),
            (">=16.0.0", "16.0.0-dev", False),  # a pre-release sorts below its release
            ("<17.0.0", "17.0.0-dev", False),
            (">=17.0.0-dev,<18.0.0", "17.0.0-dev", True),
            (">=0.22.0,<1.0.0", "0.8.1", False),
        ],
    )
    def test_bench_semantics(self, spec, version, ok):
        assert app_deps.simple_spec(spec, version) is ok

    def test_a_space_separated_range_does_not_parse_as_bench_s_own_does_not(self):
        assert not app_deps.range_parses(">=15.0.0 <16.0.0")
        assert app_deps.range_parses(">=15.0.0,<16.0.0")


# ------------------------------------------------------------------------ planning


def _bench(**apps) -> app_deps.BenchState:
    """A bench holding ``apps`` (name -> Declarations) with sites ``a.localhost`` and
    ``b.localhost`` that have frappe alone installed."""
    decls = {"frappe": Declarations(app="frappe", version="15.125.0")}
    decls.update(apps)
    return app_deps.BenchState(
        bench_path="/workspace/frappe-bench",
        apps=decls,
        sites=["a.localhost"],
        installed={"a.localhost": {"frappe"}},
        frappe_version="15.125.0",
    )


class _World:
    """Remote repositories: url -> {ref: Declarations}; the catalog: name -> [Source]."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str | None, Declarations]] = {}
        self.catalog: dict[str, list[Source]] = {}
        self.reads: list[tuple[str, str | None]] = []

    def repo(self, url, module, *, branches, default, **by_ref):
        self.repos[url] = {}
        for ref in branches:
            spec = by_ref.get(ref, {})
            self.repos[url][ref] = Declarations(
                app=module,
                version=spec.get("version", "1.0.0"),
                ranges=spec.get("ranges", {}),
                required=spec.get("required", []),
                branch=ref,
            )
        self.repos[url][None] = self.repos[url][default]
        self.catalog.setdefault(module, []).append(
            Source(url=url, key=url.split("://", 1)[1].lower(), default_branch=default)
        )

    def read(self, url, ref):
        self.reads.append((url, ref))
        found = self.repos.get(url, {}).get(ref)
        return found or Declarations(app=url.rsplit("/", 1)[-1], branch=ref, error="no such ref")

    def branches(self, url):
        return [r for r in self.repos.get(url, {}) if r is not None]

    def sources(self, module, pin):
        found = self.catalog.get(module, [])
        if pin:
            found = [s for s in found if s.url == pin]
        return found

    def plan(self, state, targets, **kw):
        return app_deps.build_plan(
            "proj",
            state,
            targets,
            reader=self.read,
            brancher=self.branches,
            sourcer=self.sources,
            **kw,
        )


BOOKING = "https://gitlab.example.com/team/booking"
TOOLKIT = "https://gitlab.example.com/tools/toolkit"


def _booking_world() -> _World:
    world = _World()
    world.repo(
        BOOKING,
        "booking",
        branches=["master", "develop"],
        default="master",
        master={
            "ranges": {"frappe": ">=15.0.0,<16.0.0"},
            "required": ["toolkit"],
        },
    )
    world.repo(
        TOOLKIT,
        "toolkit",
        branches=["master", "develop", "version-16"],
        default="develop",
        master={"version": "0.23.3", "ranges": {"frappe": ">=15.0.0,<17.0.0"}},
        develop={"version": "0.23.3", "ranges": {"frappe": ">=15.0.0,<17.0.0"}},
        **{"version-16": {"version": "0.8.1"}},
    )
    return world


class TestPlan:
    def test_a_missing_required_app_is_planned_first_and_needs_consent(self):
        world = _booking_world()
        plan = world.plan(_bench(), [(BOOKING, "master")])
        assert plan.order == ["toolkit", "booking"]
        dep, app = plan.nodes
        assert dep.action == "fetch+install" and dep.parent == "booking" and not dep.requested
        assert dep.ref == "master"  # the same branch name as the app being installed
        assert app.action == "fetch+install" and app.requested
        assert plan.needs_consent and plan.ok and not plan.blocked
        assert [p.code for p in plan.problems] == ["app.required_app_missing"]

    def test_the_ref_falls_back_to_version_n_then_the_default_branch(self):
        world = _booking_world()
        state = _bench()
        state = app_deps.BenchState(
            bench_path=state.bench_path,
            apps={"frappe": Declarations(app="frappe", version="16.54.0")},
            sites=state.sites,
            installed=state.installed,
            frappe_version="16.54.0",
        )
        world.repos[BOOKING]["feature-x"] = Declarations(
            app="booking", required=["toolkit"], branch="feature-x"
        )
        plan = world.plan(state, [(BOOKING, "feature-x")])
        assert plan.nodes[0].ref == "version-16"  # no feature-x there; version-16 is
        world.repos[TOOLKIT].pop("version-16")
        plan = world.plan(state, [(BOOKING, "feature-x")])
        assert plan.nodes[0].ref == "develop"  # its default branch

    def test_a_candidate_ref_whose_own_range_fails_is_passed_over(self):
        world = _booking_world()
        world.repos[TOOLKIT]["master"] = Declarations(
            app="toolkit", version="9.0.0", ranges={"frappe": ">=16.0.0"}, branch="master"
        )
        plan = world.plan(_bench(), [(BOOKING, "master")])
        assert plan.nodes[0].ref == "develop"

    def test_the_ref_also_fits_the_range_the_requiring_app_declares(self):
        world = _booking_world()
        world.repos[BOOKING]["master"] = Declarations(
            app="booking",
            ranges={"toolkit": "<0.20.0"},
            required=["toolkit"],
            branch="master",
        )
        world.repos[TOOLKIT]["version-15"] = Declarations(
            app="toolkit", version="0.8.1", branch="version-15"
        )
        plan = world.plan(_bench(), [(BOOKING, "master")])
        assert plan.nodes[0].ref == "version-15" and plan.ok  # 0.23.3 on master would not fit

    def test_a_planned_dependency_that_fits_no_branch_says_so_not_on_this_bench(self):
        world = _booking_world()
        world.repos[BOOKING]["master"] = Declarations(
            app="booking",
            ranges={"toolkit": ">=5.0.0"},
            required=["toolkit"],
            branch="master",
        )
        plan = world.plan(_bench(), [(BOOKING, "master")])
        (blocker,) = [p for p in plan.problems if p.severity == "blocker"]
        assert "no branch of toolkit" in blocker.message
        assert "on this bench" not in blocker.message

    def test_a_frappe_range_that_does_not_fit_blocks_and_is_overridable(self):
        world = _booking_world()
        plan = world.plan(_bench(), [(BOOKING, "develop")])  # develop declares nothing
        assert plan.ok
        world.repos[BOOKING]["develop"] = Declarations(
            app="booking", ranges={"frappe": ">=16.0.0,<17.0.0"}, branch="develop"
        )
        plan = world.plan(_bench(), [(BOOKING, "develop")])
        (blocker,) = [p for p in plan.problems if p.severity == "blocker"]
        assert blocker.code == "app.frappe_range_mismatch" and blocker.overridable
        assert plan.blocked and not plan.ok
        overridden = world.plan(_bench(), [(BOOKING, "develop")], ignore_frappe_range=True)
        assert overridden.ok
        assert [p.severity for p in overridden.problems] == ["notice"]

    def test_a_sibling_app_range_blocks_and_is_never_overridable(self):
        world = _World()
        world.repo(
            "https://gitlab.example.com/x/audit",
            "audit",
            branches=["develop"],
            default="develop",
            develop={"ranges": {"toolkit": ">=0.22.0,<1.0.0"}},
        )
        bench = _bench(toolkit=Declarations(app="toolkit", version="0.8.1"))
        plan = world.plan(
            bench, [("https://gitlab.example.com/x/audit", "develop")], ignore_frappe_range=True
        )
        (blocker,) = [p for p in plan.problems if p.severity == "blocker"]
        assert blocker.code == "app.dependency_range_mismatch" and not blocker.overridable

    def test_a_required_app_the_catalog_does_not_know_blocks(self):
        world = _booking_world()
        world.catalog.clear()
        plan = world.plan(_bench(), [(BOOKING, "master")])
        (blocker,) = [p for p in plan.problems if p.severity == "blocker"]
        assert blocker.code == "app.required_app_source_unknown"
        assert "not found in your app sources" in blocker.message
        assert "apps catalog" in blocker.remedy and "--fetch-only" in blocker.remedy

    def test_several_sources_are_a_choice_with_the_fitting_one_preselected(self):
        world = _booking_world()
        fork = "https://github.com/someone/toolkit"
        world.repo(
            fork,
            "toolkit",
            branches=["main"],
            default="main",
            main={"ranges": {"frappe": ">=16.0.0"}},
        )
        plan = world.plan(_bench(), [(BOOKING, "master")])
        (choice,) = [p for p in plan.problems if p.severity == "choice"]
        assert choice.default == TOOLKIT
        assert {o["value"]: o["ok"] for o in choice.options} == {TOOLKIT: True, fork: False}
        assert plan.needs_choice
        picked = world.plan(_bench(), [(BOOKING, "master")], dep_sources={"toolkit": fork})
        assert not picked.needs_choice and picked.nodes[0].target == fork
        # The pick stays visible (with its alternatives) so it can be changed.
        assert any(p.code == "app.required_app_source_chosen" for p in picked.problems)

    def test_a_named_ref_on_an_ambiguous_name_is_checked_at_that_ref(self):
        world = _booking_world()
        world.repo(
            "https://github.com/someone/toolkit", "toolkit", branches=["main"], default="main"
        )
        world.repos[TOOLKIT]["v2.0.0"] = Declarations(
            app="toolkit", version="2.0.0", ranges={"frappe": ">=16.0.0"}, branch="v2.0.0"
        )
        branch_names = world.branches
        world.branches = lambda url: [b for b in branch_names(url) if b != "v2.0.0"]
        plan = world.plan(_bench(), [("toolkit", "v2.0.0")], dep_sources={})
        node = plan.nodes[0]
        assert node.ref == "v2.0.0" and node.version == "2.0.0"
        assert "app.frappe_range_mismatch" in [p.code for p in plan.problems]

    def test_the_source_choice_for_a_named_ref_is_offered_at_that_ref(self):
        world = _booking_world()
        fork = "https://github.com/someone/toolkit"
        world.repo(fork, "toolkit", branches=["main"], default="main")
        world.catalog["toolkit"].reverse()  # the fork first: it fits at its own branch
        world.repos[TOOLKIT]["v2.0.0"] = Declarations(
            app="toolkit", version="2.0.0", branch="v2.0.0"
        )
        plan = world.plan(_bench(), [("toolkit", "v2.0.0")])
        (choice,) = [p for p in plan.problems if p.severity == "choice"]
        assert {o["value"]: o["ok"] for o in choice.options} == {fork: False, TOOLKIT: True}
        assert all(" at v2.0.0" in o["label"] for o in choice.options)
        assert choice.default == TOOLKIT and plan.nodes[0].target == TOOLKIT

    def test_a_required_app_on_the_bench_but_not_on_the_site_is_installed_first(self):
        world = _booking_world()
        bench = _bench(toolkit=Declarations(app="toolkit", version="0.23.3"))
        plan = world.plan(bench, [(BOOKING, "master")])
        dep = plan.nodes[0]
        assert dep.action == "install" and dep.on_bench and dep.missing_sites == ["a.localhost"]
        assert [p.code for p in plan.problems] == ["app.required_app_not_installed"]
        assert world.reads.count((TOOLKIT, "master")) == 0  # the bench copy is read

    def test_an_installed_required_app_is_never_touched(self):
        world = _booking_world()
        bench = _bench(toolkit=Declarations(app="toolkit", version="0.23.3"))
        bench.installed["a.localhost"].add("toolkit")
        plan = world.plan(bench, [(BOOKING, "master")])
        assert plan.nodes[0].action == "none" and not plan.needs_consent

    def test_a_cycle_refuses(self):
        world = _World()
        world.repo(
            "https://example.com/a/one",
            "one",
            branches=["main"],
            default="main",
            main={"required": ["two"]},
        )
        world.repo(
            "https://example.com/a/two",
            "two",
            branches=["main"],
            default="main",
            main={"required": ["one"]},
        )
        plan = world.plan(_bench(), [("https://example.com/a/one", "main")])
        assert "app.required_apps_cycle" in [p.code for p in plan.problems]
        assert plan.blocked

    def test_a_shared_required_app_is_planned_once_depth_first(self):
        world = _World()
        for name, required in (("one", ["two", "three"]), ("two", ["three"]), ("three", [])):
            world.repo(
                f"https://example.com/a/{name}",
                name,
                branches=["main"],
                default="main",
                main={"required": required},
            )
        plan = world.plan(_bench(), [("https://example.com/a/one", "main")])
        assert plan.order == ["three", "two", "one"]
        assert plan.ok and not plan.needs_choice

    def test_a_remote_that_cannot_be_read_is_a_notice_never_a_pass_or_a_block(self):
        world = _World()
        plan = world.plan(_bench(), [("https://example.com/a/gone", "main")])
        assert plan.ok and not plan.needs_consent
        assert [p.code for p in plan.problems] == ["app.declarations_unverified"]
        assert not plan.nodes[0].readable

    def test_an_app_declaring_nothing_gets_no_constraint(self):
        world = _World()
        world.repo("https://example.com/a/plain", "plain", branches=["main"], default="main")
        plan = world.plan(_bench(), [("https://example.com/a/plain", "main")])
        assert plan.ok and plan.problems == [] and plan.nodes[0].ranges == []

    def test_a_requested_app_already_on_the_bench_is_read_from_the_bench_copy(self):
        world = _booking_world()
        bench = _bench(
            booking=Declarations(
                app="booking",
                version="1.0.0",
                remotes=["gitlab.example.com/team/booking"],
            )
        )
        plan = world.plan(bench, [(BOOKING, "master")])
        assert world.reads == [] and plan.order == ["booking"]
        assert plan.nodes[0].action == "install"

    def test_a_new_bench_preview_plans_against_frappe_n_x(self):
        world = _booking_world()
        state = app_deps.planned_bench_state(15, with_erpnext=False, site="x.localhost")
        plan = world.plan(state, [(BOOKING, "master")])
        assert plan.ok and plan.order == ["toolkit", "booking"]
        state16 = app_deps.planned_bench_state(16, with_erpnext=False, site="x.localhost")
        plan = world.plan(state16, [(BOOKING, "master")])
        assert plan.blocked  # booking master needs Frappe 15


# ----------------------------------------------------------------------- enforcing


class TestEnforce:
    def test_a_blocker_raises_with_the_plan_and_nothing_else_happens(self):
        world = _booking_world()
        world.catalog.clear()
        plan = world.plan(_bench(), [(BOOKING, "master")])
        with pytest.raises(CwcliError) as raised:
            app_deps.enforce(plan, with_deps=True)
        assert raised.value.kind is ErrorKind.PRECONDITION
        assert raised.value.code == "app.required_app_source_unknown"
        assert raised.value.detail["plan"]["order"] == ["booking"]
        assert "\n" not in raised.value.message  # axi renders it on one TOON line

    def test_a_range_mismatch_is_a_conflict(self):
        world = _booking_world()
        world.repos[BOOKING]["master"] = Declarations(
            app="booking", ranges={"frappe": ">=16.0.0"}, branch="master"
        )
        with pytest.raises(CwcliError) as raised:
            app_deps.enforce(world.plan(_bench(), [(BOOKING, "master")]), with_deps=True)
        assert raised.value.kind is ErrorKind.CONFLICT
        assert "--ignore-frappe-range" in raised.value.hint

    def test_missing_required_apps_ask_unless_with_deps(self):
        plan = _booking_world().plan(_bench(), [(BOOKING, "master")])
        asked = app_deps.enforce(plan, with_deps=False)
        assert asked is not None and asked.status is Status.NEEDS_CHOICE
        assert asked.choice.kind == "confirm_required_apps" and asked.choice.param == "with_deps"
        assert [o["app"] for o in asked.choice.options] == ["toolkit", "booking"]
        assert app_deps.enforce(plan, with_deps=True) is None

    def test_an_open_source_pick_is_a_select_choice_even_with_consent(self):
        world = _booking_world()
        world.repo(
            "https://github.com/someone/toolkit", "toolkit", branches=["main"], default="main"
        )
        asked = app_deps.enforce(world.plan(_bench(), [(BOOKING, "master")]), with_deps=True)
        assert asked.choice.kind == "select_app_source" and asked.choice.param == "dep_sources"
        assert asked.choice.default == TOOLKIT


class TestChangedSincePlan:
    def test_a_new_required_app_after_the_check_is_named(self):
        node = _booking_world().plan(_bench(), [(BOOKING, "master")]).nodes[1]
        now = Declarations(
            app="booking",
            ranges={"frappe": ">=15.0.0,<16.0.0"},
            required=["toolkit", "payments"],
        )
        assert "payments" in app_deps.changed_since_plan(node, now)

    def test_the_same_declarations_match(self):
        node = _booking_world().plan(_bench(), [(BOOKING, "master")]).nodes[1]
        now = Declarations(
            app="booking", ranges={"frappe": ">=15.0.0,<16.0.0"}, required=["toolkit"]
        )
        assert app_deps.changed_since_plan(node, now) is None


def test_the_module_self_check_holds():
    app_deps._self_check()


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("/tmp/repo", None),
        ("https://example.com/a/plain", "--upload-pack=touch /tmp/owned"),
        ("ext::sh -c touch% /tmp/owned", "main"),
    ],
)
def test_a_remote_read_refuses_a_non_url_or_an_option_ref_without_running_git(
    monkeypatch, url, ref
):
    ran: list = []
    monkeypatch.setattr(app_deps, "_git", lambda args, cwd, timeout=0: ran.append(args))
    decl = app_deps._read_remote(url, ref)
    assert not decl.readable and ran == []


def test_a_pinned_required_app_comes_from_the_catalog_only(monkeypatch):
    catalog = {
        "remote": {
            "gitlab.example.com/tools/toolkit": {
                "name": "toolkit",
                "source": {"url": TOOLKIT, "default_branch": "develop"},
            }
        }
    }
    monkeypatch.setattr(app_deps.app_catalog, "_load_disk", lambda: catalog)
    (found,) = app_deps.find_sources("toolkit")
    assert found.url == TOOLKIT and found.default_branch == "develop"
    assert app_deps.find_sources("toolkit", f"{TOOLKIT}.git") == [found]
    assert app_deps.find_sources("toolkit", "https://github.com/someone/toolkit") == []
