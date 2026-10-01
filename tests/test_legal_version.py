"""버전 문자열이 다시 여러 곳으로 흩어지지 않게 막는 회귀 테스트.

Dockerfile이 0.2.0을 고정해 둔 탓에 0.2.8 컨테이너 빌드가 깨져 있었고, MCP
서버는 클라이언트에 0.2.0을, Windows 설치기는 0.2.0을, 설치 경로 상수는
0.2.1을 쓰고 있었다. 릴리스마다 사람이 네 곳을 같이 고치는 방식은 이미 한 번
실패했으므로, 어긋나면 테스트가 먼저 깨지게 한다.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

from taxax.legal.version import package_version, source_version

_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


class VersionSingleSourceTests(unittest.TestCase):
    def test_source_version_reads_this_checkout(self):
        self.assertEqual(source_version(), _pyproject_version())

    def test_package_version_is_a_real_version_not_a_placeholder(self):
        # 설치본이 있으면 그 버전, 없으면 소스 버전. 어느 쪽이든 "알 수 없음"은
        # 아니어야 한다. 구버전이 설치된 체크아웃에서도 안정적으로 성립한다.
        self.assertRegex(package_version(), r"^\d+\.\d+")

    def test_install_path_constant_follows_package_version(self):
        from taxax.legal import installer

        self.assertEqual(installer.APPLICATION_VERSION, package_version())

    def test_windows_installer_script_uses_the_source_version(self):
        source = (_ROOT / "scripts" / "build_windows_installer.py").read_text(encoding="utf-8")
        self.assertIn("_VERSION = source_version()", source)
        self.assertNotRegex(source, r'_VERSION\s*=\s*"')

    def test_dockerfile_does_not_pin_a_version(self):
        dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
        install_lines = [line for line in dockerfile.splitlines() if "pip install" in line and "taxax-legal-mcp" in line]
        self.assertTrue(install_lines)
        for line in install_lines:
            self.assertNotRegex(line, r"taxax-legal-mcp[=<>!~]", line)

    def test_mcp_server_advertises_the_real_version(self):
        source = (_ROOT / "src" / "taxax" / "mcp" / "server.py").read_text(encoding="utf-8")
        self.assertIn("version=package_version()", source)
        self.assertNotRegex(source, r'version\s*=\s*"\d+\.\d+')


class AclScriptPortabilityTests(unittest.TestCase):
    """Get-Acl 커맨드릿 의존이 되살아나는 것을 막는다.

    이 커맨드릿은 Microsoft.PowerShell.Security 모듈에 있어서 PSModulePath가
    바뀐 환경에서는 로딩에 실패하고, 그러면 설치·등록·secret 접근이 전부
    막힌다. FileInfo/DirectoryInfo의 GetAccessControl()은 같은 일을 하면서
    모듈을 요구하지 않는다.
    """

    def test_local_config_uses_dotnet_acl_accessors_only(self):
        source = (_ROOT / "src" / "taxax" / "legal" / "local_config.py").read_text(encoding="utf-8")
        # 산문에서 이름을 언급하는 것은 괜찮고, 실제 호출 형태만 막는다.
        invocation = re.compile(r"\b(?:Get-Acl|Set-Acl)\s+-[A-Za-z]")
        offenders = [line.strip() for line in source.splitlines() if invocation.search(line)]
        self.assertEqual(offenders, [], "ACL 스크립트는 커맨드릿 대신 .NET 메서드를 써야 합니다.")



class FirstRunGuidanceTests(unittest.TestCase):
    """갓 설치한 사용자가 막히지 않고 다음 단계로 갈 수 있는지 고정한다."""

    def test_module_entry_point_exists_for_broken_path(self):
        # console script가 PATH에 없는 설치본에서 유일하게 통하는 경로.
        entry = _ROOT / "src" / "taxax" / "legal" / "__main__.py"
        self.assertTrue(entry.is_file(), "python -m taxax.legal 진입점이 없습니다.")
        self.assertIn("from .cli import main", entry.read_text(encoding="utf-8"))

    def test_module_invocation_actually_runs(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "taxax.legal", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=120,
            cwd=str(_ROOT),
            env={**os.environ, "PYTHONPATH": str(_ROOT / "src"), "PYTHONUTF8": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr[:400])
        self.assertIn("collect-seeds", result.stdout)

    def test_install_guidance_covers_credential_and_index(self):
        source = (_ROOT / "src" / "taxax" / "legal" / "cli.py").read_text(encoding="utf-8")
        # 인증키 발급처와, 색인을 채워야 한다는 사실을 둘 다 알려야 한다.
        self.assertIn("open.law.go.kr", source)
        self.assertIn("collect-seeds", source)

    def test_guidance_follows_how_the_user_invoked_the_cli(self):
        source = (_ROOT / "src" / "taxax" / "legal" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("def _invocation()", source)
        # next_steps가 실행 방식과 무관하게 taxax-legal을 박아두면 안 된다.
        self.assertNotIn('next_steps.append("법제처 외부 조회를 쓰려면 `taxax-legal', source)

class ConsoleEncodingTests(unittest.TestCase):
    """한글 출력이 주변 코드페이지 때문에 죽지 않는지 고정한다.

    GitHub Actions의 windows runner는 stdout 인코딩이 cp1252였다. 거기에
    `taxax-legal-setup --help`가 한글 설명을 쓰려다 UnicodeEncodeError로
    죽었고, PyInstaller --windowed bootloader가 그 예외를 메시지 상자로 띄운
    채 멈춰 job이 600초 한도에 잘렸다. 콘솔 프로그램이면 죽고 끝이지만
    GUI 실행파일에서는 영구 정지가 된다.
    """

    def test_korean_survives_a_cp1252_stream(self):
        import io

        from taxax.legal.console import prepare_console_streams

        korean = "TAXax Legal MCP Windows 설치 마법사"
        original = sys.stdout
        buffer = io.BytesIO()
        sys.stdout = io.TextIOWrapper(buffer, encoding="cp1252", newline="")
        try:
            with self.assertRaises(UnicodeEncodeError):
                sys.stdout.write(korean)
                sys.stdout.flush()
            prepare_console_streams()
            self.assertEqual(sys.stdout.encoding, "utf-8")
            sys.stdout.write(korean)
            sys.stdout.flush()
        finally:
            try:
                sys.stdout.detach()
            except Exception:
                pass
            sys.stdout = original
        self.assertIn(korean.encode("utf-8"), buffer.getvalue())

    def test_missing_stream_gets_a_sink_instead_of_crashing(self):
        from taxax.legal.console import prepare_console_streams

        original = sys.stdout
        sys.stdout = None
        try:
            prepare_console_streams()
            self.assertIsNotNone(sys.stdout)
            sys.stdout.write("한글")  # 예외가 나지 않아야 한다.
        finally:
            if sys.stdout is not None and sys.stdout is not original:
                sys.stdout.close()
            sys.stdout = original

    def test_both_entry_points_prepare_streams_before_printing(self):
        for relative in ("cli.py", "windows_setup.py"):
            source = (_ROOT / "src" / "taxax" / "legal" / relative).read_text(encoding="utf-8")
            with self.subTest(module=relative):
                self.assertIn("prepare_console_streams()", source)


if __name__ == "__main__":
    unittest.main()
