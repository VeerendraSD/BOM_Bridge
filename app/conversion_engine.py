"""
The eBOM -> mBOM conversion engine.

This is the core of the project: given an engineering BOM (a tree built by
design engineers) and a table of manufacturing rules, produce a manufacturing
BOM that reflects how the product is actually built and procured.

The transform logic is split into small, pure functions that operate on plain
dicts (no SQLAlchemy objects) so they can be unit tested without a database or
app context. `run_conversion_for_part` is the thin persistence wrapper that
loads real data, calls the pure functions, and writes the result.

Pipeline
--------
1. explode_ebom      - walk the eBOM tree, produce a flat list of edges
2. apply_substitutes - swap out any part with an approved substitute
3. inject_items      - add manufacturing-only items rules say belong under
                        certain assembly types (packaging, fasteners, ...)
4. classify_edges    - stamp make/buy, work center, and scrap-adjusted
                        quantity onto every edge (eBOM-derived AND injected)
"""


def explode_ebom(bom_edges_by_parent, root_id, _visited=None, _parent_extended=1.0):
    """
    Walk the eBOM tree starting at root_id and return a flat list of edges.

    bom_edges_by_parent: dict[parent_id] -> list of edge dicts, each with
        keys: id (bom_item id), child_id, quantity, reference_designator
    Returns a list of edge dicts with keys:
        parent_id, child_id, quantity, extended_quantity, source_bom_item_id

    `quantity` is the direct quantity within the immediate parent (what a
    drawing callout shows). `extended_quantity` is that quantity multiplied
    down the full chain back to the root -- e.g. 2 bearings per housing
    assembly, times 3 housing assemblies per unit, is 6 bearings per unit.
    This is the number procurement actually needs for one top-level build.

    Guards against circular references (a part cannot end up as its own
    ancestor) by tracking visited parents on the current path.
    """
    _visited = _visited or set()
    if root_id in _visited:
        raise ValueError(f"Circular BOM reference detected at part {root_id}")
    _visited = _visited | {root_id}

    edges = []
    for edge in bom_edges_by_parent.get(root_id, []):
        extended = edge["quantity"] * _parent_extended
        edges.append({
            "parent_id": root_id,
            "child_id": edge["child_id"],
            "quantity": edge["quantity"],
            "extended_quantity": extended,
            "source_bom_item_id": edge["id"],
        })
        # recurse into the child in case it is itself an assembly
        edges.extend(explode_ebom(bom_edges_by_parent, edge["child_id"], _visited, extended))
    return edges


def apply_substitutes(edges, substitute_rules):
    """
    substitute_rules: list of dicts with keys target_part_id, replacement_part_id
    For every edge whose child matches a rule's target, replace the child with
    the approved replacement and mark provenance.
    """
    by_target = {r["target_part_id"]: r["replacement_part_id"] for r in substitute_rules}
    result = []
    for edge in edges:
        new_edge = dict(edge)
        if edge["child_id"] in by_target:
            new_edge["substituted_from_part_id"] = edge["child_id"]
            new_edge["child_id"] = by_target[edge["child_id"]]
            new_edge["source"] = "SUBSTITUTED"
        else:
            new_edge["substituted_from_part_id"] = None
            new_edge["source"] = "FROM_EBOM"
        result.append(new_edge)
    return result


def inject_items(edges, parts_by_id, root_id, add_item_rules):
    """
    add_item_rules: list of dicts with keys applies_to_part_type,
        add_item_part_id, add_item_quantity
    For every assembly node appearing as a parent in `edges` (plus root_id),
    whose part_type matches a rule, add a new manufacturing-only edge under it.
    The injected item's extended_quantity is scaled by how many of that
    assembly are needed per top-level unit (1.0 for the root itself).
    """
    extended_by_node = {root_id: 1.0}
    for e in edges:
        extended_by_node[e["child_id"]] = e["extended_quantity"]

    assembly_ids = {root_id} | {e["parent_id"] for e in edges}
    injected = []
    for assembly_id in assembly_ids:
        part = parts_by_id.get(assembly_id)
        if part is None:
            continue
        for rule in add_item_rules:
            if rule["applies_to_part_type"] == part["part_type"]:
                injected.append({
                    "parent_id": assembly_id,
                    "child_id": rule["add_item_part_id"],
                    "quantity": rule["add_item_quantity"],
                    "extended_quantity": rule["add_item_quantity"] * extended_by_node.get(assembly_id, 1.0),
                    "source_bom_item_id": None,
                    "substituted_from_part_id": None,
                    "source": "INJECTED",
                })
    return edges + injected


def classify_edges(edges, parts_by_id, make_or_buy_rules, scrap_rules, routing_rules):
    """
    Stamp make_or_buy, work_center, and scrap-adjusted quantity onto every
    edge based on the CHILD part's type. Rules are keyed by part_type; if
    multiple rules match the same type, the first one wins (rules should be
    kept mutually exclusive per type in practice).
    """
    mob_by_type = {r["applies_to_part_type"]: r["make_or_buy_value"] for r in make_or_buy_rules}
    scrap_by_type = {r["applies_to_part_type"]: r["scrap_factor"] for r in scrap_rules}
    routing_by_type = {r["applies_to_part_type"]: r["work_center"] for r in routing_rules}

    result = []
    for edge in edges:
        new_edge = dict(edge)
        child_type = parts_by_id.get(edge["child_id"], {}).get("part_type")
        new_edge["make_or_buy"] = mob_by_type.get(child_type)
        new_edge["work_center"] = routing_by_type.get(child_type)
        scrap_factor = scrap_by_type.get(child_type)
        if scrap_factor:
            new_edge["quantity"] = edge["quantity"] * scrap_factor
            new_edge["extended_quantity"] = edge["extended_quantity"] * scrap_factor
        result.append(new_edge)
    return result


def convert_ebom_to_mbom(root_id, parts_by_id, bom_edges_by_parent, rules):
    """
    Run the full pipeline. `rules` is a dict with keys:
        substitute, add_item, make_or_buy, scrap, routing
    each a list of plain-dict rules as consumed by the functions above.
    Returns the final flat list of mBOM edge dicts.
    """
    edges = explode_ebom(bom_edges_by_parent, root_id)
    edges = apply_substitutes(edges, rules.get("substitute", []))
    edges = inject_items(edges, parts_by_id, root_id, rules.get("add_item", []))
    edges = classify_edges(
        edges, parts_by_id,
        rules.get("make_or_buy", []), rules.get("scrap", []), rules.get("routing", []),
    )
    return edges


# ---------------------------------------------------------------------------
# Persistence wrapper: loads real ORM data, runs the pure pipeline above,
# and writes a ConversionRun + MBomItem rows + an audit log entry.
# ---------------------------------------------------------------------------

def run_conversion_for_part(root_part_id, actor="system"):
    from app.models import db, Part, BomItem, ConversionRule, ConversionRun, MBomItem, AuditLog

    parts_by_id = {
        p.id: {"id": p.id, "part_type": p.part_type, "part_number": p.part_number}
        for p in Part.query.all()
    }

    bom_edges_by_parent = {}
    for bi in BomItem.query.all():
        bom_edges_by_parent.setdefault(bi.parent_part_id, []).append({
            "id": bi.id, "child_id": bi.child_part_id, "quantity": bi.quantity,
        })

    all_rules = ConversionRule.query.filter_by(active=True).order_by(ConversionRule.priority).all()
    rules = {
        "substitute": [
            {"target_part_id": r.substitute_target_part_id, "replacement_part_id": r.substitute_replacement_part_id}
            for r in all_rules if r.rule_type == "SUBSTITUTE_PART"
        ],
        "add_item": [
            {"applies_to_part_type": r.applies_to_part_type, "add_item_part_id": r.add_item_part_id,
             "add_item_quantity": r.add_item_quantity}
            for r in all_rules if r.rule_type == "ADD_ITEM"
        ],
        "make_or_buy": [
            {"applies_to_part_type": r.applies_to_part_type, "make_or_buy_value": r.make_or_buy_value}
            for r in all_rules if r.rule_type == "MAKE_OR_BUY"
        ],
        "scrap": [
            {"applies_to_part_type": r.applies_to_part_type, "scrap_factor": r.scrap_factor}
            for r in all_rules if r.rule_type == "SCRAP_FACTOR"
        ],
        "routing": [
            {"applies_to_part_type": r.applies_to_part_type, "work_center": r.work_center}
            for r in all_rules if r.rule_type == "ROUTING"
        ],
    }

    mbom_edges = convert_ebom_to_mbom(root_part_id, parts_by_id, bom_edges_by_parent, rules)

    run = ConversionRun(root_part_id=root_part_id, created_by=actor)
    db.session.add(run)
    db.session.flush()  # get run.id

    for edge in mbom_edges:
        db.session.add(MBomItem(
            run_id=run.id,
            parent_part_id=edge["parent_id"],
            child_part_id=edge["child_id"],
            quantity=edge["quantity"],
            extended_quantity=edge["extended_quantity"],
            make_or_buy=edge.get("make_or_buy"),
            work_center=edge.get("work_center"),
            source=edge.get("source", "FROM_EBOM"),
            source_bom_item_id=edge.get("source_bom_item_id"),
            substituted_from_part_id=edge.get("substituted_from_part_id"),
        ))

    db.session.add(AuditLog(
        entity_type="ConversionRun", entity_id=run.id, action="CONVERTED", actor=actor,
        detail=f"Generated mBOM for part {root_part_id}: {len(mbom_edges)} line(s)",
    ))
    db.session.commit()
    return run
