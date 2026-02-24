"""
Automated Background Quality Checker
Runs quality checks automatically on every file change and reports issues to Claude.
Designed for continuous development without manual intervention.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Ensure UTF-8 and line-buffered output (important when running in background)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print("ERROR: watchdog not installed")
    sys.exit(1)


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


class BackgroundQualityChecker(FileSystemEventHandler):
    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.issues_log = self.project_root / "quality_logs" / "active_issues.json"
        self.exclude_dirs = {".venv", "venv", "__pycache__", ".git", "node_modules", "quality_logs", ".pytest_cache"}
        self.last_check_time = 0
        self.cooldown = 3  # seconds
        self.pending_files = set()
        self.active_issues = {}
        self.load_issues()

    def should_process(self, file_path: str) -> bool:
        """Check if file should be monitored."""
        path = Path(file_path)
        if path.suffix != ".py":
            return False
        if any(excluded in path.parts for excluded in self.exclude_dirs):
            return False
        return True

    def on_modified(self, event):
        if not event.is_directory and self.should_process(event.src_path):
            rel_path = Path(event.src_path).relative_to(self.project_root)
            self.pending_files.add(str(rel_path))
            self._schedule_check()

    def on_created(self, event):
        if not event.is_directory and self.should_process(event.src_path):
            rel_path = Path(event.src_path).relative_to(self.project_root)
            self.pending_files.add(str(rel_path))
            self._schedule_check()

    def _schedule_check(self):
        """Schedule check after cooldown."""
        current_time = time.time()
        if current_time - self.last_check_time > self.cooldown:
            self._run_check()
        self.last_check_time = current_time

    def _run_check(self):
        """Execute quality check and report results."""
        if not self.pending_files:
            return

        print(f"\n{Colors.CYAN}[{datetime.now().strftime('%H:%M:%S')}] Files changed: {len(self.pending_files)}{Colors.END}")
        print(f"Running quality checks...\n")

        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            result = subprocess.run(
                [sys.executable, str(self.project_root / "quality_checker.py")],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

            if result.returncode == 0:
                print(f"{Colors.GREEN}✓ ALL CHECKS PASSED{Colors.END}\n")
                self.active_issues.clear()
                self.save_issues()
            else:
                self._parse_and_report_issues(result.stdout, result.stderr)

        except subprocess.TimeoutExpired:
            print(f"{Colors.RED}⚠ Quality check timed out!{Colors.END}\n")
        except Exception as e:
            print(f"{Colors.RED}Error: {e}{Colors.END}\n")

        self.pending_files.clear()

    def _parse_and_report_issues(self, stdout: str, stderr: str):
        """Parse quality check output and report issues."""
        print(f"{Colors.BOLD}{Colors.RED}Issues Found - Reporting to Claude Code:{Colors.END}\n")

        # Extract issues from output
        output = stdout + stderr
        self.active_issues = self._extract_issues(output)

        # Save to file for Claude to read
        self.save_issues()

        # Print formatted issues for immediate visibility
        self._print_formatted_issues()

    def _extract_issues(self, output: str) -> Dict:
        """Extract issues from quality checker output."""
        issues = {}

        # Parse output for issue lines
        for line in output.split("\n"):
            if "FAILED" in line or "error:" in line or "Error:" in line:
                # Extract file and issue
                parts = line.split(":")
                if len(parts) >= 2:
                    file_path = parts[0].strip()
                    issue_desc = ":".join(parts[1:]).strip()

                    if file_path not in issues:
                        issues[file_path] = []
                    issues[file_path].append(issue_desc)

        return issues

    def _print_formatted_issues(self):
        """Print issues in Claude-readable format."""
        if not self.active_issues:
            return

        print(f"{Colors.BOLD}ISSUES TO FIX:{Colors.END}")
        print("-" * 70)

        issue_count = 0
        for file_path, file_issues in sorted(self.active_issues.items()):
            rel_path = file_path.replace(str(self.project_root), "").lstrip("/\\")
            print(f"\n{Colors.CYAN}{rel_path}{Colors.END}:")
            for issue in file_issues[:5]:  # Show top 5 issues
                issue_count += 1
                print(f"  {Colors.RED}✗{Colors.END} {issue}")

        print(f"\n{Colors.BOLD}Total Issues: {issue_count}{Colors.END}")
        print(f"{Colors.YELLOW}→ Review the issues above and fix them in your code.{Colors.END}")
        print(f"{Colors.YELLOW}→ Save files to trigger re-check automatically.{Colors.END}\n")

    def load_issues(self):
        """Load active issues from log."""
        if self.issues_log.exists():
            try:
                with open(self.issues_log, "r", encoding="utf-8") as f:
                    self.active_issues = json.load(f)
            except Exception:
                self.active_issues = {}

    def save_issues(self):
        """Save active issues to log for Claude reference."""
        self.issues_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.issues_log, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "total_issues": sum(len(v) for v in self.active_issues.values()),
                        "issues": self.active_issues,
                        "status": "PASS" if not self.active_issues else "FAIL"
                    },
                    f,
                    indent=2,
                    ensure_ascii=False
                )
        except Exception as e:
            print(f"Error saving issues log: {e}")


class AutoQualityCheckService:
    """Background service for continuous quality checking."""

    def __init__(self, project_root: str = None):
        self.project_root = Path(project_root) if project_root else Path(__file__).parent
        self.observer = Observer()
        self.checker = BackgroundQualityChecker(str(self.project_root))

    def start(self):
        """Start the background service."""
        print(f"\n{Colors.BOLD}{Colors.GREEN}{'='*70}{Colors.END}")
        print(f"{Colors.BOLD}{Colors.GREEN}AUTOMATED QUALITY CHECK SERVICE STARTED{Colors.END}")
        print(f"{Colors.BOLD}{Colors.GREEN}{'='*70}{Colors.END}")
        print(f"\nProject Root: {self.project_root}")
        print(f"Monitoring for changes...\n")
        print(f"{Colors.CYAN}What happens now:{Colors.END}")
        print(f"  1. Every Python file change detected automatically")
        print(f"  2. Quality checks run (3 second cooldown)")
        print(f"  3. Issues reported in real-time")
        print(f"  4. Issues saved to: quality_logs/active_issues.json")
        print(f"  5. You (Claude) will be notified to fix issues")
        print(f"\nPress Ctrl+C to stop.\n")

        self.observer.schedule(self.checker, str(self.project_root), recursive=True)
        self.observer.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Stop the background service."""
        print(f"\n{Colors.YELLOW}Stopping quality check service...{Colors.END}")
        self.observer.stop()
        self.observer.join()
        print(f"{Colors.GREEN}Service stopped.{Colors.END}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Automated background quality checker")
    parser.add_argument("--root", type=str, help="Project root directory")
    args = parser.parse_args()

    service = AutoQualityCheckService(args.root)
    service.start()
