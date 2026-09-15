from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Sequence

from .installer import InstallationError, install_local_application, remove_local_credential
from .local_config import default_secret_file
from .service import default_legal_data_dir

_LAW_GO_GUIDE = "https://open.law.go.kr/"


def _bundle_directory() -> Path:
    if getattr(sys, "frozen", False):
        extracted = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        payload = extracted / "payload"
        return payload if payload.is_dir() else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3] / "dist" / "windows-payload"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taxax-legal-setup", description="TAXax Legal MCP Windows 설치 마법사")
    parser.add_argument("--print-paths", action="store_true", help="사용자별 설치·데이터·secret 경로를 JSON으로 출력합니다.")
    parser.add_argument("--paths-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--self-check-output", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("taxax-legal-setup은 Windows에서만 실행할 수 있습니다.")
    if arguments.self_check_output is not None:
        bundle = _bundle_directory()
        expected = ("taxax-legal.exe", "taxax-legal-mcp.exe")
        missing = [name for name in expected if not (bundle / name).is_file()]
        if missing:
            raise SystemExit("설치 payload self-check에 실패했습니다.")
        arguments.self_check_output.write_text(
            json.dumps(
                {"status": "ok", "payload": list(expected)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    if arguments.print_paths or arguments.paths_output is not None:
        from .installer import default_claude_desktop_config, default_install_root

        payload = {
            "install_root": str(default_install_root()),
            "data_dir": str(default_legal_data_dir()),
            "secret_file": str(default_secret_file()),
            "claude_config": str(default_claude_desktop_config()),
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if arguments.paths_output is not None:
            arguments.paths_output.write_text(serialized, encoding="utf-8")
        else:
            print(serialized, end="")
        return 0

    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("TAXax Legal MCP 설치")
    root.geometry("720x600")
    root.minsize(680, 560)

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="TAXax Legal MCP", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text="Claude Desktop에서 공식 공개 법률 자료를 조사하는 로컬 MCP 서버를 설치합니다.",
        wraplength=660,
    ).pack(anchor="w", pady=(4, 16))

    ttk.Label(frame, text="1. 법제처 Open API OC (선택)", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text=(
            "키가 없으면 비워 두고 설치할 수 있습니다. MCP와 offline 기능은 기동되며 공식 API 조회만 차단됩니다. "
            "입력한 값은 Claude 설정이 아니라 현재 사용자 전용 ACL을 적용한 별도 평문 파일에 저장됩니다. "
            "관리자 권한으로 접근 가능한 암호화 저장소는 아닙니다."
        ),
        wraplength=660,
    ).pack(anchor="w", pady=(4, 6))
    credential = tk.StringVar()
    ttk.Entry(frame, textvariable=credential, show="●", width=80).pack(fill="x")
    ttk.Button(frame, text="법제처 Open API 안내 열기", command=lambda: webbrowser.open(_LAW_GO_GUIDE)).pack(anchor="w", pady=(6, 16))

    ttk.Label(frame, text="2. 설치 위치", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"데이터: {default_legal_data_dir()}", wraplength=660).pack(anchor="w", pady=(4, 2))
    ttk.Label(frame, text=f"인증 설정: {default_secret_file()}", wraplength=660).pack(anchor="w", pady=(0, 16))

    ttk.Label(frame, text="3. Claude Desktop 등록", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    consent = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        frame,
        text="기존 설정을 보존·백업하고 taxax-legal 서버 항목을 병합하는 데 동의합니다.",
        variable=consent,
    ).pack(anchor="w", pady=(4, 2))
    allow_update = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        frame,
        text="기존 taxax-legal 항목이 다르면 백업 후 이 version의 경로로 교체합니다.",
        variable=allow_update,
    ).pack(anchor="w", pady=(0, 16))

    status = tk.StringVar(value="설치 준비가 되었습니다.")
    ttk.Label(frame, textvariable=status, wraplength=660, foreground="#184a75").pack(anchor="w", pady=(0, 16))

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", side="bottom")

    def install() -> None:
        if not consent.get():
            messagebox.showwarning("동의 필요", "Claude Desktop 설정 병합 동의를 먼저 선택하십시오.")
            return
        status.set("설치 파일과 설정을 검증하고 있습니다...")
        root.update_idletasks()
        try:
            result = install_local_application(
                _bundle_directory(),
                credential=credential.get() or None,
                consent_to_configure_claude=True,
                allow_config_update=allow_update.get(),
            )
        except (InstallationError, OSError) as exc:
            status.set("설치가 완료되지 않았습니다. 기존 설정과 데이터는 삭제하지 않았습니다.")
            messagebox.showerror(
                "설치 실패",
                f"{exc}\n\n일부 로컬 설치 파일이나 명시적으로 입력한 OC가 저장됐을 수 있습니다.",
            )
            return
        credential.set("")
        backup = f"\n설정 backup: {result.backup_path}" if result.backup_path else ""
        status.set("doctor 진단이 통과했습니다. Claude Desktop을 완전히 종료한 뒤 다시 시작하십시오.")
        messagebox.showinfo(
            "설치 완료",
            f"설치 경로: {result.install_dir}\n데이터 경로: {result.data_dir}{backup}\n\nClaude Desktop을 재시작하십시오.",
        )

    def remove_secret() -> None:
        if not messagebox.askyesno(
            "법제처 OC 삭제",
            "현재 사용자 전용 법제처 OC 파일을 삭제하시겠습니까? 공개 법률 upstream 조회는 비활성화됩니다.",
        ):
            return
        try:
            removed = remove_local_credential(confirmed=True)
        except (InstallationError, OSError) as exc:
            messagebox.showerror("삭제 실패", str(exc))
            return
        credential.set("")
        status.set("저장된 법제처 OC를 삭제했습니다." if removed else "저장된 법제처 OC가 없습니다.")

    ttk.Button(buttons, text="취소", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="설치 및 진단", command=install).pack(side="right", padx=8)
    ttk.Button(buttons, text="저장된 OC 삭제", command=remove_secret).pack(side="left")

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
