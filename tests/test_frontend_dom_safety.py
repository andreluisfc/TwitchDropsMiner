import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def test_app_js_only_uses_innerhtml_to_clear_elements():
    app_source = APP_JS.read_text(encoding="utf-8")
    unsafe_assignments = []

    for match in re.finditer(r"\binnerHTML\s*=\s*([^;\n]+)", app_source):
        assigned_value = match.group(1).strip()
        if assigned_value not in {"''", '""'}:
            line_number = app_source.count("\n", 0, match.start()) + 1
            unsafe_assignments.append(f"line {line_number}: {match.group(0).strip()}")

    assert unsafe_assignments == []


def test_hub_module_actions_are_declared_by_modules():
    app_source = APP_JS.read_text(encoding="utf-8")
    action_fn = app_source[
        app_source.index("function getHubModuleActions") : app_source.index("async function runHubModuleAction")
    ]

    assert "module.actions" in action_fn
    assert "module.id ===" not in action_fn
    assert "run_account" in app_source


def test_hub_module_links_are_declared_by_module_details():
    app_source = APP_JS.read_text(encoding="utf-8")
    link_fn = app_source[
        app_source.index("function getHubModuleLinks") : app_source.index("function appendFreeGamesLogButton")
    ]

    assert "module.details?.vnc" in link_fn
    assert "module.id ===" not in link_fn
