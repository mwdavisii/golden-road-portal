"""The stylesheet (spec 2026-10-30 R1, R10): the design's values once, as custom properties; AA contrast for the text pairs the
pages use; a visible focus; the sidebar above the content on narrow screens; no web fonts, imports or remote URLs (the CSP allows none)."""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "portal/static/portal.css").read_text(encoding="utf-8")


def var(name):
    return re.search(rf"--{name}:\s*(#[0-9a-f]{{6}})\s*;", CSS).group(1)


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [("ink", "paper"), ("ink", "surface"), ("muted", "paper"), ("muted", "surface"), ("muted", "sunk"), ("muted-2", "surface"),
              ("muted-2", "sunk"), ("muted-2", "track"), ("gold-ink", "paper"), ("gold-ink", "surface"), ("side-ink", "side"),
              ("side-muted", "side"), ("gold", "side"), ("on-gold", "gold"), ("ok", "ok-wash"), ("ok-ink", "ok-wash"),
              ("warn", "warn-wash"), ("warn-ink", "warn-wash"), ("info", "info-wash"), ("info-ink", "info-wash"),
              ("danger", "danger-wash"), ("danger", "surface"), ("on-dark", "button"), ("on-dark", "ok"), ("term-ink", "term")]


@pytest.mark.parametrize("fg, bg", TEXT_PAIRS)
def test_text_pairs_meet_wcag_aa(fg, bg):
    assert contrast(var(fg), var(bg)) >= 4.5, (fg, bg, round(contrast(var(fg), var(bg)), 2))


def test_field_borders_and_the_focus_ring_meet_the_non_text_minimum():
    assert contrast(var("field-line"), var("surface")) >= 3 and contrast(var("focus"), var("surface")) >= 3


def test_focus_is_visible_and_the_sidebar_collapses_on_narrow_screens():
    assert ":focus-visible" in CSS and "outline:" in CSS.split(":focus-visible", 1)[1].split("}", 1)[0]
    narrow = CSS.split("@media (max-width: 860px)", 1)[1]
    assert ".shell" in narrow and "display: block" in narrow


def test_no_fonts_imports_or_remote_urls():
    assert "@import" not in CSS and "@font-face" not in CSS and "url(" not in CSS and "http" not in CSS


def test_the_classes_the_templates_use_are_styled():
    for cls in ("shell", "sidebar", "brand-mark", "nav-count", "nav-heading", "account", "content", "skip", "visually-hidden", "eyebrow",
                "lead", "card", "callout", "error", "badge", "state-pending", "state-sent_back", "state-merged", "state-failed", "button",
                "secondary", "danger", "ghost", "gold", "choices", "choice", "choice-grid", "choice-title", "choice-line", "tag", "decide",
                "override", "chips", "tabs", "table-wrap", "facts",
                "terminal", "steps", "timeline", "stepper", "toast", "empty", "confirm", "mono", "hint", "counter", "field", "actions"):
        assert re.search(rf"\.{re.escape(cls)}\b", CSS), cls


def test_the_sidebar_stretches_and_its_content_is_sticky():
    sidebar = CSS.split(".sidebar {", 1)[1].split("}", 1)[0]
    inner = CSS.split(".sidebar-inner {", 1)[1].split("}", 1)[0]
    assert "align-self: stretch" in sidebar and "min-height: 100vh" in sidebar and "sticky" not in sidebar
    assert "position: sticky" in inner and "top: 0" in inner


def test_one_centred_column_and_no_empty_notes():
    column = CSS.split(".column {", 1)[1].split("}", 1)[0]
    assert "max-width" in column and "margin: 0 auto" in column and ".content > *" not in CSS
    assert re.search(r"\.callout:empty[^{]*\{[^}]*display: none", CSS) and re.search(r"\.toast:empty", CSS)


def test_a_commit_is_short_in_monospace():
    assert re.search(r"\.sha\b", CSS)


def rules_with(selector_part):
    """(selector, body) of every rule whose selector names selector_part."""
    plain = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)   # a comment before a rule is not part of its selector
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", plain) if selector_part in sel]


def test_no_focus_rule_hides_the_outline_without_a_solid_ring():
    for selector, body in rules_with(":focus-visible"):
        if re.search(r"outline:\s*none", body):
            assert re.search(r"box-shadow:\s*0 0 0 [2-9]px var\(--focus\)", body), selector
            assert all(kind in selector for kind in ('input[type="text"]', "select", "textarea")) and "input:focus-visible" not in selector, selector


def test_a_choice_card_shows_focus_on_the_card():
    [(_, body)] = rules_with(".choice:has(input:focus-visible)")
    assert "outline: 3px solid var(--focus)" in body


LIGHT = ("paper", "surface", "sunk", "track", "wash", "highlight", "ok-wash", "warn-wash", "info-wash", "danger-wash")


@pytest.mark.parametrize("bg", LIGHT)
def test_the_focus_ring_is_3_to_1_on_every_light_background(bg):
    assert contrast(var("focus"), var(bg)) >= 3, (bg, round(contrast(var("focus"), var(bg)), 2))


def test_on_dark_backgrounds_the_ring_is_gold():
    for bg in ("side", "term", "button"):
        assert contrast(var("focus-on-dark"), var(bg)) >= 3, bg
    for scope in (".sidebar", ".card.dark", ".terminal"):
        assert any(scope in sel and "var(--focus-on-dark)" in body for sel, body in rules_with(":focus-visible")), scope


def test_links_on_the_dark_card_are_gold():
    [(_, body)] = rules_with(".card.dark a:not(.button)")
    assert "color: var(--gold)" in body and contrast(var("gold"), var("side")) >= 4.5


def test_the_timeline_has_a_connecting_line_and_the_requests_table_is_centred():
    line = [body for sel, body in rules_with(".timeline li") if "::after" in sel]
    assert line and any("var(--line-strong)" in b for b in line)
    assert any("step-done" in sel and "var(--ok)" in body for sel, body in rules_with("::after"))
    [(_, body)] = rules_with("table.requests td")
    assert "vertical-align: middle" in body


def test_the_people_page_s_classes_are_out_of_the_interim_section_and_nothing_unused_stays():
    assert "later rounds reskin" not in CSS and ".field-row" not in CSS
    assert re.search(r"\.link-list\b", CSS) and re.search(r"\.add-link\b", CSS)


def test_the_audit_table_fits_its_card():
    table = [body for selector, body in rules_with("table.audit") if selector == "table.audit"][0]   # the first: not the narrow-screen one
    assert "table-layout: fixed" in table
    cells = [b for s, b in rules_with("table.audit td")]
    assert any("overflow-wrap: anywhere" in b for b in cells)
    assert any("white-space: pre-wrap" in b for s, b in rules_with("table.audit pre"))
    assert all(re.search(rf"table\.audit col\.{c}\b", CSS) for c in ("when", "who", "action", "subject"))


def test_the_people_link_rows_line_up():
    form = [body for selector, body in rules_with(".link-list form") if selector == ".link-list form"][0]
    assert "display: grid" in form and re.search(r"grid-template-columns:\s*\d+rem 1fr auto", form)


def test_an_audit_action_wraps_only_at_its_dots_and_no_unused_inline_form_rule():
    [code] = [body for selector, body in rules_with("table.audit td.action code")]
    assert "overflow-wrap: normal" in code and "word-break: normal" in code
    assert "width: 12rem" in [body for selector, body in rules_with("table.audit col.action")][0]
    assert "form.inline" not in CSS
