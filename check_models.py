import json
import os
import glob

workflow_dir = r"E:\Calliope\example_ComfyUI_workflows"
model_dir = r"E:\ComfyUI-Easy-Install\ComfyUI\models"

# Gather all existing model files
existing_files_rel = {}
for root, dirs, files in os.walk(model_dir):
    for f in files:
        full = os.path.join(root, f)
        rel = os.path.relpath(full, model_dir).replace("\\", "/")
        existing_files_rel[rel.lower()] = full
        existing_files_rel[f.lower()] = full

# Collect all workflows
json_files = sorted(glob.glob(os.path.join(workflow_dir, "*.json")))

results = {}

for jf in json_files:
    fname = os.path.basename(jf)
    with open(jf, "r", encoding="utf-8") as f:
        data = json.load(f)

    models_found = []

    if "nodes" in data and isinstance(data["nodes"], list):
        # UI format
        for n in data["nodes"]:
            ntype = n.get("type", "Unknown")
            wvals = n.get("widgets_values", [])
            if isinstance(wvals, list):
                for val in wvals:
                    if isinstance(val, str) and any(val.lower().endswith(ext) for ext in [".safetensors", ".ckpt", ".pt", ".bin", ".pth", ".gguf"]):
                        models_found.append({
                            "node": ntype,
                            "key": "widget",
                            "model": val
                        })
    else:
        # API format
        for nid, node in data.items():
            if not isinstance(node, dict):
                continue
            ctype = node.get("class_type", "Unknown")
            inputs = node.get("inputs", {})
            for k, val in inputs.items():
                if isinstance(val, str) and any(val.lower().endswith(ext) for ext in [".safetensors", ".ckpt", ".pt", ".bin", ".pth", ".gguf"]):
                    models_found.append({
                        "node": ctype,
                        "key": k,
                        "model": val
                    })

    for item in models_found:
        m = item["model"]
        m_norm = m.replace("\\", "/").lower()
        m_base = os.path.basename(m_norm)
        exists = (m_norm in existing_files_rel) or (m_base in existing_files_rel)
        item["exists"] = exists
        if exists:
            item["found_at"] = existing_files_rel.get(m_norm, existing_files_rel.get(m_base))

    results[fname] = models_found

# Summary of all unique models across all workflows
all_unique_models = {}
for fname, mods in results.items():
    for item in mods:
        m = item["model"]
        if m not in all_unique_models:
            all_unique_models[m] = {
                "exists": item["exists"],
                "node_types": set(),
                "used_in": []
            }
        all_unique_models[m]["node_types"].add(item["node"])
        all_unique_models[m]["used_in"].append(fname)

print("=== WORKFLOW DETAILS ===")
for fname, mods in results.items():
    print(f"\n--- {fname} ---")
    if not mods:
        print("  (Nessun modello identificato)")
    for item in mods:
        status = "PRESENTE" if item["exists"] else "MANCANTE"
        print(f"  [{status}] Node: {item['node']} ({item['key']}) -> {item['model']}")

print("\n\n=== RIEPILOGO GENERALE MODELLI UNICI ===")
missing_count = sum(1 for m, info in all_unique_models.items() if not info["exists"])
present_count = sum(1 for m, info in all_unique_models.items() if info["exists"])
print(f"Totale modelli unici referenziati: {len(all_unique_models)}")
print(f"Modelli presenti: {present_count}")
print(f"Modelli mancanti: {missing_count}\n")

print("--- MODELLI MANCANTI ---")
for m, info in sorted(all_unique_models.items()):
    if not info["exists"]:
        nodes_str = ", ".join(info["node_types"])
        print(f"❌ {m}")
        print(f"   Nodi: {nodes_str}")
        print(f"   Workflow ({len(info['used_in'])}): {', '.join(info['used_in'])}")

print("\n--- MODELLI GIÀ PRESENTI ---")
for m, info in sorted(all_unique_models.items()):
    if info["exists"]:
        nodes_str = ", ".join(info["node_types"])
        print(f"✅ {m}")
        print(f"   Nodi: {nodes_str}")
