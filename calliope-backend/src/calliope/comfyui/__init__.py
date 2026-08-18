"""ComfyUI integration package."""
from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.patcher import patch_workflow

__all__ = ["parse_dynamic_inputs", "parse_dynamic_outputs", "patch_workflow"]
