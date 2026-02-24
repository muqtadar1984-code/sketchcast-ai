"""
File watcher for automatic quality checking on code changes.
Monitors Python files and triggers quality checks when changes are detected.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Set

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print("ERROR: watchdog not installed. Run: pip install watchdog")
    sys.exit(1)


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


class PythonFileHandler(FileSystemEventHandler):
    def __init__(self, project_root: str, callback):
        self.project_root = Path(project_root)
        self.callback = callback
        self.cooldown = 3  # seconds to wait after last change before running checks
        self.last_check_time = 0
        self.pending_files: Set[str] = set()
        self.exclude_dirs = {".venv", "venv", "__pycache__", ".git", "node_modules", "quality_logs"}

    def should_process(self, file_path: str) -> bool:
        """Determine if file should be processed."""
        path = Path(file_path)
        
        # Only Python files
        if path.suffix != ".py":
            return False
        
        # Exclude certain directories
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
            print(f"{Colors.YELLOW}[NEW FILE]{Colors.END} {rel_path}")
            self.pending_files.add(str(rel_path))
            self._schedule_check()

    def _schedule_check(self):
        """Schedule a quality check after cooldown."""
        current_time = time.time()
        if current_time - self.last_check_time > self.cooldown:
            self.last_check_time = current_time
            self._run_check()
        else:
            # Re-check after cooldown
            time_until_check = self.cooldown - (current_time - self.last_check_time)
            print(f"{Colors.BLUE}[SCHEDULED]{Colors.END} Quality check in {time_until_check:.1f}s...")

    def _run_check(self):
        """Trigger quality check."""
        if not self.pending_files:
            return
        
        files_str = ", ".join(sorted(self.pending_files))
        print(f"\n{Colors.BOLD}{Colors.CYAN}[DETECTED CHANGES]{Colors.END}")
        print(f"{Colors.CYAN}{files_str}{Colors.END}\n")
        
        self.callback()
        self.pending_files.clear()
        self.last_check_time = time.time()


class QualityCheckWatcher:
    def __init__(self, project_root: str = None):
        self.project_root = Path(project_root) if project_root else Path(__file__).parent
        self.observer = Observer()
        self.handler = PythonFileHandler(str(self.project_root), self._on_file_change)

    def _on_file_change(self):
        """Callback when files change."""
        print(f"{Colors.BOLD}{Colors.YELLOW}Running quality checks...{Colors.END}\n")
        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            subprocess.run(
                [sys.executable, str(self.project_root / "quality_checker.py")],
                timeout=300,
                env=env,
            )
        except subprocess.TimeoutExpired:
            print(f"{Colors.RED}Quality check timed out!{Colors.END}")
        except Exception as e:
            print(f"{Colors.RED}Quality check failed: {e}{Colors.END}")
        
        print(f"\n{Colors.BLUE}[WATCHING]{Colors.END} Monitoring for changes...\n")

    def start(self):
        """Start monitoring files."""
        print(f"{Colors.BOLD}{Colors.GREEN}{'='*70}{Colors.END}")
        print(f"{Colors.BOLD}{Colors.GREEN}Quality Check Watcher Started{Colors.END}")
        print(f"{Colors.BOLD}{Colors.GREEN}{'='*70}{Colors.END}")
        print(f"Project Root: {self.project_root}")
        print(f"Watching for Python file changes...")
        print(f"Cooldown: {self.handler.cooldown}s")
        print(f"\nPress Ctrl+C to stop.\n")
        
        self.observer.schedule(self.handler, str(self.project_root), recursive=True)
        self.observer.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Stop monitoring files."""
        print(f"\n{Colors.YELLOW}Stopping watcher...{Colors.END}")
        self.observer.stop()
        self.observer.join()
        print(f"{Colors.GREEN}Watcher stopped.{Colors.END}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Watch Python files and run quality checks")
    parser.add_argument("--root", type=str, help="Project root directory")
    args = parser.parse_args()
    
    watcher = QualityCheckWatcher(args.root)
    watcher.start()
