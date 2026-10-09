"""Pins for the files that decide WHEN and HOW the daily scan runs. ASCII only.

A workflow is code that no unit test imports: a changed mode, a dropped
timezone, an earlier first attempt or a test step that cannot fail would not
show until a scan ran against live data. These are static reads of the files
as committed; they need no network and no runner.

    python -m unittest tests.test_pin_workflows -v
"""
import re
import unittest

from tests.pin_support import read_text


def _flat(rel):
    return read_text(rel).replace("\r\n", "\n")


def _live_lines(text):
    """Lines that are not whole-line comments."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _crons(text):
    return re.findall(r'^\s*-\s*cron:\s*"([^"]+)"', text, re.M)


class ScanWorkflow(unittest.TestCase):
    def setUp(self):
        self.yml = _flat(".github/workflows/scan.yml")
        self.live = "\n".join(_live_lines(self.yml))

    def test_it_scans_the_prelaunch_mode_and_nothing_else(self):
        calls = re.findall(r"python scan_headless\.py\s+(\S+)", self.live)
        self.assertEqual(calls, ["mode_prelaunch"])

    def test_the_scan_job_runs_on_taipei_time(self):
        job_env = self.live.split("jobs:", 1)[1].split("steps:", 1)[0]
        self.assertRegex(job_env, r"(?m)^\s+TZ:\s*Asia/Taipei\s*$")

    def test_the_fallback_crons_are_the_three_weekday_runs(self):
        self.assertEqual(_crons(self.live),
                         ["30 6 * * 1-5", "0 9 * * 1-5", "0 10 * * 1-5"])

    def test_exit_codes_map_to_what_the_pages_job_and_the_commit_step_read(self):
        case = self.yml.split('case "$code" in', 1)[1].split("esac", 1)[0]

        def arm(label):
            return case.split("\n            %s)" % label, 1)[1].split(";;", 1)[0]

        zero, one, three, four = arm("0"), arm("1"), arm("3"), arm("4")
        self.assertIn('published=true', zero)
        self.assertNotIn("frozen", zero)
        self.assertIn('published=false', one)
        self.assertNotIn("frozen", one)
        self.assertIn('published=false', three)      # frozen: nothing to republish
        self.assertIn('frozen=true', three)
        self.assertNotIn("amended", three)
        self.assertIn('published=true', four)        # amended: the new payload goes out
        self.assertIn('frozen=true', four)
        self.assertIn('amended=true', four)
        # only an unexpected code is a red build
        fallback = case.split("*)", 1)[1]
        self.assertIn('exit "$code"', fallback)
        for label in ("0", "1", "3", "4"):
            self.assertNotIn('exit "$code"', arm(label))

    def test_a_run_with_no_payload_never_uploads_pages_or_runs_the_checks(self):
        for name in ("Self-check every published column",
                     "Upload Pages artifact"):
            step = self.yml.split("- name: " + name, 1)[1].split("\n      - name:", 1)[0]
            self.assertIn("if: steps.scan.outputs.published == 'true'", step, name)

    def test_the_state_commit_is_unconditional_and_the_freeze_gate_has_a_site(self):
        step = self.yml.split("- name: Commit durable state", 1)[1].split("run: |", 1)[0]
        self.assertNotIn("if:", step)
        self.assertIn("YENTOOL_FORCE_RESCAN: ${{ inputs.force_rescan && '1' || '' }}", self.yml)
        self.assertIn("PAGES_URL: https://${{ github.repository_owner }}.github.io/", self.yml)

    def test_deps_install_from_the_pinned_resolution(self):
        self.assertIn("pip install -r requirements.txt -c constraints.txt", self.live)


class ScanTimerScript(unittest.TestCase):
    def setUp(self):
        self.sh = _flat(".github/scripts/scan_timer.sh")

    def grab(self, name):
        m = re.search(r'(?m)^%s=("?)([^"\s#]+)\1' % name, self.sh)
        self.assertIsNotNone(m, name)
        return m.group(2)

    def test_the_first_attempt_is_the_moment_the_list_may_become_final(self):
        from scanner import list_freeze
        self.assertEqual(self.grab("FIRST_ATTEMPT"), "15:00")
        self.assertEqual(list_freeze.FINAL_NOT_BEFORE, "15:00")

    def test_the_last_attempt_and_the_retry_spacing(self):
        self.assertEqual(self.grab("LAST_ATTEMPT"), "19:30")
        self.assertEqual(self.grab("RETRY_MIN"), "75")
        # 15:00, 16:15, 17:30, 18:45 are all before 19:30; 20:00 is not
        first = 15 * 60
        runs = []
        t = first
        while t <= 19 * 60 + 30:
            runs.append(t)
            t += 75
        self.assertEqual(runs, [900, 975, 1050, 1125])

    def test_the_job_hops_before_the_runner_limit(self):
        self.assertEqual(self.grab("JOB_BUDGET_MIN"), "340")

    def test_it_reads_the_published_list_state_not_a_guess(self):
        self.assertIn(".meta.list_status.state", self.sh)
        self.assertIn('"$list_state" == "final"', self.sh)


class ScanTimerWorkflow(unittest.TestCase):
    def setUp(self):
        self.yml = _flat(".github/workflows/scan-timer.yml")
        self.live = "\n".join(_live_lines(self.yml))

    def test_two_early_weekday_crons(self):
        self.assertEqual(_crons(self.live), ["0 22 * * 0-4", "0 2 * * 1-5"])

    def test_the_timer_runs_on_taipei_time_and_calls_the_script(self):
        self.assertRegex(self.live, r"(?m)^\s+TZ:\s*Asia/Taipei\s*$")
        self.assertIn("run: bash .github/scripts/scan_timer.sh", self.live)

    def test_least_privilege_and_one_timer_at_a_time(self):
        perms = self.live.split("permissions:", 1)[1].split("\n\n", 1)[0]
        self.assertRegex(perms, r"(?m)^\s+actions:\s*write")
        self.assertRegex(perms, r"(?m)^\s+contents:\s*read")
        self.assertNotRegex(perms, r"contents:\s*write")
        self.assertRegex(self.live, r"(?m)^\s+group:\s*scan-timer\s*$")
        self.assertRegex(self.live, r"(?m)^\s+cancel-in-progress:\s*false\s*$")


class TestsWorkflow(unittest.TestCase):
    def setUp(self):
        self.yml = _flat(".github/workflows/tests.yml")
        self.live = "\n".join(_live_lines(self.yml))

    def test_the_unit_test_step_runs_the_whole_suite_and_can_fail(self):
        lines = [ln.strip() for ln in self.live.splitlines()]
        self.assertIn("run: python -m unittest discover -s tests -t . -v", lines)

    def test_no_step_swallows_a_failure(self):
        self.assertNotIn("|| true", self.live)
        self.assertNotIn("||true", self.live)
        self.assertNotIn("continue-on-error", self.live)
        self.assertNotIn("set +e", self.live)

    def test_it_runs_on_every_push_to_main_and_every_pull_request(self):
        on = self.live.split("on:", 1)[1].split("concurrency:", 1)[0]
        self.assertIn("branches: [main]", on)
        self.assertIn("pull_request:", on)

    def test_the_dependency_tree_and_every_module_are_checked(self):
        self.assertIn("run: python -m pip check", self.live)
        self.assertIn("python -m compileall -q", self.live)
        self.assertIn("pip install -r requirements.txt -c constraints.txt", self.live)


class ConstraintsAreExactPins(unittest.TestCase):
    def pins(self):
        out = []
        for ln in _flat("constraints.txt").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
        return out

    def test_every_line_is_an_exact_version(self):
        pins = self.pins()
        self.assertGreaterEqual(len(pins), 20)
        for ln in pins:
            self.assertRegex(ln, r"^[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9_.\-+!]*$", ln)

    def test_every_direct_requirement_has_a_pin(self):
        names = {ln.split("==")[0].lower().replace("_", "-") for ln in self.pins()}
        wanted = []
        for ln in _flat("requirements.txt").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                wanted.append(re.split(r"[<>=!~ ]", ln, maxsplit=1)[0].lower().replace("_", "-"))
        self.assertEqual(len(wanted), 5)
        for w in wanted:
            self.assertIn(w, names, w)

    def test_no_name_is_pinned_twice(self):
        names = [ln.split("==")[0].lower().replace("_", "-") for ln in self.pins()]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
