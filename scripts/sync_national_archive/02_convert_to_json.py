# This script scans an input directory of Akoma Ntoso (or other) XML files,
# inventories all tags/attributes encountered, and converts each XML to a
# consistent JSON representation.
#
# Usage from a terminal:
#   python akn_xml_to_json.py --input "/path/to/judgements_xml/dump" --output "/path/to/out_json"
#
# The JSON representation uses a stable convention:
#   - Element attributes are under the "@attrs" object.
#   - Element text (if non-empty) is under the "#text" key.
#   - Child elements are keyed by *local* tag name (namespace stripped).
#   - If multiple children share the same tag name, the value is a list (else a single object).
#   - Unknown / new tags are naturally included by the generic recursion.
#   - Namespaces found in the root element are recorded once at the top "_namespaces".
#
# Additionally, the script writes an inventory in the output directory:
#   - schema_tags.json: map of "path" -> count of occurrences across all files
#   - schema_attrs.json: map of "path@attr" -> count of occurrences across all files
#   - schema_summary.md: human-readable summary of what was found
#
# Paths are slash-separated local tag names from the document root, e.g. "akomaNtoso/judgment/meta/identification/FRBRWork".
#
# NOTE: Only the Python standard library is used.
import os
import sys
import json
import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter
from typing import Dict, Any, Tuple, List, Optional

def strip_ns(tag: str) -> str:
    """Return the local tag name without the namespace."""
    if tag is None:
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag

def gather_namespaces(elem: ET.Element) -> Dict[str, str]:
    """
    Best-effort extraction of namespace prefixes from the root tag's attributes.
    xml.etree doesn't retain the xmlns declarations explicitly, so we infer from attributes.
    """
    ns_map = {}
    for k, v in elem.attrib.items():
        # Namespace declarations don't appear in attrib with xml.etree, but we keep this for completeness.
        if k.startswith("xmlns"):
            # k may be 'xmlns' or 'xmlns:prefix'
            parts = k.split(":", 1)
            if len(parts) == 2:
                ns_map[parts[1]] = v
            else:
                ns_map[""] = v
    return ns_map

def element_to_obj(elem: ET.Element, path_stack: List[str], tag_counts: Counter, attr_counts: Counter) -> Any:
    """
    Convert an XML element into a consistent JSON-friendly object.
    Also populate tag/attribute occurrence stats using local-name paths.
    """
    name = strip_ns(elem.tag)
    # register this element path
    path = "/".join(path_stack + [name]) if path_stack else name
    tag_counts[path] += 1

    # Build node
    node: Dict[str, Any] = {}

    # Attributes under "@attrs"
    if elem.attrib:
        node["@attrs"] = {}
        for ak, av in elem.attrib.items():
            a_local = strip_ns(ak)
            node["@attrs"][a_local] = av
            attr_counts[f"{path}@{a_local}"] += 1

    # Children
    children_by_name: Dict[str, List[Any]] = defaultdict(list)
    for child in list(elem):
        c_name = strip_ns(child.tag)
        child_obj = element_to_obj(child, path_stack + [name], tag_counts, attr_counts)
        children_by_name[c_name].append(child_obj)

    for c_name, items in children_by_name.items():
        if len(items) == 1:
            node[c_name] = items[0]
        else:
            node[c_name] = items

    # Text (if non-empty when stripped)
    txt = (elem.text or "").strip()
    if txt:
        node["#text"] = txt

    # If element has ONLY text and no attributes or children, we return just the text for compactness.
    # But to keep a *unique* representation across files, we will *not* collapse to raw text;
    # we always keep a dict node with "#text" key if present.
    # This ensures the shape of a given tag is uniform.
    if not node:
        # no attrs, no children, no text
        return {}

    return node

def convert_file(xml_path: str, out_path: str, tag_counts: Counter, attr_counts: Counter) -> Tuple[Optional[str], Optional[str]]:
    """
    Convert one XML file to JSON and write to out_path.
    Returns (error_message, out_path) where error_message is None on success.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        return (f"Parse error for '{xml_path}': {e}", None)

    # Build JSON
    root_name = strip_ns(root.tag)
    obj = element_to_obj(root, [], tag_counts, attr_counts)

    # Attempt to capture namespaces at the root (best-effort)
    ns_map = gather_namespaces(root)
    out = {
        root_name: obj
    }
    if ns_map:
        out["_namespaces"] = ns_map

    # Write JSON
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=False)

    return (None, out_path)

def walk_xml_files(input_dir: str) -> List[str]:
    files = []
    for root, _, filenames in os.walk(input_dir):
        for fn in filenames:
            if fn.lower().endswith(".xml"):
                files.append(os.path.join(root, fn))
    return files

def make_out_path(input_dir: str, output_dir: str, xml_path: str) -> str:
    rel = os.path.relpath(xml_path, input_dir)
    base, _ = os.path.splitext(rel)
    return os.path.join(output_dir, base + ".json")

def write_schema_reports(output_dir: str, tag_counts: Counter, attr_counts: Counter) -> None:
    os.makedirs(output_dir, exist_ok=True)
    tags_path = os.path.join(output_dir, "schema_tags.json")
    attrs_path = os.path.join(output_dir, "schema_attrs.json")
    summary_path = os.path.join(output_dir, "schema_summary.md")

    # Sort by path for deterministic outputs
    tags_sorted = dict(sorted(tag_counts.items(), key=lambda kv: kv[0]))
    attrs_sorted = dict(sorted(attr_counts.items(), key=lambda kv: kv[0]))

    with open(tags_path, "w", encoding="utf-8") as f:
        json.dump(tags_sorted, f, ensure_ascii=False, indent=2)

    with open(attrs_path, "w", encoding="utf-8") as f:
        json.dump(attrs_sorted, f, ensure_ascii=False, indent=2)

    # Human-readable summary
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("# XML → JSON Inventory Summary\n\n")
        f.write("## Tag paths encountered (path → count)\n\n")
        for p, c in tags_sorted.items():
            f.write(f"- `{p}` → {c}\n")
        f.write("\n## Attributes encountered (path@attr → count)\n\n")
        for p, c in attrs_sorted.items():
            f.write(f"- `{p}` → {c}\n")

def main():
    parser = argparse.ArgumentParser(description="Convert (Akoma Ntoso) XML files to consistent JSON and inventory schema.")
    parser.add_argument("--input", required=True, help="Input directory containing XML files")
    parser.add_argument("--output", required=True, help="Output directory to write JSON files and schema reports")
    args = parser.parse_args()

    input_dir = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output)

    xml_files = walk_xml_files(input_dir)
    if not xml_files:
        print(f"No .xml files found under: {input_dir}")
        sys.exit(1)

    print(f"Found {len(xml_files)} XML files. Converting...")

    tag_counts: Counter = Counter()
    attr_counts: Counter = Counter()

    errors: List[str] = []
    written = 0
    for i, xp in enumerate(xml_files, 1):
        outp = make_out_path(input_dir, output_dir, xp)
        err, out_file = convert_file(xp, outp, tag_counts, attr_counts)
        if err:
            errors.append(err)
            print(f"[{i}/{len(xml_files)}] ERROR: {err}")
        else:
            written += 1
            print(f"[{i}/{len(xml_files)}] Wrote: {out_file}")

    # Write schema reports
    write_schema_reports(output_dir, tag_counts, attr_counts)

    print(f"\nDone. JSON files written: {written}. Errors: {len(errors)}.")
    if errors:
        print("\nErrors encountered:")
        for e in errors:
            print(f" - {e}")

if __name__ == "__main__":
    main()
