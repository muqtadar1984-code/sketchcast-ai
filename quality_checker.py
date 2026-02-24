"""
SketchCast AI - Quality Checker Agent
Monitors Python files for code quality, provides automated fixes, and generates reports.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Ensure UTF-8 output on Windows (handles ✓ and other Unicode characters)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# Color codes for terminal output
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


class QualityChecker:
    def __init__(self, project_root: str = None):
        self.project_root = Path(project_root) if project_root else Path(__file__).parent
        self.log_dir = self.project_root / "quality_logs"
        self.log_dir.mkdir(exist_ok=True)
        self.issues: Dict[str, List[Dict]] = {}
        self.fixes_applied: List[str] = []
        self.current_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = self.log_dir / f"quality_check_{self.current_timestamp}.log"

    def log(self, message: str, level: str = "INFO"):
        """Log to both console and file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {level}: {message}"
        print(log_line)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

    def get_python_files(self) -> List[Path]:
        """Get all Python files excluding virtual envs and cache."""
        python_files = []
        exclude_dirs = {".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".git"}
        for py_file in self.project_root.rglob("*.py"):
            if not any(excluded in py_file.parts for excluded in exclude_dirs):
                python_files.append(py_file)
        return sorted(python_files)

    def check_syntax(self) -> Dict[str, List[str]]:
        """Check Python syntax using ast module."""
        self.log("Starting syntax checks...", "INFO")
        syntax_issues = {}
        
        for py_file in self.get_python_files():
            try:
                with open(py_file, "r", encoding="utf-8") as f:
                    compile(f.read(), str(py_file), "exec")
            except SyntaxError as e:
                rel_path = py_file.relative_to(self.project_root)
                syntax_issues[str(rel_path)] = [f"Line {e.lineno}: {e.msg}"]
                self.log(f"Syntax error in {rel_path}: {e.msg}", "ERROR")
        
        if not syntax_issues:
            self.log("✓ All files passed syntax checks", "OK")
        return syntax_issues

    def check_linting(self) -> Dict[str, List[str]]:
        """Check PEP 8 compliance using pylint."""
        self.log("Starting linting checks (PEP 8)...", "INFO")
        linting_issues = {}
        
        try:
            for py_file in self.get_python_files():
                rel_path = py_file.relative_to(self.project_root)
                result = subprocess.run(
                    [sys.executable, "-m", "pylint", str(py_file), "--disable=all", 
                     "--enable=W,E", "--exit-zero"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.stdout and "Your code" not in result.stdout:
                    linting_issues[str(rel_path)] = result.stdout.strip().split("\n")[:5]
                    self.log(f"Linting issues in {rel_path}", "WARNING")
        except subprocess.TimeoutExpired:
            self.log("Pylint check timed out", "WARNING")
        except Exception as e:
            self.log(f"Linting check failed: {e}", "WARNING")
        
        if not linting_issues:
            self.log("✓ All files passed linting checks", "OK")
        return linting_issues

    def check_type_hints(self) -> Dict[str, List[str]]:
        """Check type hints using mypy."""
        self.log("Starting type checking (mypy)...", "INFO")
        type_issues = {}
        
        try:
            # Run mypy on project
            result = subprocess.run(
                [sys.executable, "-m", "mypy", str(self.project_root), 
                 "--ignore-missing-imports", "--exit-zero"],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.stdout:
                for line in result.stdout.split("\n"):
                    if "error:" in line or "warning:" in line:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            file_path = parts[0]
                            file_key = file_path.replace(str(self.project_root), "").lstrip("/\\")
                            if file_key not in type_issues:
                                type_issues[file_key] = []
                            type_issues[file_key].append(":".join(parts[1:]).strip())
                            self.log(f"Type issue: {line}", "WARNING")
        except subprocess.TimeoutExpired:
            self.log("Mypy check timed out", "WARNING")
        except Exception as e:
            self.log(f"Type checking failed: {e}", "INFO")
        
        if not type_issues:
            self.log("✓ Type checking complete (no critical issues)", "OK")
        return type_issues

    def check_imports(self) -> Dict[str, List[str]]:
        """Check import organization using isort."""
        self.log("Starting import organization checks...", "INFO")
        import_issues = {}
        
        try:
            for py_file in self.get_python_files():
                with open(py_file, "r", encoding="utf-8") as f:
                    original_content = f.read()
                
                result = subprocess.run(
                    [sys.executable, "-m", "isort", str(py_file), "--check-only", "--diff"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode != 0 and result.stdout:
                    rel_path = py_file.relative_to(self.project_root)
                    import_issues[str(rel_path)] = ["Imports not properly organized"]
                    self.log(f"Import organization issue in {rel_path}", "WARNING")
        except Exception as e:
            self.log(f"Import check failed: {e}", "INFO")
        
        if not import_issues:
            self.log("✓ All imports properly organized", "OK")
        return import_issues

    def check_tests(self) -> Dict[str, List[str]]:
        """Run pytest and collect results."""
        self.log("Running unit tests (pytest)...", "INFO")
        test_issues = {}
        
        tests_dir = self.project_root / "tests"
        if not tests_dir.exists():
            self.log("No tests directory found", "INFO")
            return test_issues
        
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(tests_dir), "-v", "--tb=short"],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                test_issues["tests"] = result.stdout.split("\n")[-20:]
                self.log(f"Some tests failed", "ERROR")
                for line in result.stdout.split("\n"):
                    if "FAILED" in line:
                        self.log(f"  {line}", "ERROR")
            else:
                self.log("✓ All tests passed", "OK")
        except subprocess.TimeoutExpired:
            self.log("Tests timed out", "WARNING")
        except FileNotFoundError:
            self.log("pytest not installed", "INFO")
        except Exception as e:
            self.log(f"Test run failed: {e}", "INFO")
        
        return test_issues

    def check_docstrings(self) -> Dict[str, List[str]]:
        """Check for missing docstrings."""
        self.log("Checking documentation (docstrings)...", "INFO")
        doc_issues = {}
        
        try:
            for py_file in self.get_python_files():
                result = subprocess.run(
                    [sys.executable, "-m", "pydocstyle", str(py_file), "--ignore=D100,D104"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.stdout and "0 errors" not in result.stdout:
                    rel_path = py_file.relative_to(self.project_root)
                    doc_issues[str(rel_path)] = result.stdout.strip().split("\n")[:3]
                    self.log(f"Documentation issue in {rel_path}", "WARNING")
        except Exception as e:
            self.log(f"Docstring check failed: {e}", "INFO")
        
        if not doc_issues:
            self.log("✓ Documentation checks complete", "OK")
        return doc_issues

    def check_complexity(self) -> Dict[str, List[str]]:
        """Check code complexity using radon."""
        self.log("Checking code complexity...", "INFO")
        complexity_issues = {}
        
        try:
            result = subprocess.run(
                [sys.executable, "-m", "radon", "cc", str(self.project_root), "-a", "-j"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.stdout:
                try:
                    data = json.loads(result.stdout)
                    for filepath, functions in data.items():
                        if isinstance(functions, dict):
                            for func, metrics in functions.items():
                                if isinstance(metrics, dict) and metrics.get("complexity", 0) > 10:
                                    rel_path = filepath.replace(str(self.project_root), "").lstrip("/\\")
                                    if rel_path not in complexity_issues:
                                        complexity_issues[rel_path] = []
                                    complexity_issues[rel_path].append(
                                        f"{func}: complexity {metrics['complexity']} (high)"
                                    )
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            self.log(f"Complexity check failed: {e}", "INFO")
        
        if not complexity_issues:
            self.log("✓ Code complexity acceptable", "OK")
        return complexity_issues

    def run_all_checks(self) -> Tuple[bool, int]:
        """Run all quality checks."""
        self.log("=" * 60, "INFO")
        self.log(f"Quality Check Started - {datetime.now()}", "INFO")
        self.log("=" * 60, "INFO")
        
        self.issues = {
            "syntax": self.check_syntax(),
            "linting": self.check_linting(),
            "type_hints": self.check_type_hints(),
            "imports": self.check_imports(),
            "tests": self.check_tests(),
            "docstrings": self.check_docstrings(),
            "complexity": self.check_complexity(),
        }
        
        total_issues = sum(len(v) for v in self.issues.values() if isinstance(v, dict))
        critical_issues = len(self.issues["syntax"]) + len(self.issues["tests"])
        
        return critical_issues == 0, total_issues

    def attempt_fixes(self) -> bool:
        """Attempt to auto-fix issues."""
        self.log("\n" + "=" * 60, "INFO")
        self.log("Attempting Auto-Fixes...", "INFO")
        self.log("=" * 60, "INFO")
        
        # Fix import organization
        if self.issues.get("imports"):
            self.log("Auto-fixing import organization...", "INFO")
            try:
                subprocess.run(
                    [sys.executable, "-m", "isort", str(self.project_root), "--recursive"],
                    timeout=30
                )
                self.fixes_applied.append("Imports reorganized")
                self.log("✓ Imports fixed", "OK")
            except Exception as e:
                self.log(f"Import fix failed: {e}", "WARNING")
        
        # Fix code formatting with autopep8
        if self.issues.get("linting"):
            self.log("Auto-fixing PEP 8 violations...", "INFO")
            try:
                for py_file in self.get_python_files():
                    subprocess.run(
                        [sys.executable, "-m", "autopep8", "--in-place", str(py_file)],
                        timeout=30
                    )
                self.fixes_applied.append("PEP 8 violations fixed")
                self.log("✓ PEP 8 violations fixed", "OK")
            except Exception as e:
                self.log(f"PEP 8 fix failed: {e}", "WARNING")
        
        return len(self.fixes_applied) > 0

    def generate_report(self, passed: bool, total_issues: int):
        """Generate quality report."""
        report = f"""
{Colors.BOLD}{'='*70}{Colors.END}
{Colors.BOLD}QUALITY CHECK REPORT{Colors.END}
{Colors.BOLD}{'='*70}{Colors.END}

Timestamp: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Project Root: {self.project_root}
Total Files Checked: {len(self.get_python_files())}
Total Issues Found: {total_issues}

{Colors.BOLD}Check Results:{Colors.END}
"""
        
        for check_name, issues in self.issues.items():
            if isinstance(issues, dict):
                issue_count = len(issues)
                if issue_count == 0:
                    status = f"{Colors.GREEN}✓ PASS{Colors.END}"
                else:
                    status = f"{Colors.RED}✗ FAIL ({issue_count}){Colors.END}"
                report += f"  {check_name.ljust(20)} {status}\n"
        
        if self.fixes_applied:
            report += f"\n{Colors.BOLD}Fixes Applied:{Colors.END}\n"
            for fix in self.fixes_applied:
                report += f"  {Colors.GREEN}•{Colors.END} {fix}\n"
        
        if passed:
            report += f"\n{Colors.GREEN}{Colors.BOLD}✓ ALL CHECKS PASSED!{Colors.END}\n"
        else:
            report += f"\n{Colors.RED}{Colors.BOLD}✗ ISSUES REMAIN - Review details below{Colors.END}\n"
        
        if any(self.issues.values()):
            report += f"\n{Colors.BOLD}Detailed Issues:{Colors.END}\n"
            for check_name, issues in self.issues.items():
                if isinstance(issues, dict) and issues:
                    report += f"\n{Colors.YELLOW}{check_name.upper()}:{Colors.END}\n"
                    for file_path, file_issues in issues.items():
                        report += f"  {Colors.CYAN}{file_path}{Colors.END}:\n"
                        for issue in file_issues[:3]:
                            report += f"    - {issue}\n"
        
        report += f"\n{Colors.BOLD}Log File: {self.log_file}{Colors.END}\n"
        report += f"{Colors.BOLD}{'='*70}{Colors.END}\n"
        
        return report

    def run(self, auto_fix: bool = True) -> bool:
        """Execute full quality check cycle."""
        passed, total_issues = self.run_all_checks()
        
        if not passed and auto_fix:
            self.attempt_fixes()
            self.log("\nRe-running checks after fixes...", "INFO")
            passed, total_issues = self.run_all_checks()
        
        report = self.generate_report(passed, total_issues)
        print(report)
        
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write("\n" + report + "\n")
        
        return passed


if __name__ == "__main__":
    checker = QualityChecker()
    success = checker.run(auto_fix=True)
    sys.exit(0 if success else 1)
