import argparse
import re
from html.parser import HTMLParser
from pathlib import Path


DESKTOP_SOURCE_FILES = [
    "main.pyw",
    "viper_ui_dashboard.py",
    "viper_ui_device_tools.py",
    "viper_ui_doorbell.py",
    "viper_ui_diagnostics.py",
    "viper_ui_fridge.py",
    "viper_ui_hvac.py",
    "viper_ui_prompts.py",
    "viper_ui_speakers.py",
    "viper_ui_tts.py",
    "viper_ui_vacuum.py",
]


class RemoteControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.buttons = []
        self.controls = []
        self.status_regions = []
        self.labels_for = {}
        self._button = None
        self._label = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        parent_tags = [item[0] for item in self.stack]
        self.stack.append((tag, attrs_dict))
        line = self.getpos()[0]
        if tag == "label":
            self._label = {"for": attrs_dict.get("for", ""), "text": "", "line": line}
        if tag == "button":
            self._button = {"attrs": attrs_dict, "text": "", "line": line}
        if tag in {"input", "select", "textarea"}:
            self.controls.append({
                "tag": tag,
                "attrs": attrs_dict,
                "line": line,
                "inside_label": "label" in parent_tags,
            })
        if attrs_dict.get("aria-live") or attrs_dict.get("role") in {"alert", "status"}:
            self.status_regions.append({"tag": tag, "attrs": attrs_dict, "line": line})

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None:
            self.buttons.append(self._button)
            self._button = None
        if tag == "label" and self._label is not None:
            if self._label["for"]:
                self.labels_for[self._label["for"]] = text_content(self._label["text"])
            self._label = None
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                del self.stack[idx:]
                break

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data
        if self._label is not None:
            self._label["text"] += data


def text_content(value):
    return " ".join(str(value or "").split())


def desktop_inventory(main_text):
    items = []
    for match in re.finditer(r"(self\.[A-Za-z0-9_]+)\s*=\s*wx\.Button\([^\\n]+label=([^\\n]+)", main_text):
        items.append(("button", match.group(1), text_content(match.group(2)), match.start()))
    for match in re.finditer(r"(self\.[A-Za-z0-9_]+)\s*=\s*wx\.CheckBox\([^\\n]+label=([^\\n]+)", main_text):
        items.append(("checkbox", match.group(1), text_content(match.group(2)), match.start()))
    for match in re.finditer(r"(self\.[A-Za-z0-9_]+)\s*=\s*wx\.TextCtrl\(", main_text):
        tail = main_text[match.start():match.start() + 900]
        name = ""
        describe = re.search(r"_describe_control\(\s*" + re.escape(match.group(1)) + r"\s*,\s*\"([^\"]+)\"", tail)
        set_name = re.search(re.escape(match.group(1)) + r"\.SetName\(\"([^\"]+)\"", tail)
        if describe:
            name = describe.group(1)
        elif set_name:
            name = set_name.group(1)
        items.append(("status/text", match.group(1), name or "NO ACCESSIBLE NAME FOUND NEAR CREATION", match.start()))
    items.sort(key=lambda item: item[3])
    return items


def remote_inventory(template_text):
    parser = RemoteControlParser()
    parser.feed(template_text)
    lines = []
    for button in parser.buttons:
        attrs = button["attrs"]
        text = text_content(button["text"])
        accessible = text_content(attrs.get("aria-label") or text)
        lines.append(("button", accessible or "NO ACCESSIBLE NAME", button["line"]))
    for control in parser.controls:
        attrs = control["attrs"]
        control_type = attrs.get("type") or control["tag"]
        if control_type == "hidden":
            continue
        label = (
            text_content(attrs.get("aria-label"))
            or parser.labels_for.get(attrs.get("id", ""), "")
            or text_content(attrs.get("aria-describedby"))
            or ("inside label" if control["inside_label"] else "")
            or "NO LABEL FOUND"
        )
        name = attrs.get("name") or attrs.get("id") or control["tag"]
        lines.append((f"{control['tag']}:{control_type}", f"{name}: {label}", control["line"]))
    for status in parser.status_regions:
        attrs = status["attrs"]
        label = attrs.get("aria-label") or attrs.get("id") or attrs.get("role") or attrs.get("aria-live")
        lines.append(("status/live", text_content(label), status["line"]))
    return sorted(lines, key=lambda item: item[2])


def build_report(root):
    root = Path(root)
    main_text = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in DESKTOP_SOURCE_FILES
        if (root / path).exists()
    )
    template_text = (root / "templates" / "remote.html").read_text(encoding="utf-8")
    lines = [
        "Viper Vision Accessibility Control Inventory",
        "==========================================",
        "",
        "Desktop wx Controls",
        "-------------------",
    ]
    for kind, name, label, _pos in desktop_inventory(main_text):
        lines.append(f"{kind}: {name}: {label}")
    lines.extend(["", "Remote Web Controls", "-------------------"])
    for kind, label, line in remote_inventory(template_text):
        lines.append(f"line {line}: {kind}: {label}")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = build_report(args.root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
