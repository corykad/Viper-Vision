"""Helpers for exposing Home Assistant VM functions through UI modules."""

from __future__ import annotations


SIMPLE_DELEGATE_NAMES = (
    "open_official_link",
    "open_url",
    "find_vboxmanage",
    "find_winget",
    "get_machine_architecture",
    "get_ha_vm_platform_status",
    "normalize_ha_vm_ram_mb",
    "normalize_ha_vm_disk_gb",
    "get_ha_vm_drive_space_status",
    "get_winget_status",
    "get_virtualbox_status",
    "is_windows_admin",
    "_run_powershell_command",
    "_windows_optional_feature_state",
    "get_windows_virtualization_status",
    "optimize_windows_for_virtualbox",
    "install_virtualbox_with_winget",
    "_hidden_subprocess_kwargs",
    "_clean_process_progress_line",
    "_run_process_with_progress",
    "_run_vbox",
    "_run_vbox_progress",
    "_vbox_vm_exists",
    "_choose_bridged_adapter",
    "get_latest_haos_virtualbox_asset",
    "download_file",
    "_extract_haos_disk",
    "_import_ha_ova",
    "_resize_virtualbox_disk",
    "_setup_progress_default_state",
    "_coerce_setup_progress_state",
    "_bytes_progress_percent",
    "_classify_setup_progress_message",
    "_format_setup_progress_state",
    "_check_home_assistant_core_ready",
    "build_ha_install_preflight_summary",
)


def make_delegate(ha_vm_module, name):
    def delegate(*args, **kwargs):
        return getattr(ha_vm_module, name)(*args, **kwargs)

    delegate.__name__ = name
    delegate.__qualname__ = name
    delegate.__doc__ = f"Delegate to viper_ha_vm.{name}."
    delegate._ha_vm_delegate = name
    return delegate


def install_simple_delegates(namespace, ha_vm_module, names=SIMPLE_DELEGATE_NAMES):
    for name in names:
        namespace[name] = make_delegate(ha_vm_module, name)
