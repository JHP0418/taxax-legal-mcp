from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

_SECRET_FORMAT = "taxax-legal-secrets-v1"
_MAX_SECRET_FILE_BYTES = 16 * 1024
_MAX_CREDENTIAL_CHARS = 512


class LocalSecretError(ValueError):
    pass


def default_secret_file() -> Path:
    configured = os.environ.get("TAXAX_LEGAL_SECRET_FILE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise LocalSecretError("TAXAX_LEGAL_SECRET_FILE은 절대경로여야 합니다.")
        return path
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data).expanduser() if local_app_data else Path.home() / "AppData" / "Local"
        return base / "TAXax" / "config" / "provider-secrets.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TAXax" / "config" / "provider-secrets.json"
    configured_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(configured_home).expanduser() if configured_home and Path(configured_home).expanduser().is_absolute() else Path.home() / ".config"
    return base / "taxax" / "provider-secrets.json"


def _powershell(script: str, *, environment: dict[str, str]) -> str:
    """ACL 스크립트를 Windows PowerShell로 실행한다.

    스크립트 안에서 Get-Acl/Set-Acl 커맨드릿을 쓰지 않는 이유가 두 가지다.
    Set-Acl은 디렉터리 대상일 때 SeSecurityPrivilege를 요구하는 결함이 있고,
    Get-Acl은 Microsoft.PowerShell.Security 모듈에 들어 있어서 PSModulePath가
    바뀐 환경(GitHub Actions windows runner처럼 pwsh용 경로로 덮어쓴 경우)에서는
    "module could not be loaded"로 실패한다. 둘 다 FileInfo/DirectoryInfo의
    GetAccessControl()/SetAccessControl() 메서드로 대체하면 모듈 로딩도
    특권 요구도 없이 같은 일을 한다.
    """
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        raise LocalSecretError("Windows ACL을 설정할 PowerShell을 찾지 못했습니다.")
    process_environment = dict(os.environ)
    process_environment.update(environment)
    completed = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=process_environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        suffix = f" ({detail[:300]})" if detail else ""
        raise LocalSecretError(f"Windows ACL 설정 또는 검증에 실패했습니다.{suffix}")
    return completed.stdout.strip().lstrip("﻿")


def restrict_directory_to_current_user(path: Path) -> None:
    if os.name == "nt":
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$target = $env:TAXAX_ACL_TARGET
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
# 새 DirectorySecurity 객체를 ::new()로 만들면 어떤 섹션을 실제로 바꿀지
# 모르는 상태라 SeSecurityPrivilege 요구 오류가 날 수 있어, 기존 ACL을
# GetAccessControl()로 읽어서 그 객체를 수정한다. 또한 Set-Acl 커맨드릿은
# 디렉터리 대상일 때 그 자체로 SeSecurityPrivilege를 요구하는 결함이 있어
# (일반 사용자 토큰에는 없는 권한), DirectoryInfo.SetAccessControl() 메서드를
# 직접 호출해 이를 우회한다.
$dirInfo = New-Object System.IO.DirectoryInfo($target)
$acl = $dirInfo.GetAccessControl()
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($rule)
$dirInfo.SetAccessControl($acl)
"""
        _powershell(script, environment={"TAXAX_ACL_TARGET": str(path)})
        return
    os.chmod(path, 0o700, follow_symlinks=False)


def restrict_file_to_current_user(path: Path) -> None:
    if os.name == "nt":
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$target = $env:TAXAX_ACL_TARGET
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$fileInfo = New-Object System.IO.FileInfo($target)
$acl = $fileInfo.GetAccessControl()
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($rule)
$fileInfo.SetAccessControl($acl)
"""
        _powershell(script, environment={"TAXAX_ACL_TARGET": str(path)})
        return
    os.chmod(path, 0o600, follow_symlinks=False)


def restricted_directory_permissions_verified(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    if os.name == "nt":
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$target = $env:TAXAX_ACL_TARGET
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = (New-Object System.IO.DirectoryInfo($target)).GetAccessControl()
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier])
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
$validRule = $false
if ($rules.Count -eq 1) {
    $rule = $rules[0]
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $validRule = (
        (-not $rule.IsInherited) -and
        ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) -and
        ($rule.IdentityReference.Value -eq $current.Value) -and
        (($rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl) -and
        (($rule.InheritanceFlags -band $inheritance) -eq $inheritance)
    )
}
$result = [ordered]@{
    valid = ($acl.AreAccessRulesProtected -and ($owner.Value -eq $current.Value) -and $validRule)
}
$result | ConvertTo-Json -Compress
"""
        try:
            output = _powershell(script, environment={"TAXAX_ACL_TARGET": str(path)})
            result = json.loads(output)
        except (LocalSecretError, json.JSONDecodeError, OSError):
            return False
        return result.get("valid") is True
    directory_stat = path.stat(follow_symlinks=False)
    owner_matches = not hasattr(os, "getuid") or directory_stat.st_uid == os.getuid()
    return owner_matches and stat.S_IMODE(directory_stat.st_mode) == 0o700


def restricted_permissions_verified(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if os.name == "nt":
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$target = $env:TAXAX_ACL_TARGET
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = (New-Object System.IO.FileInfo($target)).GetAccessControl()
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier])
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
$validRule = $false
if ($rules.Count -eq 1) {
    $rule = $rules[0]
    $validRule = (
        (-not $rule.IsInherited) -and
        ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) -and
        ($rule.IdentityReference.Value -eq $current.Value) -and
        (($rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl)
    )
}
$result = [ordered]@{
    valid = ($acl.AreAccessRulesProtected -and ($owner.Value -eq $current.Value) -and $validRule)
    protected = $acl.AreAccessRulesProtected
    owner_matches = ($owner.Value -eq $current.Value)
    rule_count = $rules.Count
}
$result | ConvertTo-Json -Compress
"""
        try:
            output = _powershell(script, environment={"TAXAX_ACL_TARGET": str(path)})
            result = json.loads(output)
        except (LocalSecretError, json.JSONDecodeError, OSError):
            return False
        return result.get("valid") is True
    file_stat = path.stat(follow_symlinks=False)
    owner_matches = not hasattr(os, "getuid") or file_stat.st_uid == os.getuid()
    return owner_matches and stat.S_IMODE(file_stat.st_mode) == 0o600


def copy_file_permissions(source: Path, destination: Path) -> None:
    if os.name == "nt":
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$source = $env:TAXAX_ACL_SOURCE
$target = $env:TAXAX_ACL_TARGET
$acl = (New-Object System.IO.FileInfo($source)).GetAccessControl()
$fileInfo = New-Object System.IO.FileInfo($target)
$fileInfo.SetAccessControl($acl)
"""
        _powershell(
            script,
            environment={
                "TAXAX_ACL_SOURCE": str(source),
                "TAXAX_ACL_TARGET": str(destination),
            },
        )
        return
    os.chmod(destination, stat.S_IMODE(source.stat(follow_symlinks=False).st_mode), follow_symlinks=False)


def _validate_target(path: Path) -> None:
    if path.is_symlink():
        raise LocalSecretError("secret 설정 파일은 symbolic link일 수 없습니다.")
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            raise LocalSecretError("secret 설정 경로에 symbolic link directory가 포함될 수 없습니다.")
        current = current.parent


def _credential(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_CREDENTIAL_CHARS
        or "\x00" in normalized
        or "\r" in normalized
        or "\n" in normalized
    ):
        raise LocalSecretError(f"법제처 OC는 1~{_MAX_CREDENTIAL_CHARS}자의 한 줄 값이어야 합니다.")
    return normalized


_ACL_VERIFY_SNIPPET = r"""
function Test-TaxaxOwnerOnlyAcl($acl, $sid, $inheritanceRequired) {
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier])
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    $validRule = $false
    if ($rules.Count -eq 1) {
        $rule = $rules[0]
        $rightsOk = (($rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl)
        $baseOk = (
            (-not $rule.IsInherited) -and
            ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) -and
            ($rule.IdentityReference.Value -eq $sid.Value) -and
            $rightsOk
        )
        if ($inheritanceRequired) {
            $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
            $validRule = $baseOk -and (($rule.InheritanceFlags -band $inheritance) -eq $inheritance)
        } else {
            $validRule = $baseOk
        }
    }
    return ($acl.AreAccessRulesProtected -and ($owner.Value -eq $sid.Value) -and $validRule)
}
"""


def _windows_secret_write(target: Path, payload: bytes) -> None:
    """secret 파일 생성·ACL 설정·원자적 교체를 하나의 프로세스에서 수행한다.

    Microsoft Store(패키지형) Python은 `%LOCALAPPDATA%` 하위 쓰기를 패키지별
    가상 저장소로 조용히 리디렉션한다. Python이 먼저 mkdir·파일을 만들고
    별도 프로세스인 powershell.exe로 그 경로의 ACL만 설정하면, PowerShell은
    가상화되지 않은 실제 경로를 보므로 "경로가 존재하지 않는다"며 실패한다.
    그래서 디렉터리 생성부터 원자적 교체까지 전부 이 하나의 PowerShell
    프로세스 안에서 처리해, 두 프로세스가 서로 다른 파일시스템 뷰를 보는
    상황 자체를 없앤다.
    """
    script = (
        _ACL_VERIFY_SNIPPET
        + r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $dir = $env:TAXAX_SECRET_DIR
    $target = $env:TAXAX_SECRET_TARGET
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    if ((Get-Item -LiteralPath $dir).LinkType) { throw "directory is a symlink" }

    # ::new()로 만든 빈 ACL 객체는 SeSecurityPrivilege 오류를 낼 수 있어
    # GetAccessControl()로 읽은 객체를 수정한다. 또한 Set-Acl 커맨드릿은
    # 디렉터리 대상일 때 그 자체로 (일반 사용자 토큰에는 없는)
    # SeSecurityPrivilege를 요구하는 결함이 있어, DirectoryInfo/FileInfo의
    # SetAccessControl() 메서드를 직접 호출해 이를 우회한다.
    $dirInfo = New-Object System.IO.DirectoryInfo($dir)
    $dirAcl = $dirInfo.GetAccessControl()
    $dirAcl.SetOwner($sid)
    $dirAcl.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $dirRule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, $inheritance, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
    $dirAcl.SetAccessRule($dirRule)
    $dirInfo.SetAccessControl($dirAcl)

    $bytes = [System.Convert]::FromBase64String($env:TAXAX_SECRET_PAYLOAD_B64)
    $temp = Join-Path $dir (".taxax-secret-" + [System.Guid]::NewGuid().ToString("N") + ".tmp")
    [System.IO.File]::WriteAllBytes($temp, $bytes)

    $fileInfo = New-Object System.IO.FileInfo($temp)
    $fileAcl = $fileInfo.GetAccessControl()
    $fileAcl.SetOwner($sid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileRule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, [System.Security.AccessControl.AccessControlType]::Allow)
    $fileAcl.SetAccessRule($fileRule)
    $fileInfo.SetAccessControl($fileAcl)

    if (Test-Path -LiteralPath $target) {
        if ((Get-Item -LiteralPath $target).LinkType) { throw "target is a symlink" }
        # File.Replace는 대상의 기존 ACL을 내부적으로 병합하려다 SeSecurityPrivilege를
        # 요구할 수 있다. 삭제 후 rename이면 그 병합 시도 자체가 없다.
        Remove-Item -LiteralPath $target -Force
    }
    Rename-Item -LiteralPath $temp -NewName (Split-Path -Leaf $target)

    $finalAcl = (New-Object System.IO.FileInfo($target)).GetAccessControl()
    $ok = Test-TaxaxOwnerOnlyAcl $finalAcl $sid $false
    if (-not $ok) { throw "written file ACL verification failed" }
    [ordered]@{ ok = $true } | ConvertTo-Json -Compress
}
catch {
    [ordered]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
    )
    environment = {
        "TAXAX_SECRET_DIR": str(target.parent),
        "TAXAX_SECRET_TARGET": str(target),
        "TAXAX_SECRET_PAYLOAD_B64": base64.b64encode(payload).decode("ascii"),
    }
    try:
        output = _powershell(script, environment=environment)
        result = json.loads(output)
    except (LocalSecretError, json.JSONDecodeError) as exc:
        raise LocalSecretError("secret 파일 쓰기에 실패했습니다.") from exc
    if not result.get("ok"):
        raise LocalSecretError(f"secret 파일 쓰기에 실패했습니다: {result.get('error', '알 수 없는 오류')}")


def _windows_secret_read(target: Path, *, max_bytes: int) -> bytes | None:
    """secret 파일 존재·심볼릭링크·크기·ACL 확인과 읽기를 한 프로세스에서 수행한다."""
    script = (
        _ACL_VERIFY_SNIPPET
        + r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $target = $env:TAXAX_SECRET_TARGET
    $maxBytes = [int]$env:TAXAX_SECRET_MAX_BYTES
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        [ordered]@{ ok = $true; exists = $false } | ConvertTo-Json -Compress
        exit 0
    }
    $item = Get-Item -LiteralPath $target
    if ($item.LinkType) { throw "secret file is a symlink" }
    if ($item.Length -gt $maxBytes) { throw "secret file exceeds size limit" }
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = (New-Object System.IO.FileInfo($target)).GetAccessControl()
    if (-not (Test-TaxaxOwnerOnlyAcl $acl $sid $false)) { throw "secret file is not protected to the current user" }
    $bytes = [System.IO.File]::ReadAllBytes($target)
    $b64 = [System.Convert]::ToBase64String($bytes)
    [ordered]@{ ok = $true; exists = $true; payload_b64 = $b64 } | ConvertTo-Json -Compress
}
catch {
    [ordered]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
    )
    environment = {
        "TAXAX_SECRET_TARGET": str(target),
        "TAXAX_SECRET_MAX_BYTES": str(max_bytes),
    }
    try:
        output = _powershell(script, environment=environment)
        result = json.loads(output)
    except (LocalSecretError, json.JSONDecodeError) as exc:
        raise LocalSecretError("secret 파일 읽기에 실패했습니다.") from exc
    if not result.get("ok"):
        raise LocalSecretError(f"secret 파일 읽기에 실패했습니다: {result.get('error', '알 수 없는 오류')}")
    if not result.get("exists"):
        return None
    return base64.b64decode(result["payload_b64"])


def _windows_secret_delete(target: Path) -> bool:
    """secret 파일 삭제를 mkdir·ACL과 동일하게 단일 PowerShell 프로세스에서 수행한다."""
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $target = $env:TAXAX_SECRET_TARGET
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        [ordered]@{ ok = $true; existed = $false } | ConvertTo-Json -Compress
        exit 0
    }
    if ((Get-Item -LiteralPath $target).LinkType) { throw "secret file is a symlink" }
    Remove-Item -LiteralPath $target -Force
    [ordered]@{ ok = $true; existed = $true } | ConvertTo-Json -Compress
}
catch {
    [ordered]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
    try:
        output = _powershell(script, environment={"TAXAX_SECRET_TARGET": str(target)})
        result = json.loads(output)
    except (LocalSecretError, json.JSONDecodeError) as exc:
        raise LocalSecretError("secret 파일 삭제에 실패했습니다.") from exc
    if not result.get("ok"):
        raise LocalSecretError(f"secret 파일 삭제에 실패했습니다: {result.get('error', '알 수 없는 오류')}")
    return bool(result.get("existed"))


def _decode_secret_payload(payload: bytes) -> str:
    try:
        parsed: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalSecretError("secret 설정 파일을 읽지 못했습니다.") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"format", "law_go_oc"} or parsed.get("format") != _SECRET_FORMAT:
        raise LocalSecretError("secret 설정 파일 schema가 올바르지 않습니다.")
    value = parsed.get("law_go_oc")
    if not isinstance(value, str):
        raise LocalSecretError("secret 설정 파일 schema가 올바르지 않습니다.")
    return _credential(value)


def save_law_go_credential(value: str, path: Path | None = None) -> Path:
    credential = _credential(value)
    target = Path(path) if path is not None else default_secret_file()
    if not target.is_absolute():
        raise LocalSecretError("secret 설정 파일은 절대경로여야 합니다.")
    _validate_target(target)
    payload = (
        json.dumps(
            {"format": _SECRET_FORMAT, "law_go_oc": credential},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    if os.name == "nt":
        # 디렉터리 생성부터 ACL·원자적 교체까지 전부 하나의 PowerShell 프로세스
        # 안에서 처리한다. 패키지형(Microsoft Store) Python은 %LOCALAPPDATA% 쓰기를
        # 패키지별 가상 저장소로 조용히 리디렉션하므로, 이 프로세스(Python)가 먼저
        # mkdir·파일을 만들고 별도 프로세스(powershell.exe)로 그 경로만 ACL 설정하면
        # 서로 다른 파일시스템 뷰를 보게 되어 "경로가 존재하지 않는다"며 실패한다.
        _windows_secret_write(target, payload)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    if not restricted_directory_permissions_verified(target.parent):
        restrict_directory_to_current_user(target.parent)
        if not restricted_directory_permissions_verified(target.parent):
            raise LocalSecretError("현재 사용자 전용 secret directory 권한을 검증하지 못했습니다.")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        restrict_file_to_current_user(temporary)
        if not restricted_permissions_verified(temporary):
            raise LocalSecretError("현재 사용자 전용 secret 파일 권한을 검증하지 못했습니다.")
        if target.exists():
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise LocalSecretError("secret 설정 파일이 동시에 생성돼 덮어쓰지 않습니다.") from exc
            temporary.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def load_law_go_credential(path: Path | None = None) -> str | None:
    target = Path(path) if path is not None else default_secret_file()
    _validate_target(target)

    if os.name == "nt":
        payload = _windows_secret_read(target, max_bytes=_MAX_SECRET_FILE_BYTES)
        return _decode_secret_payload(payload) if payload is not None else None

    if not target.exists():
        return None
    if not target.is_file() or target.stat().st_size > _MAX_SECRET_FILE_BYTES:
        raise LocalSecretError("secret 설정 파일 형식 또는 크기가 올바르지 않습니다.")
    if not restricted_permissions_verified(target):
        raise LocalSecretError("secret 설정 파일이 현재 사용자 전용 권한으로 보호되지 않았습니다.")
    try:
        return _decode_secret_payload(target.read_bytes())
    except OSError as exc:
        raise LocalSecretError("secret 설정 파일을 읽지 못했습니다.") from exc


def delete_law_go_credential(path: Path | None = None, *, confirmed: bool = False) -> bool:
    if not confirmed:
        raise LocalSecretError("secret 삭제에는 명시적 확인이 필요합니다.")
    target = Path(path) if path is not None else default_secret_file()
    _validate_target(target)

    if os.name == "nt":
        return _windows_secret_delete(target)

    if not target.exists():
        return False
    if not target.is_file():
        raise LocalSecretError("secret 설정 경로가 일반 파일이 아닙니다.")
    target.unlink()
    return True
