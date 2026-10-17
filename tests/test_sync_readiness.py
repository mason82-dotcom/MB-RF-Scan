"""Integration-style shell tests for the Pi dashboard readiness polling.

Mocks only systemctl/curl/sleep; no SDR, webserver, or systemd service is touched.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pi/sync-from-github.sh"


class DashboardReadinessTests(unittest.TestCase):
    @staticmethod
    def _shell_function():
        script = SCRIPT.read_text(encoding="utf-8")
        marker = "wait_for_dashboard() {"
        beginning = script.index(marker)
        ending = script.index("\n}\n", beginning) + 2
        return script[beginning:ending]

    def _run_check(self, success_at=1, systemctl_ok=True):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bin_dir = home / "bin"
            bin_dir.mkdir()
            counter = home / "count"
            counter.write_text("0", encoding="ascii")
            (bin_dir / "systemctl").write_text(
                "#!/bin/sh\n"
                '[ "$TEST_SERVICE_OK" = 1 ] && exit 0\n'
                "exit 1\n", encoding="utf-8"
            )
            (bin_dir / "curl").write_text(
                "#!/bin/sh\n"
                'n=$(cat "$TEST_COUNT_FILE")\n'
                'n=$((n + 1))\n'
                'printf "%s" "$n" > "$TEST_COUNT_FILE"\n'
                '[ "$n" -ge "$TEST_SUCCESS_AT" ]\n',
                encoding="utf-8",
            )
            (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            for executable in bin_dir.iterdir():
                executable.chmod(0o755)
            env = os.environ.copy()
            env.update(
                PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                TEST_COUNT_FILE=str(counter),
                TEST_SUCCESS_AT=str(success_at),
                TEST_SERVICE_OK="1" if systemctl_ok else "0",
            )
            result = subprocess.run(
                ["/usr/bin/bash", "-c", self._shell_function() + "\nwait_for_dashboard"],
                env=env, capture_output=True, text=True, timeout=10,
            )
            return result.returncode, int(counter.read_text(encoding="ascii"))

    def test_delayed_webserver_is_accepted(self):
        status, attempts = self._run_check(success_at=3)
        self.assertEqual((status, attempts), (0, 3))

    def test_unavailable_webserver_times_out(self):
        status, attempts = self._run_check(success_at=99)
        self.assertEqual((status, attempts), (1, 20))

    def test_inactive_service_is_not_accepted(self):
        status, attempts = self._run_check(systemctl_ok=False)
        self.assertEqual((status, attempts), (1, 0))


if __name__ == "__main__":
    unittest.main()
