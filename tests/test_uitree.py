from __future__ import annotations

import pytest

from qa_mcp_server.errors import CommandFailed
from qa_mcp_server.fake import FakeAdbTransport
from qa_mcp_server.uitree import extract_xml, find_nodes, parse_bounds, parse_hierarchy, to_tree
from qa_mcp_server.validation import Selector

XML = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
    '<hierarchy rotation="0">'
    '<node class="android.widget.FrameLayout" package="p.q" bounds="[0,0][100,200]">'
    '<node class="android.widget.LinearLayout" package="p.q" bounds="[0,0][100,100]">'
    '<node class="android.widget.Button" text="OK" resource-id="p.q:id/ok" clickable="true"'
    ' package="p.q" bounds="[10,10][50,30]" />'
    '<node class="android.widget.EditText" text="hunter2" password="true" resource-id="p.q:id/pw"'
    ' package="p.q" bounds="[10,40][90,60]" focused="true" enabled="false" />'
    "</node>"
    '<node class="android.widget.TextView" text="OK later" package="p.q" bounds="bad" />'
    "</node></hierarchy>"
)


def test_extract_xml_strips_status_line() -> None:
    raw = XML + "UI hierchary dumped to: /dev/tty\n"
    assert extract_xml(raw) == XML


def test_extract_xml_without_hierarchy() -> None:
    with pytest.raises(CommandFailed, match="no hierarchy"):
        extract_xml("ERROR: could not get idle state.")
    with pytest.raises(CommandFailed, match="empty output"):
        extract_xml("")


def test_parse_bounds() -> None:
    assert parse_bounds("[1,2][3,4]") == (1, 2, 3, 4)
    assert parse_bounds("garbage") is None


def test_compact_tree_drops_wrappers_and_redacts_passwords() -> None:
    tree = to_tree(parse_hierarchy(XML), compact=True, max_nodes=100)
    assert tree["packages"] == ["p.q"]
    assert tree["total_nodes"] == 5
    classes = [n["class"] for n in tree["nodes"]]
    assert classes == ["Button", "EditText", "TextView"]
    button, password, _ = tree["nodes"]
    assert button == {
        "class": "Button",
        "text": "OK",
        "resource_id": "p.q:id/ok",
        "bounds": [10, 10, 50, 30],
        "flags": ["clickable"],
    }
    assert "text" not in password
    assert password["flags"] == ["focused", "password", "disabled"]


def test_full_tree_keeps_every_node() -> None:
    tree = to_tree(parse_hierarchy(XML), compact=False, max_nodes=100)
    assert tree["node_count"] == 5
    assert tree["nodes"][0]["class"] == "FrameLayout"


def test_max_nodes_truncates() -> None:
    tree = to_tree(parse_hierarchy(XML), compact=False, max_nodes=2)
    assert tree["truncated"] is True
    assert tree["node_count"] == 2
    compact = to_tree(parse_hierarchy(XML), compact=True, max_nodes=1)
    assert compact["truncated"] is True


def test_find_nodes_exact_contains_and_short_id() -> None:
    nodes = parse_hierarchy(XML)
    assert len(find_nodes(nodes, Selector(text="OK"))) == 1
    # "OK later" has no bounds, so it cannot be tapped and is not returned.
    assert len(find_nodes(nodes, Selector(text="ok", match="contains"))) == 1
    assert find_nodes(nodes, Selector(resource_id="ok"))[0].center == (30, 20)
    assert find_nodes(nodes, Selector(resource_id="p.q:id/ok"))
    assert not find_nodes(nodes, Selector(content_desc="nothing"))


def test_invalid_xml_rejected() -> None:
    with pytest.raises(CommandFailed, match="invalid UI hierarchy"):
        parse_hierarchy("<hierarchy><node></hierarchy>")


def test_wrong_root_rejected() -> None:
    with pytest.raises(CommandFailed):
        parse_hierarchy("<?xml version='1.0'?><evil></evil><hierarchy></hierarchy>")


@pytest.mark.security
def test_xml_entity_expansion_bomb_rejected() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        '<hierarchy><node text="&lol2;" bounds="[0,0][1,1]"/></hierarchy>'
    )
    with pytest.raises(CommandFailed, match="invalid UI hierarchy"):
        parse_hierarchy(bomb)


@pytest.mark.security
def test_external_entity_rejected() -> None:
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE h [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<hierarchy><node text="&x;" bounds="[0,0][1,1]"/></hierarchy>'
    )
    with pytest.raises(CommandFailed, match="invalid UI hierarchy"):
        parse_hierarchy(xxe)


@pytest.mark.parametrize(
    "screen", ["launcher", "notes_empty", "editor", "notes_list", "crash_dialog"]
)
def test_bundled_fixture_screens_parse(screen: str) -> None:
    fake = FakeAdbTransport.from_file()
    nodes = parse_hierarchy(fake.render(screen))
    assert to_tree(nodes, compact=True, max_nodes=500)["node_count"] > 0
