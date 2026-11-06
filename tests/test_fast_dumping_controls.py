"""Exercise the real mode-switch handler against the actual form structure."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_both_settings_forms_switch_modes_with_hidden_compatibility_inputs():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the UI handler regression check")
    root = Path(__file__).resolve().parents[1] / "backend/app/static"

    class FormIds(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.ids = [], {}

        def handle_starttag(self, tag, attrs):
            data = dict(attrs)
            if data.get("id"):
                self.ids[data["id"]] = "label" in self.stack
            if tag not in {"input", "img", "meta", "link", "br", "hr"}:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.stack:
                self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    form = FormIds()
    form.feed((root / "fast-dumping.html").read_text())
    script = (root / "fast-dumping.js").read_text()
    handler = script[script.index("const manualControls ="):script.index('for (const prefix of ["", "edit-"])')]
    harness = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ids = JSON.parse(process.argv[1]);
const elements = Object.fromEntries(Object.entries(ids).map(([id, hasLabel]) =>
  [id, {disabled:false, textContent:'', setAttribute(){},
    closest(){return hasLabel ? {classList:{toggle(){}}} : null;}}]));
const context = vm.createContext({document:{querySelector(selector){
  assert.ok(elements[selector.slice(1)], `Missing element ${selector}`);
  return elements[selector.slice(1)];
}}});
vm.runInContext(process.argv[2], context);
for (const prefix of ['', 'edit-']) {
  vm.runInContext(`setAutomation('${prefix}', true)`, context);
  assert.equal(elements[prefix+'undercut-step'].disabled, true);
  vm.runInContext(`setAutomation('${prefix}', false)`, context);
  assert.equal(elements[prefix+'undercut-step'].disabled, false);
}
"""
    subprocess.run([node, "-e", harness, json.dumps(form.ids), handler], check=True,
        capture_output=True, text=True)
