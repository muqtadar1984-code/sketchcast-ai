"""
Quality Checking Control Panel
Main entry point for quality checking, fixing, and monitoring.
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_banner():
    banner = f"""
{Colors.BOLD}{Colors.CYAN}╔══════════════════════════════════════════════════════════════════╗
║         SketchCast AI - Quality Checking Control Panel         ║
╚══════════════════════════════════════════════════════════════════╝{Colors.END}
"""
    print(banner)


def print_menu():
    menu = f"""
{Colors.BOLD}Available Commands:{Colors.END}

  1. {Colors.GREEN}check{Colors.END}      - Run quality checks once
  2. {Colors.GREEN}fix{Colors.END}        - Run checks + auto-fix issues + recheck
  3. {Colors.GREEN}watch{Colors.END}      - Monitor files and auto-run checks on changes
  4. {Colors.GREEN}service{Colors.END}    - Start background service (continuous monitoring)
  5. {Colors.GREEN}report{Colors.END}     - Show latest quality report
  6. {Colors.GREEN}logs{Colors.END}       - View quality check logs
  7. {Colors.GREEN}issues{Colors.END}     - Show current active issues
  8. {Colors.GREEN}clean{Colors.END}      - Clean up quality logs
  9. {Colors.YELLOW}help{Colors.END}      - Show this menu
 10. {Colors.RED}exit{Colors.END}      - Exit

Usage:
  python quality_control.py <command>
  
Examples:
  python quality_control.py check      # Single check
  python quality_control.py fix        # Fix all issues
  python quality_control.py watch      # Watch for changes
  python quality_control.py service    # Start background service
"""
    print(menu)


def run_check(project_root):
    """Run quality checks."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}Starting Quality Check...{Colors.END}\n")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(project_root) / "quality_checker.py")],
            timeout=300
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"{Colors.RED}Quality check timed out!{Colors.END}")
        return False
    except Exception as e:
        print(f"{Colors.RED}Error running quality check: {e}{Colors.END}")
        return False


def run_fix(project_root):
    """Run checks with auto-fix enabled."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}Starting Quality Check with Auto-Fix...{Colors.END}\n")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(project_root) / "quality_checker.py")],
            timeout=300
        )
        if result.returncode == 0:
            print(f"\n{Colors.GREEN}{Colors.BOLD}✓ All checks passed after fixes!{Colors.END}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"{Colors.RED}Quality check timed out!{Colors.END}")
        return False
    except Exception as e:
        print(f"{Colors.RED}Error running quality check: {e}{Colors.END}")
        return False


def run_watch(project_root):
    """Start file watcher."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}Starting File Watcher...{Colors.END}\n")
    try:
        subprocess.run(
            [sys.executable, str(Path(project_root) / "quality_watcher.py"), "--root", str(project_root)]
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Watcher stopped.{Colors.END}")
    except FileNotFoundError:
        print(f"{Colors.RED}watchdog module not installed. Run: pip install watchdog{Colors.END}")
    except Exception as e:
        print(f"{Colors.RED}Error starting watcher: {e}{Colors.END}")


def run_service(project_root):
    """Start background quality check service (long-running)."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}Starting Background Quality Check Service...{Colors.END}\n")
    try:
        subprocess.run(
            [sys.executable, str(Path(project_root) / "background_quality_checker.py"), "--root", str(project_root)]
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Service stopped.{Colors.END}")
    except Exception as e:
        print(f"{Colors.RED}Error starting service: {e}{Colors.END}")


def show_active_issues(project_root):
    """Show currently active issues saved by the background checker."""
    issues_file = Path(project_root) / "quality_logs" / "active_issues.json"
    if not issues_file.exists():
        print(f"{Colors.YELLOW}No active issues found. Run a check first or start the service.{Colors.END}")
        return
    try:
        import json
        with open(issues_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        status = data.get("status", "UNKNOWN")
        total = data.get("total_issues", 0)
        print(f"\n{Colors.BOLD}Active Issues ({status}):{Colors.END} {total} total")
        issues = data.get("issues", {})
        for fp, items in issues.items():
            print(f"\n{Colors.CYAN}{fp}{Colors.END}:")
            for it in items[:10]:
                print(f"  - {it}")

    except Exception as e:
        print(f"{Colors.RED}Failed to read active issues: {e}{Colors.END}")


def show_report(project_root):
    """Show latest quality report."""
    logs_dir = Path(project_root) / "quality_logs"
    if not logs_dir.exists():
        print(f"{Colors.YELLOW}No quality logs found yet. Run 'python quality_control.py check' first.{Colors.END}")
        return
    
    log_files = sorted(logs_dir.glob("quality_check_*.log"))
    if not log_files:
        print(f"{Colors.YELLOW}No quality check logs found.{Colors.END}")
        return
    
    latest_log = log_files[-1]
    print(f"\n{Colors.BOLD}{Colors.CYAN}Latest Quality Report: {latest_log.name}{Colors.END}\n")
    try:
        with open(latest_log, "r") as f:
            content = f.read()
            # Show only the formatted report (last part of file)
            report_start = content.rfind("="*70)
            if report_start != -1:
                print(content[report_start:])
            else:
                print(content)
    except Exception as e:
        print(f"{Colors.RED}Error reading log: {e}{Colors.END}")


def show_logs(project_root):
    """List all quality check logs."""
    logs_dir = Path(project_root) / "quality_logs"
    if not logs_dir.exists():
        print(f"{Colors.YELLOW}No quality logs found yet.{Colors.END}")
        return
    
    log_files = sorted(logs_dir.glob("quality_check_*.log"))
    if not log_files:
        print(f"{Colors.YELLOW}No quality check logs found.{Colors.END}")
        return
    
    print(f"\n{Colors.BOLD}{Colors.CYAN}Quality Check Logs:{Colors.END}\n")
    for i, log_file in enumerate(log_files[-10:], 1):  # Show last 10
        size_kb = log_file.stat().st_size / 1024
        print(f"  {i}. {log_file.name} ({size_kb:.1f} KB)")
    
    print(f"\nLogs directory: {logs_dir}\n")


def clean_logs(project_root):
    """Clean up old quality logs."""
    logs_dir = Path(project_root) / "quality_logs"
    if not logs_dir.exists():
        print(f"{Colors.YELLOW}No quality logs to clean.{Colors.END}")
        return
    
    log_files = sorted(logs_dir.glob("quality_check_*.log"))
    if not log_files:
        print(f"{Colors.YELLOW}No quality check logs to clean.{Colors.END}")
        return
    
    # Keep last 5 logs, delete older ones
    logs_to_delete = log_files[:-5]
    
    if not logs_to_delete:
        print(f"{Colors.GREEN}Only {len(log_files)} logs found. Keeping all.{Colors.END}")
        return
    
    total_size = 0
    for log_file in logs_to_delete:
        total_size += log_file.stat().st_size
        log_file.unlink()
    
    size_mb = total_size / (1024 * 1024)
    print(f"{Colors.GREEN}✓ Deleted {len(logs_to_delete)} old logs ({size_mb:.1f} MB freed){Colors.END}")


def main():
    parser = argparse.ArgumentParser(
        description="SketchCast AI Quality Checking Control Panel",
        add_help=False
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="menu",
        help="Command to run: check, fix, watch, report, logs, clean, help, exit"
    )
    parser.add_argument("--root", type=str, help="Project root directory")
    
    args = parser.parse_args()
    
    project_root = Path(args.root) if args.root else Path(__file__).parent
    
    # Ensure it's a valid project root
    if not (project_root / "requirements.txt").exists():
        print(f"{Colors.RED}Error: requirements.txt not found in {project_root}{Colors.END}")
        print(f"Specify correct root with --root flag")
        sys.exit(1)
    
    print_banner()
    
    command = args.command.lower() if args.command else "help"
    
    if command == "check":
        success = run_check(project_root)
        if success:
            print(f"\n{Colors.GREEN}{Colors.BOLD}✓ All quality checks passed!{Colors.END}\n")
        else:
            print(f"\n{Colors.YELLOW}{Colors.BOLD}⚠ Some issues remain. Run 'python quality_control.py fix' to attempt fixes.{Colors.END}\n")
    
    elif command == "fix":
        success = run_fix(project_root)
        if success:
            print(f"\n{Colors.GREEN}{Colors.BOLD}✓ All quality checks passed after fixes!{Colors.END}\n")
        else:
            print(f"\n{Colors.RED}{Colors.BOLD}✗ Some issues could not be auto-fixed.{Colors.END}\n")
            print(f"Check the logs for details: {project_root}/quality_logs/\n")
    
    elif command == "watch":
        run_watch(project_root)
    
    elif command == "service":
        run_service(project_root)

    elif command == "issues":
        show_active_issues(project_root)
    
    elif command == "report":
        show_report(project_root)
    
    elif command == "logs":
        show_logs(project_root)
    
    elif command == "clean":
        clean_logs(project_root)
    
    elif command == "help" or command == "menu":
        print_menu()
    
    elif command == "exit":
        print(f"{Colors.CYAN}Goodbye!{Colors.END}")
        sys.exit(0)
    
    else:
        print(f"{Colors.RED}Unknown command: {command}{Colors.END}\n")
        print_menu()
        sys.exit(1)


if __name__ == "__main__":
    main()
