from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_local_env.sh"


def test_native_kernel_smoke_activates_wsl_compatibility_before_torch() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index('"$python_bin" -I - "$attempt_root/gpu_kernel_smoke.json"')
    end = text.index("\nPY\n", start)
    smoke = text[start:end]

    activation = smoke.index("activation_state = activate()")
    torch_import = smoke.index("import torch")
    boltzgen_import = smoke.index(
        "from boltzgen.model.layers.triangular import TriangleMultiplicationOutgoing"
    )

    assert activation < torch_import < boltzgen_import
    assert 'activation_state["active"] is True' in smoke
    assert 'activation_state["activation_scope"] == "CURRENT_PROCESS_ONLY"' in smoke
    assert '"compatibility_state": compatibility_state' in smoke
