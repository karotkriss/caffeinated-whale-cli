"""Unit tests for the bench-label model: validation, selector resolution, and the
per-bench marker file I/O (``utils/bench_labels.py``).

These pin the label rules the captain approved:
  - numeric labels are the positional index; user labels may NOT be purely numeric,
  - ``--bench`` resolves a label first, then a numeric index,
  - the marker file round-trips a label as JSON and recovers safely from a
    missing/garbage marker.
"""

import json

from caffeinated_whale_cli.utils import bench_labels
from tests.bench_fakes import MarkerFakeContainer


class TestValidateUserLabel:
    def test_accepts_normal_labels(self):
        for good in ["staging", "prod-2", "v15_test", "site.local", "A1"]:
            assert bench_labels.validate_user_label(good) is None

    def test_rejects_empty(self):
        assert bench_labels.validate_user_label("") is not None

    def test_rejects_purely_numeric(self):
        # Would collide with a bench's numeric index.
        for numeric in ["0", "1", "42"]:
            err = bench_labels.validate_user_label(numeric)
            assert err is not None and "numeric" in err.lower()

    def test_unicode_digit_labels_are_not_numeric(self):
        # str.isdigit() matches Unicode digit-like chars (superscripts, Arabic-Indic,
        # circled, Roman numerals) that int() cannot parse. is_numeric_label must be
        # ASCII-only so these are NOT treated as a numeric index (which would crash
        # resolve_bench on int(selector)). They pass the numeric gate but are then
        # rejected by the charset rule, so they never become a valid user label either.
        for uni in ["²", "٣", "१२", "①", "Ⅻ"]:
            assert bench_labels.is_numeric_label(uni) is False
            assert bench_labels.validate_user_label(uni) is not None

    def test_rejects_bad_charset(self):
        for bad in ["has space", "semi;colon", "slash/es", "quote'd", "star*"]:
            assert bench_labels.validate_user_label(bad) is not None

    def test_rejects_too_long(self):
        assert bench_labels.validate_user_label("a" * 65) is not None


class TestResolveBench:
    def _benches(self):
        return [
            {"path": "/a", "label": None},
            {"path": "/b", "label": "staging"},
            {"path": "/c"},
        ]

    def test_resolve_by_index(self):
        b = self._benches()
        assert bench_labels.resolve_bench(b, "0")["path"] == "/a"
        assert bench_labels.resolve_bench(b, "2")["path"] == "/c"

    def test_resolve_by_label(self):
        assert bench_labels.resolve_bench(self._benches(), "staging")["path"] == "/b"

    def test_label_wins_over_index_namespace(self):
        # A label is matched before any numeric interpretation; indices and labels
        # never overlap because labels can't be numeric.
        benches = [{"path": "/x", "label": "alpha"}, {"path": "/y", "label": "beta"}]
        assert bench_labels.resolve_bench(benches, "beta")["path"] == "/y"

    def test_index_out_of_range_is_none(self):
        assert bench_labels.resolve_bench(self._benches(), "9") is None

    def test_unknown_label_is_none(self):
        assert bench_labels.resolve_bench(self._benches(), "nope") is None

    def test_empty_selector_is_none(self):
        assert bench_labels.resolve_bench(self._benches(), None) is None

    def test_unicode_digit_selector_is_none_not_crash(self):
        # A Unicode digit-like selector must fall through to the no-match path,
        # never reach int() and raise ValueError. This is the CLI crash CodeRabbit
        # flagged: `--bench ²` previously passed the isdigit() gate then blew up.
        for uni in ["²", "٣", "①", "Ⅻ"]:
            assert bench_labels.resolve_bench(self._benches(), uni) is None


class TestFormatBenchList:
    def test_shows_index_label_path(self):
        out = bench_labels.format_bench_list([{"path": "/a"}, {"path": "/b", "label": "staging"}])
        assert "[0]" in out and "/a" in out
        assert "[1]" in out and "staging" in out and "/b" in out


class TestMarkerIO:
    BENCH = "/workspace/frappe-bench"
    MARKER = "/workspace/frappe-bench/.cwcli/.bench-label"

    def test_write_then_read_roundtrip(self):
        c = MarkerFakeContainer(bench_path=self.BENCH)
        assert bench_labels.write_label_marker(c, self.BENCH, "staging") is True
        # The stored bytes are valid JSON with schema + label.
        stored = json.loads(c.fs[self.MARKER].decode())
        assert stored["label"] == "staging"
        assert stored["schema"] == bench_labels.MARKER_SCHEMA
        # And read recovers it.
        assert bench_labels.read_label_marker(c, self.BENCH) == "staging"

    def test_read_absent_marker_is_none(self):
        c = MarkerFakeContainer(bench_path=self.BENCH)
        assert bench_labels.read_label_marker(c, self.BENCH) is None

    def test_read_malformed_marker_is_none(self):
        c = MarkerFakeContainer(bench_path=self.BENCH, fs={self.MARKER: b"not json {["})
        assert bench_labels.read_label_marker(c, self.BENCH) is None

    def test_read_marker_without_label_key_is_none(self):
        c = MarkerFakeContainer(bench_path=self.BENCH, fs={self.MARKER: b'{"schema": 1}'})
        assert bench_labels.read_label_marker(c, self.BENCH) is None

    def test_clear_removes_marker(self):
        c = MarkerFakeContainer(bench_path=self.BENCH)
        bench_labels.write_label_marker(c, self.BENCH, "staging")
        assert self.MARKER in c.fs
        assert bench_labels.clear_label_marker(c, self.BENCH) is True
        assert self.MARKER not in c.fs
        assert bench_labels.read_label_marker(c, self.BENCH) is None

    def test_clear_absent_marker_still_ok(self):
        c = MarkerFakeContainer(bench_path=self.BENCH)
        assert bench_labels.clear_label_marker(c, self.BENCH) is True

    def test_write_survives_exec_exception(self):
        class Boom:
            def exec_run(self, *a, **k):
                raise RuntimeError("docker down")

        assert bench_labels.write_label_marker(Boom(), self.BENCH, "x") is False
        assert bench_labels.read_label_marker(Boom(), self.BENCH) is None
