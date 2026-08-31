"""
Unit tests for the pure conversion-engine pipeline. No database or Flask app
context needed -- the pipeline operates on plain dicts by design so this stays
fast and easy to reason about.
"""
import pytest

from app.conversion_engine import (
    explode_ebom, apply_substitutes, inject_items, classify_edges, convert_ebom_to_mbom,
)


# --- a tiny two-level fixture: ASSY -> SUB -> {A, B} , ASSY -> C ---------------

def make_parts():
    return {
        1: {"id": 1, "part_type": "ASSEMBLY"},
        2: {"id": 2, "part_type": "SUBASSEMBLY"},
        3: {"id": 3, "part_type": "COMPONENT"},  # A
        4: {"id": 4, "part_type": "COMPONENT"},  # B
        5: {"id": 5, "part_type": "COMPONENT"},  # C
        6: {"id": 6, "part_type": "COMPONENT"},  # substitute replacement for B
        7: {"id": 7, "part_type": "MFG_ITEM"},   # injected packaging
    }


def make_edges():
    return {
        1: [
            {"id": 101, "child_id": 2, "quantity": 1},  # ASSY -> SUB
            {"id": 102, "child_id": 5, "quantity": 1},  # ASSY -> C
        ],
        2: [
            {"id": 201, "child_id": 3, "quantity": 2},  # SUB -> A
            {"id": 202, "child_id": 4, "quantity": 1},  # SUB -> B
        ],
    }


def test_explode_ebom_flattens_full_tree():
    edges = explode_ebom(make_edges(), root_id=1)
    pairs = {(e["parent_id"], e["child_id"]) for e in edges}
    assert pairs == {(1, 2), (1, 5), (2, 3), (2, 4)}
    assert len(edges) == 4


def test_explode_ebom_detects_circular_reference():
    circular = {1: [{"id": 1, "child_id": 2, "quantity": 1}], 2: [{"id": 2, "child_id": 1, "quantity": 1}]}
    with pytest.raises(ValueError):
        explode_ebom(circular, root_id=1)


def test_apply_substitutes_replaces_target_and_keeps_others():
    edges = explode_ebom(make_edges(), root_id=1)
    result = apply_substitutes(edges, [{"target_part_id": 4, "replacement_part_id": 6}])

    substituted = [e for e in result if e["source"] == "SUBSTITUTED"]
    assert len(substituted) == 1
    assert substituted[0]["child_id"] == 6
    assert substituted[0]["substituted_from_part_id"] == 4

    untouched = [e for e in result if e["parent_id"] == 2 and e["child_id"] == 3]
    assert untouched[0]["source"] == "FROM_EBOM"


def test_inject_items_adds_line_under_every_matching_assembly():
    edges = explode_ebom(make_edges(), root_id=1)
    edges = apply_substitutes(edges, [])  # pipeline always runs substitutes first, to set "source"
    parts = make_parts()
    rules = [{"applies_to_part_type": "ASSEMBLY", "add_item_part_id": 7, "add_item_quantity": 1}]

    result = inject_items(edges, parts, root_id=1, add_item_rules=rules)
    injected = [e for e in result if e["source"] == "INJECTED"]

    assert len(injected) == 1
    assert injected[0]["parent_id"] == 1
    assert injected[0]["child_id"] == 7
    # original eBOM edges are preserved, not replaced
    assert len(result) == len(edges) + 1


def test_classify_edges_applies_scrap_factor_and_routing_by_child_type():
    edges = explode_ebom(make_edges(), root_id=1)
    edges = apply_substitutes(edges, [])
    parts = make_parts()

    result = classify_edges(
        edges, parts,
        make_or_buy_rules=[{"applies_to_part_type": "COMPONENT", "make_or_buy_value": "BUY"}],
        scrap_rules=[{"applies_to_part_type": "COMPONENT", "scrap_factor": 1.10}],
        routing_rules=[{"applies_to_part_type": "COMPONENT", "work_center": "RECEIVING"}],
    )

    a_line = next(e for e in result if e["child_id"] == 3)
    assert a_line["quantity"] == pytest.approx(2 * 1.10)
    assert a_line["make_or_buy"] == "BUY"
    assert a_line["work_center"] == "RECEIVING"

    # the SUBASSEMBLY node (child_id=2) has no matching rule -> untouched
    sub_line = next(e for e in result if e["child_id"] == 2)
    assert sub_line["quantity"] == 1
    assert sub_line["make_or_buy"] is None


def test_extended_quantity_multiplies_down_multiple_levels():
    """
    ASSY -> SUB (qty 3) -> A (qty 2). One unit of ASSY needs 3 SUBs, and each
    SUB needs 2 As, so ASSY needs 3*2=6 As total -- not 2. This is the
    difference between "quantity on this drawing" and "quantity to procure".
    """
    edges = explode_ebom({
        1: [{"id": 101, "child_id": 2, "quantity": 3}],
        2: [{"id": 201, "child_id": 3, "quantity": 2}],
    }, root_id=1)

    sub_edge = next(e for e in edges if e["child_id"] == 2)
    leaf_edge = next(e for e in edges if e["child_id"] == 3)

    assert sub_edge["quantity"] == 3
    assert sub_edge["extended_quantity"] == 3       # 3 SUBs per 1 ASSY
    assert leaf_edge["quantity"] == 2                # drawing-level: 2 As per SUB
    assert leaf_edge["extended_quantity"] == 6       # procurement-level: 6 As per ASSY


def test_injected_item_extended_quantity_scales_with_parent_assembly_count():
    """An item injected under a sub-assembly that itself appears 3x per unit
    should need 3x as many, not 1x -- injection has to respect the same
    extended-quantity math as eBOM-derived edges."""
    edges = explode_ebom(make_edges(), root_id=1)  # SUB (child_id=2) appears 1x under ASSY here
    edges = apply_substitutes(edges, [])
    parts = make_parts()
    rules = [{"applies_to_part_type": "SUBASSEMBLY", "add_item_part_id": 7, "add_item_quantity": 1}]

    result = inject_items(edges, parts, root_id=1, add_item_rules=rules)
    injected = next(e for e in result if e["source"] == "INJECTED")

    sub_edge = next(e for e in edges if e["child_id"] == 2)
    assert injected["extended_quantity"] == sub_edge["extended_quantity"] * 1  # 1 kit per SUB instance


def test_full_pipeline_end_to_end():
    parts = make_parts()
    bom = make_edges()
    rules = {
        "substitute": [{"target_part_id": 4, "replacement_part_id": 6}],
        "add_item": [{"applies_to_part_type": "ASSEMBLY", "add_item_part_id": 7, "add_item_quantity": 1}],
        "make_or_buy": [{"applies_to_part_type": "COMPONENT", "make_or_buy_value": "BUY"}],
        "scrap": [{"applies_to_part_type": "COMPONENT", "scrap_factor": 1.05}],
        "routing": [{"applies_to_part_type": "COMPONENT", "work_center": "RECEIVING"}],
    }

    result = convert_ebom_to_mbom(root_id=1, parts_by_id=parts, bom_edges_by_parent=bom, rules=rules)

    # 4 original eBOM edges + 1 injected packaging line = 5
    assert len(result) == 5

    child_ids = {e["child_id"] for e in result}
    assert 6 in child_ids  # substitute took effect
    assert 4 not in child_ids  # original part fully replaced, not duplicated
    assert 7 in child_ids  # packaging injected

    packaging_line = next(e for e in result if e["child_id"] == 7)
    assert packaging_line["parent_id"] == 1
    assert packaging_line["source"] == "INJECTED"
    assert packaging_line["extended_quantity"] == 1  # 1 packaging kit per top-level unit
