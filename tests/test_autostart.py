from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_user_service_exposes_only_middleware_protected_phone_access() -> None:
    service = (ROOT / "deploy/morning-mail-pa.service").read_text(encoding="utf-8")

    assert "--host 0.0.0.0" in service
    assert "WantedBy=default.target" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=%h/dishan/morning-mail-pa/data %h/dishan/morning-mail-pa/secrets" in service


def test_installer_uses_user_service_not_system_sudo() -> None:
    installer = (ROOT / "scripts/install_user_service.sh").read_text(encoding="utf-8")

    assert "systemctl --user enable --now morning-mail-pa.service" in installer
    assert "sudo" not in installer
