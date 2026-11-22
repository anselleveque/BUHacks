"""Developer dialog IDs are unique and its static controls have accessible names."""
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


class DialogMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_dialog = False
        self.ids = Counter()
        self.controls = []
        self.labels = []
        self.open_label = None
        self.open_button = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "dialog" and attrs.get("id") == "devDialog":
            self.in_dialog = True
        if not self.in_dialog:
            return
        if "id" in attrs:
            self.ids[attrs["id"]] += 1
        if tag == "label":
            self.open_label = {"for": attrs.get("for"), "text": ""}
            self.labels.append(self.open_label)
        if tag in {"button", "input", "select", "textarea"}:
            control = (tag, attrs, self.open_label, [])
            self.controls.append(control)
            if tag == "button":
                self.open_button = control

    def handle_data(self, data):
        if self.open_label is not None:
            self.open_label["text"] += data
        if self.open_button is not None:
            self.open_button[3].append(data)

    def handle_endtag(self, tag):
        if tag == "dialog":
            self.in_dialog = False
        if tag == "label":
            self.open_label = None
        if tag == "button":
            self.open_button = None


def test_developer_dialog_controls_have_unique_ids_and_labels():
    markup = DialogMarkup()
    markup.feed((Path(__file__).resolve().parents[1] / "index.html").read_text())
    assert markup.ids["devDemoTimeInput"] == 1
    assert not [id_ for id_, count in markup.ids.items() if count > 1]
    for tag, attrs, wrapping_label, text in markup.controls:
        assert (
            attrs.get("aria-label", "").strip()
            or (attrs.get("aria-labelledby") and all(ref in markup.ids for ref in attrs["aria-labelledby"].split()))
            or (tag == "button" and "".join(text).strip())
            or (wrapping_label and wrapping_label["text"].strip())
            or any(label["for"] and label["for"] == attrs.get("id") and label["text"].strip()
                   for label in markup.labels)
        ), (tag, attrs)
