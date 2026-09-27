"""
convert_api_to_workflow.py

Converts ComfyUI API prompt JSON format (exported via 'Export (API)' or used in /prompt)
into full visual ComfyUI workflow JSON format (with LiteGraph nodes, coordinates, links,
and widgets_values) suitable for drag-and-drop or loading into the ComfyUI canvas.
"""

import argparse
import glob
import json
import os
import shutil
import sys
import urllib.request
from collections import defaultdict, deque

COMFY_OBJECT_INFO_URL = "http://127.0.0.1:8188/object_info"
DEFAULT_INPUT_DIR = r"e:\Calliope\example_ComfyUI_workflows"
DEFAULT_OUTPUT_DIR = r"e:\Calliope\visual_workflows_ComfyUI"
DEFAULT_COMFY_USER_DIR = r"E:\ComfyUI-Easy-Install\ComfyUI\user\default\workflows\Calliope_Visual"


def get_object_info(url=COMFY_OBJECT_INFO_URL):
    """Fetches node metadata from the running ComfyUI instance."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Calliope-Converter/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Warning: could not fetch live object_info from {url}: {e}")
        return {}


def is_visual_workflow(data: dict) -> bool:
    """True for UI-format workflows (LiteGraph "nodes"/"links" lists), not API prompts."""
    return isinstance(data.get("nodes"), list) and isinstance(data.get("links"), list)


def api_to_workflow(api_data: dict, object_info: dict) -> dict:
    """
    Transforms a ComfyUI API JSON dict into a LiteGraph visual workflow dict.
    """
    # 1. Normalize node IDs to unique integers
    node_id_map = {}
    next_id = 1
    # Pure numeric keys get their integer value
    for nid in api_data.keys():
        if str(nid).isdigit():
            val = int(nid)
            node_id_map[nid] = val
            next_id = max(next_id, val + 1)
            
    # Subgraph or alphanumeric keys get an auto-incremented integer
    for nid in api_data.keys():
        if nid not in node_id_map:
            node_id_map[nid] = next_id
            next_id += 1

    # 2. Build DAG adjacency and in-degrees for topological layout
    adj = defaultdict(list)
    in_degree = defaultdict(int)
    nodes_api = {}

    for nid, node_data in api_data.items():
        if not isinstance(node_data, dict):
            continue
        nodes_api[nid] = node_data
        for param, val in node_data.get("inputs", {}).items():
            if isinstance(val, list) and len(val) == 2:
                src_nid = str(val[0])
                if src_nid in node_id_map:
                    adj[src_nid].append(nid)
                    in_degree[nid] += 1

    # 3. Compute topological depth (layer) for horizontal placement
    depth = {}
    q = deque([nid for nid in nodes_api if in_degree[nid] == 0])
    for nid in q:
        depth[nid] = 0

    while q:
        curr = q.popleft()
        curr_d = depth.get(curr, 0)
        for nxt in adj[curr]:
            in_degree[nxt] -= 1
            depth[nxt] = max(depth.get(nxt, 0), curr_d + 1)
            if in_degree[nxt] == 0:
                q.append(nxt)

    # Place any cycle or disconnected nodes at depth 0
    for nid in nodes_api:
        if nid not in depth:
            depth[nid] = 0

    # Group nodes by layer column
    layers = defaultdict(list)
    for nid in nodes_api:
        layers[depth[nid]].append(nid)

    # Layout coordinates (X = col * 360, Y stacked in each column)
    positions = {}
    col_width = 360
    for col_idx in sorted(layers.keys()):
        x = 60 + col_idx * col_width
        y = 60
        for nid in layers[col_idx]:
            positions[nid] = (x, y)
            num_inputs = len(nodes_api[nid].get("inputs", {}))
            h = max(110, 60 + num_inputs * 26)
            y += h + 40

    # 4. Construct LiteGraph nodes, links, and slot definitions
    links = []
    link_counter = 1
    node_outputs_links = defaultdict(lambda: defaultdict(list))
    node_configured_inputs = {}
    node_widgets_values = {}

    # Pass 1: Parse inputs, establish links, and separate widget values
    for nid, node_data in nodes_api.items():
        ctype = node_data.get("class_type", "Unknown")
        info = object_info.get(ctype, {})
        req_inputs = info.get("input", {}).get("required", {})
        opt_inputs = info.get("input", {}).get("optional", {})

        all_defs = list(req_inputs.items()) + list(opt_inputs.items())
        def_keys = [k for k, _ in all_defs]

        # Extra inputs in API node not listed in schema
        for k in node_data.get("inputs", {}):
            if k not in def_keys:
                all_defs.append((k, ["*", {}]))

        inputs_list = []
        widgets_values_list = []

        for param_name, param_def in all_defs:
            val = node_data.get("inputs", {}).get(param_name)
            type_spec = param_def[0] if isinstance(param_def, (list, tuple)) and len(param_def) > 0 else "*"
            config_dict = param_def[1] if isinstance(param_def, (list, tuple)) and len(param_def) > 1 and isinstance(param_def[1], dict) else {}

            is_combo = isinstance(type_spec, list)
            is_primitive = type_spec in ("INT", "FLOAT", "STRING", "BOOLEAN")

            # Check if input is linked to another node's output
            if isinstance(val, list) and len(val) == 2:
                src_nid = str(val[0])
                src_slot = int(val[1])
                target_slot = len(inputs_list)

                link_type = type_spec if not is_combo else "COMBO"
                if link_type == "*":
                    src_ctype = nodes_api.get(src_nid, {}).get("class_type")
                    src_info = object_info.get(src_ctype, {})
                    src_outs = src_info.get("output", [])
                    if src_slot < len(src_outs):
                        link_type = src_outs[src_slot]

                cur_link_id = link_counter
                link_counter += 1

                links.append([
                    cur_link_id,
                    node_id_map.get(src_nid, int(src_nid) if src_nid.isdigit() else 0),
                    src_slot,
                    node_id_map[nid],
                    target_slot,
                    link_type
                ])
                node_outputs_links[src_nid][src_slot].append(cur_link_id)

                inp_desc = {
                    "name": param_name,
                    "type": link_type,
                    "link": cur_link_id
                }
                if is_primitive or is_combo:
                    inp_desc["widget"] = {"name": param_name}
                inputs_list.append(inp_desc)

            else:
                # Not a link -> primitive or combo widget value
                if is_combo or is_primitive or val is not None:
                    if val is not None:
                        widget_val = val
                    else:
                        widget_val = config_dict.get("default", 0 if type_spec == "INT" else (0.0 if type_spec == "FLOAT" else (False if type_spec == "BOOLEAN" else "")))
                    widgets_values_list.append(widget_val)
                    if config_dict.get("control_after_generate"):
                        widgets_values_list.append("fixed")

        node_configured_inputs[nid] = inputs_list
        node_widgets_values[nid] = widgets_values_list

    # Pass 2: Build final LiteGraph nodes with output slots and properties
    nodes_lite = []
    for nid, node_data in nodes_api.items():
        ctype = node_data.get("class_type", "Unknown")
        info = object_info.get(ctype, {})
        outs = info.get("output", [])
        out_names = info.get("output_name", outs)

        outputs_list = []
        for s_idx, out_t in enumerate(outs):
            s_name = out_names[s_idx] if s_idx < len(out_names) else out_t
            s_links = node_outputs_links[nid].get(s_idx, [])
            outputs_list.append({
                "name": s_name,
                "type": out_t,
                "links": s_links if s_links else None,
                "slot_index": s_idx
            })

        x, y = positions.get(nid, (100, 100))
        num_in = len(node_configured_inputs[nid])
        num_out = len(outputs_list)
        num_w = len(node_widgets_values[nid])
        calc_height = max(100, 50 + max(num_in, num_out) * 24 + num_w * 24)

        meta = node_data.get("_meta", {})
        title = meta.get("title", ctype)

        node_obj = {
            "id": node_id_map[nid],
            "type": ctype,
            "pos": [x, y],
            "size": [300, calc_height],
            "flags": {},
            "order": node_id_map[nid],
            "mode": 0,
            "inputs": node_configured_inputs[nid],
            "outputs": outputs_list,
            "title": title,
            "properties": {
                "Node name for S&R": ctype
            },
            "widgets_values": node_widgets_values[nid]
        }
        nodes_lite.append(node_obj)

    # Sort nodes by id for deterministic serialization
    nodes_lite.sort(key=lambda n: n["id"])

    workflow = {
        "last_node_id": max(node_id_map.values()) if node_id_map else 0,
        "last_link_id": link_counter - 1,
        "nodes": nodes_lite,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4
    }
    return workflow


def convert_all(input_dir=DEFAULT_INPUT_DIR, output_dir=DEFAULT_OUTPUT_DIR, comfy_user_dir=DEFAULT_COMFY_USER_DIR):
    """Converts all API JSON workflows in input_dir and saves visual workflows."""
    os.makedirs(output_dir, exist_ok=True)
    if comfy_user_dir:
        os.makedirs(comfy_user_dir, exist_ok=True)

    obj_info = get_object_info()
    api_files = glob.glob(os.path.join(input_dir, "*.json"))
    if not api_files:
        print(f"No .json files found in {input_dir}")
        return

    print(f"Found {len(api_files)} API workflow files in {input_dir}")
    print(f"Connected to ComfyUI object_info ({len(obj_info)} node definitions)")
    print("-" * 60)

    results = []
    for af in api_files:
        basename = os.path.basename(af)
        clean_name = basename.replace("_API.json", ".json").replace("-API.json", ".json")
        out_path = os.path.join(output_dir, clean_name)

        try:
            with open(af, "r", encoding="utf-8") as f:
                api_data = json.load(f)

            # Already a visual (UI-format) workflow — e.g. one downloaded as-is.
            # Converting it as API JSON turns every node into "Unknown"; copy it.
            visual_wf = api_data if is_visual_workflow(api_data) else api_to_workflow(api_data, obj_info)

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(visual_wf, f, indent=2)

            if comfy_user_dir:
                comfy_dest = os.path.join(comfy_user_dir, clean_name)
                shutil.copy2(out_path, comfy_dest)

            node_count = len(visual_wf["nodes"])
            link_count = len(visual_wf["links"])
            print(f"[OK] {basename} -> {clean_name} ({node_count} nodes, {link_count} links)")
            results.append((clean_name, True, node_count, link_count))
        except Exception as e:
            print(f"[ERR] {basename} failed: {e}")
            results.append((clean_name, False, 0, str(e)))

    print("-" * 60)
    print(f"Saved visual workflows to: {output_dir}")
    if comfy_user_dir:
        print(f"Installed to ComfyUI workflows: {comfy_user_dir}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Convert ComfyUI API JSON to visual workflow JSON")
    parser.add_argument("--input", "-i", default=DEFAULT_INPUT_DIR, help="Input API JSON file or directory")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT_DIR, help="Output visual JSON file or directory")
    parser.add_argument("--no-comfy-sync", action="store_true", help="Do not copy to ComfyUI user workflows directory")
    args = parser.parse_args()

    comfy_dir = None if args.no_comfy_sync else DEFAULT_COMFY_USER_DIR

    if os.path.isfile(args.input):
        obj_info = get_object_info()
        with open(args.input, "r", encoding="utf-8") as f:
            api_data = json.load(f)
        visual_wf = api_to_workflow(api_data, obj_info)
        out_file = args.output
        if os.path.isdir(out_file):
            basename = os.path.basename(args.input).replace("_API.json", ".json")
            out_file = os.path.join(out_file, basename)
        os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(visual_wf, f, indent=2)
        print(f"Converted {args.input} -> {out_file}")
    else:
        convert_all(args.input, args.output, comfy_dir)


if __name__ == "__main__":
    main()
