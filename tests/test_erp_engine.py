"""
Unit tests for the pure ERP planning function. No database needed -- same
style as test_conversion_engine.py: plain dicts in, plain dicts out.
"""
from app.erp_engine import plan_erp_documents


def test_make_lines_become_work_orders():
    rollup = [{"part_id": 1, "total_qty": 3, "make_or_buy": "MAKE", "work_center": "ASSY-LINE-2"}]
    plan = plan_erp_documents(rollup, {}, default_supplier_id=99)
    assert plan["work_orders"] == [{"part_id": 1, "quantity": 3, "work_center": "ASSY-LINE-2"}]
    assert plan["po_lines_by_supplier"] == {}


def test_buy_line_with_no_stock_generates_full_po_quantity():
    rollup = [{"part_id": 2, "total_qty": 10, "make_or_buy": "BUY", "work_center": None}]
    plan = plan_erp_documents(rollup, {}, default_supplier_id=99)
    assert plan["po_lines_by_supplier"] == {99: [{"part_id": 2, "quantity": 10, "unit_cost": 0}]}
    assert plan["covered_by_stock"] == []


def test_buy_line_partially_covered_by_stock_orders_only_the_shortfall():
    rollup = [{"part_id": 2, "total_qty": 10, "make_or_buy": "BUY", "work_center": None}]
    inventory = {2: {"on_hand_qty": 6, "unit_cost": 1.5, "preferred_supplier_id": 5}}
    plan = plan_erp_documents(rollup, inventory, default_supplier_id=99)
    assert plan["po_lines_by_supplier"] == {5: [{"part_id": 2, "quantity": 4, "unit_cost": 1.5}]}


def test_buy_line_fully_covered_by_stock_needs_no_po():
    rollup = [{"part_id": 2, "total_qty": 5, "make_or_buy": "BUY", "work_center": None}]
    inventory = {2: {"on_hand_qty": 8, "unit_cost": 1.5, "preferred_supplier_id": 5}}
    plan = plan_erp_documents(rollup, inventory, default_supplier_id=99)
    assert plan["po_lines_by_supplier"] == {}
    assert plan["covered_by_stock"] == [2]


def test_unclassified_line_defaults_to_buy():
    # e.g. an injected manufacturing item with no MAKE_OR_BUY rule for its part type
    rollup = [{"part_id": 3, "total_qty": 1, "make_or_buy": None, "work_center": None}]
    plan = plan_erp_documents(rollup, {}, default_supplier_id=99)
    assert plan["po_lines_by_supplier"] == {99: [{"part_id": 3, "quantity": 1, "unit_cost": 0}]}


def test_lines_group_by_preferred_supplier():
    rollup = [
        {"part_id": 1, "total_qty": 5, "make_or_buy": "BUY", "work_center": None},
        {"part_id": 2, "total_qty": 3, "make_or_buy": "BUY", "work_center": None},
    ]
    inventory = {
        1: {"on_hand_qty": 0, "unit_cost": 2, "preferred_supplier_id": 7},
        2: {"on_hand_qty": 0, "unit_cost": 4, "preferred_supplier_id": 7},
    }
    plan = plan_erp_documents(rollup, inventory, default_supplier_id=99)
    assert len(plan["po_lines_by_supplier"]) == 1
    assert {l["part_id"] for l in plan["po_lines_by_supplier"][7]} == {1, 2}
